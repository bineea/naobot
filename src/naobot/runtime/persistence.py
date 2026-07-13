from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
from agentscope.state import AgentState
from cryptography.fernet import Fernet
from pydantic import BaseModel

from ..settings import Settings


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _media_digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_embedding(embedding: list[float]) -> list[float]:
    vector = [float(value) for value in embedding]
    if not vector or any(not math.isfinite(value) for value in vector):
        raise ValueError("identity embedding must contain finite values")
    return vector


def _media_text_summary(
    *,
    media_type: str,
    source_kind: str,
    digest: str,
    name: str | None = None,
) -> dict[str, Any]:
    label = f" name={name}" if name else ""
    return {
        "type": "text",
        "text": (
            f"[媒体摘要{label} media_type={media_type} "
            f"source={source_kind} sha256={digest}]"
        ),
    }


def _media_source_summary(*, media_type: str, source_kind: str, digest: str) -> dict[str, Any]:
    return {
        "kind": "media_source_summary",
        "media_type": media_type,
        "source": source_kind,
        "sha256": digest,
    }


def _scrub_serialized_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _scrub_serialized_value(value.model_dump(mode="json"))
    if isinstance(value, dict):
        value_type = value.get("type")
        if value_type == "data" and isinstance(value.get("source"), dict):
            source = value["source"]
            source_type = source.get("type")
            media_type = str(source.get("media_type", "application/octet-stream"))
            if source_type == "base64":
                digest = _media_digest(str(source.get("data", "")))
                return _media_text_summary(
                    media_type=media_type,
                    source_kind="base64",
                    digest=digest,
                    name=value.get("name"),
                )
            if source_type == "url":
                digest = _media_digest(str(source.get("url", "")))
                return _media_text_summary(
                    media_type=media_type,
                    source_kind="url",
                    digest=digest,
                    name=value.get("name"),
                )
        if value_type == "base64":
            return _media_source_summary(
                media_type=str(value.get("media_type", "application/octet-stream")),
                source_kind="base64",
                digest=_media_digest(str(value.get("data", ""))),
            )
        if value_type == "url":
            return _media_source_summary(
                media_type=str(value.get("media_type", "application/octet-stream")),
                source_kind="url",
                digest=_media_digest(str(value.get("url", ""))),
            )
        return {key: _scrub_serialized_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_serialized_value(item) for item in value]
    if isinstance(value, tuple):
        return [_scrub_serialized_value(item) for item in value]
    return value


def scrub_agent_state_for_storage(state: AgentState) -> dict[str, Any]:
    serialized = state.model_dump(mode="json", exclude_none=True)
    return _scrub_serialized_value(serialized)


