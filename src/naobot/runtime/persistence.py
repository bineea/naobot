from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
from agentscope.message import Base64Source, DataBlock, HintBlock, TextBlock, ToolResultBlock, URLSource
from agentscope.state import AgentState
from cryptography.fernet import Fernet

from ..settings import Settings


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _media_digest(source: Base64Source | URLSource) -> str:
    if isinstance(source, Base64Source):
        payload = source.data.encode("utf-8")
    else:
        payload = str(source.url).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _scrub_data_block(block: DataBlock) -> TextBlock:
    digest = _media_digest(block.source)
    media_type = block.source.media_type
    source_kind = "base64" if isinstance(block.source, Base64Source) else "url"
    name = block.name or "unnamed"
    return TextBlock(
        text=(
            f"[媒体摘要 name={name} media_type={media_type} "
            f"source={source_kind} sha256={digest}]"
        )
    )


def _scrub_block(block: Any) -> Any:
    if isinstance(block, DataBlock):
        return _scrub_data_block(block)
    if isinstance(block, HintBlock) and isinstance(block.hint, list):
        block.hint = [_scrub_block(item) for item in block.hint]
        return block
    if isinstance(block, ToolResultBlock) and isinstance(block.output, list):
        block.output = [_scrub_block(item) for item in block.output]
        return block
    return block


def scrub_agent_state_for_storage(state: AgentState) -> AgentState:
    scrubbed = state.model_copy(deep=True)
    if isinstance(scrubbed.summary, list):
        scrubbed.summary = [_scrub_block(block) for block in scrubbed.summary]
    scrubbed.context = [
        message.model_copy(
            update={"content": [_scrub_block(block) for block in deepcopy(message.content)]}
        )
        for message in scrubbed.context
    ]
    return scrubbed