class RuntimePersistence:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db_path = Path(settings.runtime_dir) / "naobot.db"
        self._initialized = False
        self._init_lock = None

    def _fernet(self) -> Fernet:
        key = self.settings.data_key or os.getenv("NAOBOT_DATA_KEY")
        if not key:
            raise RuntimeError("NAOBOT_DATA_KEY 未配置，拒绝写入敏感人脸数据。")
        return Fernet(key.encode("utf-8"))

    async def _create_v2_schema(self, db: aiosqlite.Connection) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS people (
                robot_id TEXT NOT NULL,
                person_id TEXT NOT NULL,
                display_name TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (robot_id, person_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS face_embeddings (
                robot_id TEXT NOT NULL,
                person_id TEXT NOT NULL,
                model_name TEXT NOT NULL,
                embedding_ciphertext BLOB NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (robot_id, person_id, model_name),
                FOREIGN KEY (robot_id, person_id)
                    REFERENCES people(robot_id, person_id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS face_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                robot_id TEXT NOT NULL,
                person_id TEXT NOT NULL,
                media_type TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                sample_ciphertext BLOB NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (robot_id, person_id)
                    REFERENCES people(robot_id, person_id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS conversation_sessions (
                robot_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                person_id TEXT NOT NULL,
                is_guest INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (robot_id, session_id),
                FOREIGN KEY (robot_id, person_id)
                    REFERENCES people(robot_id, person_id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS agent_runtimes (
                robot_id TEXT NOT NULL,
                person_id TEXT NOT NULL,
                agent_role TEXT NOT NULL,
                state_json TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (robot_id, person_id, agent_role),
                FOREIGN KEY (robot_id, person_id)
                    REFERENCES people(robot_id, person_id) ON DELETE CASCADE
            )
            """,
        )
        for statement in statements:
            await db.execute(statement)

    async def _migrate_v1_to_v2(self, db: aiosqlite.Connection) -> None:
        legacy_tables = (
            "face_embeddings",
            "face_samples",
            "conversation_sessions",
            "agent_runtimes",
            "people",
        )
        for table in legacy_tables:
            await db.execute(f"ALTER TABLE {table} RENAME TO {table}_v1")

        await self._create_v2_schema(db)
        await db.execute(
            """
            INSERT INTO people(
                robot_id, person_id, display_name, metadata_json, created_at, updated_at
            )
            SELECT ?, person_id, display_name, metadata_json, created_at, updated_at
            FROM people_v1
            """,
            (self.settings.robot_id,),
        )

        for table in legacy_tables[:-1]:
            await db.execute(
                f"""
                INSERT OR IGNORE INTO people(
                    robot_id, person_id, display_name, metadata_json, created_at, updated_at
                )
                SELECT DISTINCT
                    legacy.robot_id,
                    person.person_id,
                    person.display_name,
                    person.metadata_json,
                    person.created_at,
                    person.updated_at
                FROM {table}_v1 AS legacy
                JOIN people_v1 AS person ON person.person_id = legacy.person_id
                """
            )

        await db.execute(
            """
            INSERT INTO face_embeddings(
                robot_id, person_id, model_name, embedding_ciphertext, version, updated_at
            )
            SELECT robot_id, person_id, model_name, embedding_ciphertext, version, updated_at
            FROM face_embeddings_v1
            """
        )
        await db.execute(
            """
            INSERT INTO face_samples(
                id, robot_id, person_id, media_type, sha256, sample_ciphertext, created_at
            )
            SELECT id, robot_id, person_id, media_type, sha256, sample_ciphertext, created_at
            FROM face_samples_v1
            """
        )
        await db.execute(
            """
            INSERT INTO conversation_sessions(
                robot_id, session_id, person_id, is_guest, status, created_at, updated_at
            )
            SELECT robot_id, session_id, person_id, is_guest, status, created_at, updated_at
            FROM conversation_sessions_v1
            """
        )
        await db.execute(
            """
            INSERT INTO agent_runtimes(
                robot_id, person_id, agent_role, state_json, version, updated_at
            )
            SELECT robot_id, person_id, agent_role, state_json, version, updated_at
            FROM agent_runtimes_v1
            """
        )

        for table in legacy_tables[:-1]:
            await db.execute(f"DROP TABLE {table}_v1")
        await db.execute("DROP TABLE people_v1")

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
                await db.execute("PRAGMA foreign_keys=OFF")
                await db.execute("BEGIN IMMEDIATE")
                try:
                    await db.execute(
                        """
                        CREATE TABLE IF NOT EXISTS schema_migrations (
                            version INTEGER PRIMARY KEY,
                            applied_at TEXT NOT NULL
                        )
                        """
                    )
                    async with db.execute("PRAGMA table_info(people)") as cursor:
                        people_columns = {row[1] for row in await cursor.fetchall()}
                    if people_columns and "robot_id" not in people_columns:
                        await self._migrate_v1_to_v2(db)
                    else:
                        await self._create_v2_schema(db)
                    now = _utc_now()
                    await db.execute(
                        """
                        INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                        VALUES (?, ?)
                        """,
                        (1, now),
                    )
                    await db.execute(
                        """
                        INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                        VALUES (?, ?)
                        """,
                        (2, now),
                    )
                    async with db.execute("PRAGMA foreign_key_check") as cursor:
                        violations = await cursor.fetchall()
                    if violations:
                        raise RuntimeError(f"SQLite 外键校验失败: {violations}")
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise
                finally:
                    await db.execute("PRAGMA foreign_keys=ON")
                async with db.execute("PRAGMA foreign_keys") as cursor:
                    foreign_keys = await cursor.fetchone()
                if foreign_keys != (1,):
                    raise RuntimeError("SQLite foreign_keys 未能恢复为 ON")
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
            if metadata is None:
                await db.execute(
                    """
                    INSERT INTO people(
                        robot_id, person_id, display_name, metadata_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(robot_id, person_id) DO UPDATE SET
                        display_name = COALESCE(excluded.display_name, people.display_name),
                        updated_at = excluded.updated_at
                    """,
                    (
                        self.settings.robot_id,
                        person_id,
                        display_name,
                        "{}",
                        now,
                        now,
                    ),
                )
            else:
                await db.execute(
                    """
                    INSERT INTO people(
                        robot_id, person_id, display_name, metadata_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(robot_id, person_id) DO UPDATE SET
                        display_name = COALESCE(excluded.display_name, people.display_name),
                        metadata_json = excluded.metadata_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        self.settings.robot_id,
                        person_id,
                        display_name,
                        json.dumps(metadata, ensure_ascii=False),
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
                    robot_id, session_id, person_id, is_guest, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(robot_id, session_id) DO UPDATE SET
                    person_id = excluded.person_id,
                    is_guest = excluded.is_guest,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    self.settings.robot_id,
                    session_id,
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
        await self.upsert_person(person_id, metadata=None)
        await self.upsert_session(
            state.session_id,
            person_id=person_id,
            is_guest=False,
            status="active",
        )
        scrubbed_payload = scrub_agent_state_for_storage(state)
        state_json = json.dumps(scrubbed_payload, ensure_ascii=False)
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

    async def list_people(self) -> list[dict[str, Any]]:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """
                SELECT person_id, display_name, metadata_json, created_at, updated_at
                FROM people
                WHERE robot_id = ?
                ORDER BY updated_at DESC, person_id ASC
                """,
                (self.settings.robot_id,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [
            {
                "person_id": row[0],
                "display_name": row[1],
                "metadata": json.loads(row[2]),
                "created_at": row[3],
                "updated_at": row[4],
            }
            for row in rows
        ]

    async def get_person(self, person_id: str) -> dict[str, Any] | None:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """
                SELECT person_id, display_name, metadata_json, created_at, updated_at
                FROM people
                WHERE robot_id = ? AND person_id = ?
                """,
                (self.settings.robot_id, person_id),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "person_id": row[0],
            "display_name": row[1],
            "metadata": json.loads(row[2]),
            "created_at": row[3],
            "updated_at": row[4],
        }

    async def list_sessions(self) -> list[dict[str, Any]]:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """
                SELECT session_id, person_id, is_guest, status, created_at, updated_at
                FROM conversation_sessions
                WHERE robot_id = ?
                ORDER BY updated_at DESC, session_id ASC
                """,
                (self.settings.robot_id,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [
            {
                "session_id": row[0],
                "person_id": row[1],
                "is_guest": bool(row[2]),
                "status": row[3],
                "created_at": row[4],
                "updated_at": row[5],
            }
            for row in rows
        ]

    async def delete_person(self, person_id: str) -> None:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN")
            try:
                await db.execute(
                    "DELETE FROM face_samples WHERE robot_id = ? AND person_id = ?",
                    (self.settings.robot_id, person_id),
                )
                await db.execute(
                    "DELETE FROM face_embeddings WHERE robot_id = ? AND person_id = ?",
                    (self.settings.robot_id, person_id),
                )
                await db.execute(
                    "DELETE FROM conversation_sessions WHERE robot_id = ? AND person_id = ?",
                    (self.settings.robot_id, person_id),
                )
                await db.execute(
                    "DELETE FROM agent_runtimes WHERE robot_id = ? AND person_id = ?",
                    (self.settings.robot_id, person_id),
                )
                await db.execute(
                    "DELETE FROM people WHERE robot_id = ? AND person_id = ?",
                    (self.settings.robot_id, person_id),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def enroll_person_atomic(
        self,
        *,
        person_id: str,
        embedding: list[float],
        model_name: str,
        samples: list[dict[str, Any]],
        display_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if len(samples) != 5:
            raise ValueError("enrollment requires exactly 5 samples")
        embedding = _validate_embedding(embedding)
        fernet = self._fernet()
        await self.initialize()
        now = _utc_now()
        embedding_ciphertext = fernet.encrypt(
            json.dumps({"embedding": embedding}, ensure_ascii=False).encode("utf-8")
        )
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN")
            try:
                await db.execute(
                    """
                    INSERT INTO people(
                        robot_id, person_id, display_name, metadata_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(robot_id, person_id) DO UPDATE SET
                        display_name = COALESCE(excluded.display_name, people.display_name),
                        metadata_json = excluded.metadata_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        self.settings.robot_id,
                        person_id,
                        display_name,
                        json.dumps(metadata or {}, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
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
                        embedding_ciphertext,
                        now,
                    ),
                )
                for sample in samples:
                    ciphertext = fernet.encrypt(bytes(sample["sample_bytes"]))
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
                            sample["media_type"],
                            sample["sha256"],
                            ciphertext,
                            now,
                        ),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise


class FaceDataRepository:
    def __init__(self, settings: Settings, persistence: RuntimePersistence | None = None) -> None:
        self.settings = settings
        self.persistence = persistence or RuntimePersistence(settings)

    def _fernet(self) -> Fernet:
        return self.persistence._fernet()

    async def upsert_embedding(
        self,
        person_id: str,
        embedding: list[float],
        *,
        model_name: str,
    ) -> None:
        embedding = _validate_embedding(embedding)
        fernet = self._fernet()
        await self.persistence.initialize()
        await self.persistence.upsert_person(person_id, metadata=None)
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
        return _validate_embedding(list(payload["embedding"]))

    async def list_embeddings(self, *, model_name: str | None = None) -> list[dict[str, Any]]:
        fernet = self._fernet()
        await self.persistence.initialize()
        query = """
            SELECT person_id, embedding_ciphertext
            FROM face_embeddings
            WHERE robot_id = ?
        """
        params: list[Any] = [self.settings.robot_id]
        if model_name:
            query += " AND model_name = ?"
            params.append(model_name)
        query += " ORDER BY person_id ASC, updated_at DESC"
        async with aiosqlite.connect(self.persistence.db_path) as db:
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
        embeddings: list[dict[str, Any]] = []
        seen: set[str] = set()
        for person_id, ciphertext in rows:
            if person_id in seen:
                continue
            payload = json.loads(fernet.decrypt(ciphertext).decode("utf-8"))
            embeddings.append(
                {
                    "person_id": person_id,
                    "embedding": _validate_embedding(list(payload["embedding"])),
                }
            )
            seen.add(person_id)
        return embeddings

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
        await self.persistence.upsert_person(person_id, metadata=None)
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