class RuntimePersistence:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db_path = Path(settings.runtime_dir) / "naobot.db"
        self._initialized = False
        self._init_lock = None

    async def initialize(self) -> None:
        if self._initialized:
            return
        if self._init_lock is None:
            import asyncio

            self._init_lock = asyncio.Lock()
        async with self._init_lock:
            if self._initialized:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA journal_mode=WAL")
                await db.execute("PRAGMA foreign_keys=ON")
                await db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS people (
                        person_id TEXT PRIMARY KEY,
                        display_name TEXT,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS face_embeddings (
                        robot_id TEXT NOT NULL,
                        person_id TEXT NOT NULL,
                        model_name TEXT NOT NULL,
                        embedding_ciphertext BLOB NOT NULL,
                        version INTEGER NOT NULL DEFAULT 1,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (robot_id, person_id, model_name)
                    );

                    CREATE TABLE IF NOT EXISTS face_samples (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        robot_id TEXT NOT NULL,
                        person_id TEXT NOT NULL,
                        media_type TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        sample_ciphertext BLOB NOT NULL,
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS conversation_sessions (
                        session_id TEXT PRIMARY KEY,
                        robot_id TEXT NOT NULL,
                        person_id TEXT NOT NULL,
                        is_guest INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'active',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS agent_runtimes (
                        robot_id TEXT NOT NULL,
                        person_id TEXT NOT NULL,
                        agent_role TEXT NOT NULL,
                        state_json TEXT NOT NULL,
                        version INTEGER NOT NULL DEFAULT 1,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (robot_id, person_id, agent_role)
                    );
                    """
                )
                await db.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (1, _utc_now()),
                )
                await db.commit()
            self._initialized = True

    async def upsert_person(
        self,
        person_id: str,
        *,
        display_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self.initialize()
        now = _utc_now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO people(person_id, display_name, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(person_id) DO UPDATE SET
                    display_name = COALESCE(excluded.display_name, people.display_name),
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    person_id,
                    display_name,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            await db.commit()

    async def upsert_session(
        self,
        session_id: str,
        *,
        person_id: str,
        is_guest: bool,
        status: str = "active",
    ) -> None:
        await self.initialize()
        now = _utc_now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO conversation_sessions(
                    session_id, robot_id, person_id, is_guest, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    person_id = excluded.person_id,
                    is_guest = excluded.is_guest,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    session_id,
                    self.settings.robot_id,
                    person_id,
                    int(is_guest),
                    status,
                    now,
                    now,
                ),
            )
            await db.commit()

    async def load_agent_runtime(self, person_id: str, agent_role: str) -> AgentState | None:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """
                SELECT state_json
                FROM agent_runtimes
                WHERE robot_id = ? AND person_id = ? AND agent_role = ?
                """,
                (self.settings.robot_id, person_id, agent_role),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return AgentState.model_validate_json(row[0])

    async def save_agent_runtime(self, person_id: str, agent_role: str, state: AgentState) -> int:
        await self.initialize()
        await self.upsert_person(person_id)
        await self.upsert_session(
            state.session_id,
            person_id=person_id,
            is_guest=False,
            status="active",
        )
        scrubbed_state = scrub_agent_state_for_storage(state)
        state_json = scrubbed_state.model_dump_json(exclude_none=True)
        now = _utc_now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO agent_runtimes(robot_id, person_id, agent_role, state_json, version, updated_at)
                VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(robot_id, person_id, agent_role) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at,
                    version = agent_runtimes.version + 1
                """,
                (self.settings.robot_id, person_id, agent_role, state_json, now),
            )
            async with db.execute(
                """
                SELECT version
                FROM agent_runtimes
                WHERE robot_id = ? AND person_id = ? AND agent_role = ?
                """,
                (self.settings.robot_id, person_id, agent_role),
            ) as cursor:
                row = await cursor.fetchone()
            await db.commit()
        return int(row[0]) if row else 1

    async def delete_person_runtime(self, person_id: str) -> None:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM agent_runtimes WHERE robot_id = ? AND person_id = ?",
                (self.settings.robot_id, person_id),
            )
            await db.commit()


class FaceDataRepository:
    def __init__(self, settings: Settings, persistence: RuntimePersistence | None = None) -> None:
        self.settings = settings
        self.persistence = persistence or RuntimePersistence(settings)

    def _fernet(self) -> Fernet:
        key = self.settings.data_key or os.getenv("NAOBOT_DATA_KEY")
        if not key:
            raise RuntimeError("NAOBOT_DATA_KEY 未配置，拒绝写入敏感人脸数据。")
        return Fernet(key.encode("utf-8"))

    async def upsert_embedding(
        self,
        person_id: str,
        embedding: list[float],
        *,
        model_name: str,
    ) -> None:
        fernet = self._fernet()
        await self.persistence.initialize()
        await self.persistence.upsert_person(person_id)
        ciphertext = fernet.encrypt(
            json.dumps({"embedding": embedding}, ensure_ascii=False).encode("utf-8")
        )
        now = _utc_now()
        async with aiosqlite.connect(self.persistence.db_path) as db:
            await db.execute(
                """
                INSERT INTO face_embeddings(
                    robot_id, person_id, model_name, embedding_ciphertext, version, updated_at
                )
                VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(robot_id, person_id, model_name) DO UPDATE SET
                    embedding_ciphertext = excluded.embedding_ciphertext,
                    updated_at = excluded.updated_at,
                    version = face_embeddings.version + 1
                """,
                (
                    self.settings.robot_id,
                    person_id,
                    model_name,
                    ciphertext,
                    now,
                ),
            )
            await db.commit()

    async def get_embedding(self, person_id: str, *, model_name: str | None = None) -> list[float] | None:
        fernet = self._fernet()
        await self.persistence.initialize()
        query = """
            SELECT embedding_ciphertext
            FROM face_embeddings
            WHERE robot_id = ? AND person_id = ?
        """
        params: list[Any] = [self.settings.robot_id, person_id]
        if model_name:
            query += " AND model_name = ?"
            params.append(model_name)
        query += " ORDER BY updated_at DESC LIMIT 1"
        async with aiosqlite.connect(self.persistence.db_path) as db:
            async with db.execute(query, params) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        payload = json.loads(fernet.decrypt(row[0]).decode("utf-8"))
        return list(payload["embedding"])

    async def add_sample(
        self,
        person_id: str,
        sample_bytes: bytes,
        *,
        media_type: str,
        sha256: str,
    ) -> None:
        fernet = self._fernet()
        await self.persistence.initialize()
        await self.persistence.upsert_person(person_id)
        ciphertext = fernet.encrypt(sample_bytes)
        async with aiosqlite.connect(self.persistence.db_path) as db:
            await db.execute(
                """
                INSERT INTO face_samples(
                    robot_id, person_id, media_type, sha256, sample_ciphertext, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    self.settings.robot_id,
                    person_id,
                    media_type,
                    sha256,
                    ciphertext,
                    _utc_now(),
                ),
            )
            await db.commit()

    async def list_samples(self, person_id: str) -> list[dict[str, Any]]:
        await self.persistence.initialize()
        async with aiosqlite.connect(self.persistence.db_path) as db:
            async with db.execute(
                """
                SELECT id, media_type, sha256, created_at
                FROM face_samples
                WHERE robot_id = ? AND person_id = ?
                ORDER BY id ASC
                """,
                (self.settings.robot_id, person_id),
            ) as cursor:
                rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "media_type": row[1],
                "sha256": row[2],
                "created_at": row[3],
            }
            for row in rows
        ]
