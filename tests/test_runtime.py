import asyncio
import sqlite3

import pytest
from agentscope.message import Base64Source, DataBlock, Msg, TextBlock, URLSource
from agentscope.state import AgentState
from cryptography.fernet import Fernet
from pydantic import BaseModel

from naobot.runtime.persistence import FaceDataRepository, RuntimePersistence
from naobot.runtime.registry import RuntimeRegistry
from naobot.settings import Settings


class NestedMediaModel(BaseModel):
    payload: object


class FailingSavePersistence(RuntimePersistence):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.fail_save = False

    async def save_agent_runtime(
        self,
        person_id: str,
        agent_role: str,
        state: AgentState,
        *,
        expected_generation: int | None = None,
    ) -> int:
        if self.fail_save:
            raise RuntimeError("save exploded")
        return await super().save_agent_runtime(
            person_id,
            agent_role,
            state,
            expected_generation=expected_generation,
        )


class BlockingSavePersistence(RuntimePersistence):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.save_reached_session = asyncio.Event()
        self.release_save = asyncio.Event()

    async def upsert_session(
        self,
        session_id: str,
        *,
        person_id: str,
        is_guest: bool,
        status: str = "active",
    ) -> None:
        self.save_reached_session.set()
        await self.release_save.wait()
        await super().upsert_session(
            session_id,
            person_id=person_id,
            is_guest=is_guest,
            status=status,
        )


def create_v1_runtime_database(db_path, *, robot_id: str) -> None:
    now = "2026-07-13T00:00:00+00:00"
    state_json = AgentState(
        session_id="legacy-session",
        summary="legacy-summary",
    ).model_dump_json(exclude_none=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE people (
                person_id TEXT PRIMARY KEY,
                display_name TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE face_embeddings (
                robot_id TEXT NOT NULL,
                person_id TEXT NOT NULL,
                model_name TEXT NOT NULL,
                embedding_ciphertext BLOB NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (robot_id, person_id, model_name),
                FOREIGN KEY (person_id) REFERENCES people(person_id) ON DELETE CASCADE
            );
            CREATE TABLE face_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                robot_id TEXT NOT NULL,
                person_id TEXT NOT NULL,
                media_type TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                sample_ciphertext BLOB NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (person_id) REFERENCES people(person_id) ON DELETE CASCADE
            );
            CREATE TABLE conversation_sessions (
                session_id TEXT PRIMARY KEY,
                robot_id TEXT NOT NULL,
                person_id TEXT NOT NULL,
                is_guest INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (person_id) REFERENCES people(person_id) ON DELETE CASCADE
            );
            CREATE TABLE agent_runtimes (
                robot_id TEXT NOT NULL,
                person_id TEXT NOT NULL,
                agent_role TEXT NOT NULL,
                state_json TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (robot_id, person_id, agent_role),
                FOREIGN KEY (person_id) REFERENCES people(person_id) ON DELETE CASCADE
            );
            """
        )
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)",
            (now,),
        )
        conn.execute(
            """
            INSERT INTO people(person_id, display_name, metadata_json, created_at, updated_at)
            VALUES ('legacy-person', '旧用户', '{"source":"v1"}', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO conversation_sessions(
                session_id, robot_id, person_id, is_guest, status, created_at, updated_at
            ) VALUES ('legacy-session', ?, 'legacy-person', 0, 'active', ?, ?)
            """,
            (robot_id, now, now),
        )
        conn.execute(
            """
            INSERT INTO agent_runtimes(
                robot_id, person_id, agent_role, state_json, version, updated_at
            ) VALUES (?, 'legacy-person', 'primary', ?, 3, ?)
            """,
            (robot_id, state_json, now),
        )
        conn.execute(
            """
            INSERT INTO face_embeddings(
                robot_id, person_id, model_name, embedding_ciphertext, version, updated_at
            ) VALUES (?, 'legacy-person', 'face-v1', X'0102', 2, ?)
            """,
            (robot_id, now),
        )
        conn.execute(
            """
            INSERT INTO face_samples(
                robot_id, person_id, media_type, sha256, sample_ciphertext, created_at
            ) VALUES (?, 'legacy-person', 'image/jpeg', 'legacy-sha', X'0304', ?)
            """,
            (robot_id, now),
        )


@pytest.mark.asyncio
async def test_same_person_different_roles_can_enter_runtime_contexts_in_parallel(tmp_path) -> None:
    registry = RuntimeRegistry(Settings(runtime_dir=tmp_path, robot_id="robot-test"))
    primary_entered = asyncio.Event()
    specialist_entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_runtime(role: str, entered: asyncio.Event) -> None:
        async with registry.person_runtime("person-parallel", role):
            entered.set()
            await release.wait()

    primary_task = asyncio.create_task(hold_runtime("primary", primary_entered))
    await asyncio.wait_for(primary_entered.wait(), timeout=0.5)
    specialist_task = asyncio.create_task(hold_runtime("safety-specialist", specialist_entered))

    await asyncio.wait_for(specialist_entered.wait(), timeout=0.5)
    release.set()
    await asyncio.gather(primary_task, specialist_task)


@pytest.mark.asyncio
async def test_same_person_same_role_runtime_contexts_remain_serialized(tmp_path) -> None:
    registry = RuntimeRegistry(Settings(runtime_dir=tmp_path, robot_id="robot-test"))
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def hold_first() -> None:
        async with registry.person_runtime("person-serial", "primary"):
            first_entered.set()
            await release_first.wait()

    async def enter_second() -> None:
        async with registry.person_runtime("person-serial", "primary"):
            second_entered.set()

    first_task = asyncio.create_task(hold_first())
    await asyncio.wait_for(first_entered.wait(), timeout=0.5)
    second_task = asyncio.create_task(enter_second())

    await asyncio.sleep(0.05)
    assert second_entered.is_set() is False

    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert second_entered.is_set() is True


@pytest.mark.asyncio
async def test_runtime_registry_initializes_schema_and_persists_person_state(tmp_path) -> None:
    settings = Settings(runtime_dir=tmp_path, robot_id="robot-test")
    registry = RuntimeRegistry(settings)

    state = AgentState(session_id="session-1", summary="持久化摘要")
    await registry.save_state("person-1", "primary", state)

    loaded = await registry.load_state("person-1", "primary")

    assert loaded.session_id == "session-1"
    assert loaded.summary == "持久化摘要"

    db_path = tmp_path / "naobot.db"
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {
        "schema_migrations",
        "people",
        "face_embeddings",
        "face_samples",
        "conversation_sessions",
        "agent_runtimes",
    }.issubset(tables)


@pytest.mark.asyncio
async def test_guest_runtime_is_memory_only_and_destroyable(tmp_path) -> None:
    settings = Settings(runtime_dir=tmp_path, robot_id="robot-test")
    registry = RuntimeRegistry(settings)

    guest_state = AgentState(session_id="guest-session", summary="访客上下文")
    await registry.save_state("guest-1", "primary", guest_state, is_guest=True)

    loaded = await registry.load_state("guest-1", "primary", is_guest=True)
    assert loaded.summary == "访客上下文"

    with sqlite3.connect(tmp_path / "naobot.db") as conn:
        runtime_count = conn.execute("SELECT COUNT(*) FROM agent_runtimes").fetchone()[0]
    assert runtime_count == 0

    await registry.destroy_guest_runtime("guest-1")
    reset_state = await registry.load_state("guest-1", "primary", is_guest=True)
    assert reset_state.summary == ""
    assert reset_state.session_id != "guest-session"


@pytest.mark.asyncio
async def test_runtime_registry_scrubs_media_payload_before_persisting(tmp_path) -> None:
    settings = Settings(runtime_dir=tmp_path, robot_id="robot-test")
    registry = RuntimeRegistry(settings)

    state = AgentState(
        session_id="session-media",
        context=[
            Msg(
                name="user",
                role="user",
                content=[
                    TextBlock(text="请看这个"),
                    DataBlock(
                        source=Base64Source(data="SECRET_BASE64_PAYLOAD", media_type="image/png"),
                        name="camera-frame",
                    ),
                    DataBlock(
                        source=URLSource(
                            url="https://example.com/private/frame.jpg",
                            media_type="image/jpeg",
                        ),
                        name="remote-frame",
                    ),
                ],
            )
        ],
    )

    await registry.save_state("person-media", "vision", state)
    loaded = await registry.load_state("person-media", "vision")

    raw_state = sqlite3.connect(tmp_path / "naobot.db").execute(
        "SELECT state_json FROM agent_runtimes WHERE person_id = ? AND agent_role = ?",
        ("person-media", "vision"),
    ).fetchone()[0]

    assert "SECRET_BASE64_PAYLOAD" not in raw_state
    assert "https://example.com/private/frame.jpg" not in raw_state
    assert any(
        getattr(block, "type", None) == "text" and "sha256=" in block.text
        for block in loaded.context[0].content
    )


@pytest.mark.asyncio
async def test_scrub_agent_state_recursively_cleans_middle_context_nested_media(tmp_path) -> None:
    settings = Settings(runtime_dir=tmp_path, robot_id="robot-test")
    registry = RuntimeRegistry(settings)

    state = AgentState(
        session_id="session-nested",
        middle_context={
            "nested": NestedMediaModel(
                payload={
                    "list": [
                        Base64Source(data="VERY_SECRET_BASE64", media_type="image/png"),
                        {"tuple": (URLSource(url="https://example.com/secret.png", media_type="image/png"),)},
                    ],
                    "block": DataBlock(
                        source=Base64Source(data="ANOTHER_SECRET", media_type="image/png"),
                        name="nested-block",
                    ),
                }
            )
        },
    )

    await registry.save_state("person-nested", "primary", state)
    loaded = await registry.load_state("person-nested", "primary")

    raw_state = sqlite3.connect(tmp_path / "naobot.db").execute(
        "SELECT state_json FROM agent_runtimes WHERE person_id = ? AND agent_role = ?",
        ("person-nested", "primary"),
    ).fetchone()[0]

    assert "VERY_SECRET_BASE64" not in raw_state
    assert "https://example.com/secret.png" not in raw_state
    assert "ANOTHER_SECRET" not in raw_state
    assert "sha256" in raw_state
    assert "nested" in loaded.middle_context


@pytest.mark.asyncio
async def test_runtime_registry_can_reset_person_runtime(tmp_path) -> None:
    settings = Settings(runtime_dir=tmp_path, robot_id="robot-test")
    registry = RuntimeRegistry(settings)

    await registry.save_state("person-reset", "primary", AgentState(session_id="session-reset"))
    await registry.reset_person_runtime("person-reset")

    state = await registry.load_state("person-reset", "primary")

    assert state.session_id != "session-reset"


@pytest.mark.asyncio
async def test_reset_waits_for_in_flight_role_runtime(tmp_path) -> None:
    registry = RuntimeRegistry(Settings(runtime_dir=tmp_path, robot_id="robot-test"))
    role_entered = asyncio.Event()
    release_role = asyncio.Event()

    async def hold_role_runtime() -> None:
        async with registry.person_runtime("person-reset-race", "primary"):
            role_entered.set()
            await release_role.wait()

    role_task = asyncio.create_task(hold_role_runtime())
    await asyncio.wait_for(role_entered.wait(), timeout=0.5)
    reset_task = asyncio.create_task(registry.reset_person_runtime("person-reset-race"))

    await asyncio.sleep(0.05)
    reset_finished_while_role_active = reset_task.done()
    release_role.set()
    await asyncio.gather(role_task, reset_task)

    assert reset_finished_while_role_active is False


@pytest.mark.asyncio
async def test_reset_waiting_for_one_person_does_not_block_another_person(tmp_path) -> None:
    registry = RuntimeRegistry(Settings(runtime_dir=tmp_path, robot_id="robot-test"))
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def hold_first_person() -> None:
        async with registry.person_runtime("person-reset-blocked", "primary"):
            first_entered.set()
            await release_first.wait()

    first_task = asyncio.create_task(hold_first_person())
    await asyncio.wait_for(first_entered.wait(), timeout=0.5)
    reset_task = asyncio.create_task(
        registry.reset_person_runtime("person-reset-blocked")
    )
    await asyncio.sleep(0.05)

    async def enter_other_person() -> None:
        async with registry.person_runtime("person-independent", "primary"):
            pass

    await asyncio.wait_for(enter_other_person(), timeout=0.5)
    release_first.set()
    await asyncio.gather(first_task, reset_task)


@pytest.mark.asyncio
async def test_stale_runtime_session_cannot_restore_state_after_reset(tmp_path) -> None:
    registry = RuntimeRegistry(Settings(runtime_dir=tmp_path, robot_id="robot-test"))

    async with registry.person_runtime("person-stale-reset", "primary") as old_session:
        pass

    await registry.reset_person_runtime("person-stale-reset")
    version = await old_session.save(
        AgentState(session_id="stale-session", summary="stale state")
    )
    loaded = await registry.load_state("person-stale-reset", "primary")

    assert version == 0
    assert loaded.session_id != "stale-session"
    assert loaded.summary == ""


@pytest.mark.asyncio
async def test_delete_waits_for_in_flight_save_and_prevents_session_resurrection(tmp_path) -> None:
    settings = Settings(runtime_dir=tmp_path, robot_id="robot-test")
    persistence = BlockingSavePersistence(settings)
    registry = RuntimeRegistry(settings, persistence=persistence)
    await persistence.upsert_person("person-delete-race", display_name="待删除")

    async with registry.person_runtime("person-delete-race", "primary") as session:
        pass

    save_task = asyncio.create_task(
        session.save(AgentState(session_id="delete-race-session", summary="stale state"))
    )
    await asyncio.wait_for(persistence.save_reached_session.wait(), timeout=0.5)
    delete_task = asyncio.create_task(persistence.delete_person("person-delete-race"))

    await asyncio.sleep(0.05)
    delete_finished_while_save_active = delete_task.done()
    persistence.release_save.set()
    await asyncio.gather(save_task, delete_task)

    stale_version = await session.save(
        AgentState(session_id="resurrected-session", summary="resurrected state")
    )

    assert delete_finished_while_save_active is False
    assert stale_version == 0
    assert await persistence.get_person("person-delete-race") is None
    assert await persistence.load_agent_runtime("person-delete-race", "primary") is None


@pytest.mark.asyncio
async def test_session_from_before_delete_stays_stale_after_person_is_recreated(tmp_path) -> None:
    settings = Settings(runtime_dir=tmp_path, robot_id="robot-test")
    persistence = RuntimePersistence(settings)
    registry = RuntimeRegistry(settings, persistence=persistence)
    await persistence.upsert_person("person-delete-generation", display_name="旧人物")
    await registry.reset_person_runtime("person-delete-generation")

    async with registry.person_runtime(
        "person-delete-generation", "primary"
    ) as session_before_delete:
        pass

    await persistence.delete_person("person-delete-generation")
    await persistence.upsert_person("person-delete-generation", display_name="新人物")

    stale_version = await session_before_delete.save(
        AgentState(session_id="stale-after-recreate", summary="stale state")
    )
    async with registry.person_runtime(
        "person-delete-generation", "primary"
    ) as fresh_session:
        fresh_version = await fresh_session.save(
            AgentState(session_id="fresh-after-recreate", summary="fresh state")
        )

    loaded = await persistence.load_agent_runtime("person-delete-generation", "primary")
    assert stale_version == 0
    assert fresh_version == 1
    assert loaded is not None
    assert loaded.session_id == "fresh-after-recreate"


@pytest.mark.asyncio
async def test_reenrollment_after_delete_allows_fresh_runtime_session(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("NAOBOT_DATA_KEY", Fernet.generate_key().decode("utf-8"))
    settings = Settings(runtime_dir=tmp_path, robot_id="robot-test")
    persistence = RuntimePersistence(settings)
    registry = RuntimeRegistry(settings, persistence=persistence)
    await persistence.upsert_person("person-reenroll", display_name="旧人物")
    await persistence.delete_person("person-reenroll")

    await persistence.enroll_person_atomic(
        person_id="person-reenroll",
        embedding=[0.25, 0.75],
        model_name="face-v1",
        samples=[
            {
                "sample_bytes": f"sample-{index}".encode(),
                "media_type": "image/jpeg",
                "sha256": f"sha-{index}",
            }
            for index in range(5)
        ],
        display_name="新人物",
    )

    async with registry.person_runtime("person-reenroll", "primary") as fresh_session:
        version = await fresh_session.save(
            AgentState(session_id="fresh-reenrolled-session", summary="fresh state")
        )

    assert version == 1
    loaded = await persistence.load_agent_runtime("person-reenroll", "primary")
    assert loaded is not None
    assert loaded.session_id == "fresh-reenrolled-session"


@pytest.mark.asyncio
async def test_runtime_registry_keeps_old_cache_when_persistent_save_fails(tmp_path) -> None:
    settings = Settings(runtime_dir=tmp_path, robot_id="robot-test")
    persistence = FailingSavePersistence(settings)
    registry = RuntimeRegistry(settings, persistence=persistence)

    await registry.save_state(
        "person-failure",
        "primary",
        AgentState(session_id="session-old", summary="old"),
    )

    persistence.fail_save = True
    with pytest.raises(RuntimeError, match="save exploded"):
        await registry.save_state(
            "person-failure",
            "primary",
            AgentState(session_id="session-new", summary="new"),
        )

    loaded = await registry.load_state("person-failure", "primary")

    assert loaded.session_id == "session-old"
    assert loaded.summary == "old"


@pytest.mark.asyncio
async def test_upsert_person_metadata_is_preserved_when_runtime_saves_without_metadata(tmp_path) -> None:
    settings = Settings(runtime_dir=tmp_path, robot_id="robot-test")
    persistence = RuntimePersistence(settings)

    await persistence.upsert_person("person-meta", metadata={"nickname": "小王"})
    await persistence.upsert_person("person-meta", metadata=None)
    await persistence.save_agent_runtime(
        "person-meta",
        "primary",
        AgentState(session_id="session-meta", summary="hello"),
    )

    metadata_json = sqlite3.connect(tmp_path / "naobot.db").execute(
        "SELECT metadata_json FROM people WHERE person_id = ?",
        ("person-meta",),
    ).fetchone()[0]

    assert metadata_json == '{"nickname": "小王"}'


@pytest.mark.asyncio
async def test_people_list_get_and_update_are_isolated_by_robot(tmp_path) -> None:
    persistence_a = RuntimePersistence(Settings(runtime_dir=tmp_path, robot_id="robot-a"))
    persistence_b = RuntimePersistence(Settings(runtime_dir=tmp_path, robot_id="robot-b"))

    await persistence_a.upsert_person(
        "shared-person",
        display_name="A 用户",
        metadata={"owner": "a"},
    )
    await persistence_b.upsert_person(
        "shared-person",
        display_name="B 用户",
        metadata={"owner": "b"},
    )

    assert [person["display_name"] for person in await persistence_a.list_people()] == ["A 用户"]
    assert [person["display_name"] for person in await persistence_b.list_people()] == ["B 用户"]
    assert (await persistence_a.get_person("shared-person"))["metadata"] == {"owner": "a"}
    assert (await persistence_b.get_person("shared-person"))["metadata"] == {"owner": "b"}

    await persistence_a.upsert_person(
        "shared-person",
        display_name="A 已更新",
        metadata={"owner": "a-updated"},
    )

    assert (await persistence_a.get_person("shared-person"))["display_name"] == "A 已更新"
    assert (await persistence_b.get_person("shared-person"))["display_name"] == "B 用户"
    assert (await persistence_b.get_person("shared-person"))["metadata"] == {"owner": "b"}


@pytest.mark.asyncio
async def test_delete_person_preserves_other_robot_data(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NAOBOT_DATA_KEY", Fernet.generate_key().decode("utf-8"))
    settings_a = Settings(runtime_dir=tmp_path, robot_id="robot-a")
    settings_b = Settings(runtime_dir=tmp_path, robot_id="robot-b")
    persistence_a = RuntimePersistence(settings_a)
    persistence_b = RuntimePersistence(settings_b)
    repo_b = FaceDataRepository(settings_b, persistence=persistence_b)

    await persistence_a.upsert_person("shared-person", display_name="A 用户")
    await persistence_b.upsert_person("shared-person", display_name="B 用户")
    await persistence_a.save_agent_runtime(
        "shared-person",
        "primary",
        AgentState(session_id="shared-session", summary="robot-a"),
    )
    await persistence_b.save_agent_runtime(
        "shared-person",
        "primary",
        AgentState(session_id="shared-session", summary="robot-b"),
    )
    await repo_b.upsert_embedding("shared-person", [0.2, 0.8], model_name="face-v1")

    await persistence_a.delete_person("shared-person")

    assert await persistence_a.get_person("shared-person") is None
    assert (await persistence_b.get_person("shared-person"))["display_name"] == "B 用户"
    assert (await persistence_b.load_agent_runtime("shared-person", "primary")).summary == "robot-b"
    assert await repo_b.get_embedding("shared-person", model_name="face-v1") == [0.2, 0.8]
    assert [session["session_id"] for session in await persistence_b.list_sessions()] == [
        "shared-session"
    ]


@pytest.mark.asyncio
async def test_v1_schema_migrates_to_v2_without_losing_data_and_is_idempotent(tmp_path) -> None:
    db_path = tmp_path / "naobot.db"
    create_v1_runtime_database(db_path, robot_id="robot-current")
    settings = Settings(runtime_dir=tmp_path, robot_id="robot-current")

    await RuntimePersistence(settings).initialize()
    await RuntimePersistence(settings).initialize()

    persistence = RuntimePersistence(settings)
    person = await persistence.get_person("legacy-person")
    runtime = await persistence.load_agent_runtime("legacy-person", "primary")
    with sqlite3.connect(db_path) as conn:
        versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations")]
        people_pk = {
            row[1]: row[5]
            for row in conn.execute("PRAGMA table_info(people)")
            if row[5]
        }
        session_pk = {
            row[1]: row[5]
            for row in conn.execute("PRAGMA table_info(conversation_sessions)")
            if row[5]
        }
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "people",
                "face_embeddings",
                "face_samples",
                "conversation_sessions",
                "agent_runtimes",
            )
        }
        foreign_key_violations = conn.execute("PRAGMA foreign_key_check").fetchall()

    assert versions == [1, 2]
    assert people_pk == {"robot_id": 1, "person_id": 2}
    assert session_pk == {"robot_id": 1, "session_id": 2}
    assert counts == {
        "people": 1,
        "face_embeddings": 1,
        "face_samples": 1,
        "conversation_sessions": 1,
        "agent_runtimes": 1,
    }
    assert foreign_key_violations == []
    assert person is not None
    assert person["display_name"] == "旧用户"
    assert person["metadata"] == {"source": "v1"}
    assert runtime is not None
    assert runtime.summary == "legacy-summary"


@pytest.mark.asyncio
async def test_face_repository_rejects_sensitive_writes_without_key(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("NAOBOT_DATA_KEY", raising=False)
    repo = FaceDataRepository(Settings(runtime_dir=tmp_path, robot_id="robot-test"))

    with pytest.raises(RuntimeError, match="NAOBOT_DATA_KEY"):
        await repo.upsert_embedding("person-1", [0.1, 0.2], model_name="face-v1")

    with pytest.raises(RuntimeError, match="NAOBOT_DATA_KEY"):
        await repo.add_sample("person-1", b"sample-binary", media_type="image/png", sha256="abc123")


@pytest.mark.asyncio
async def test_face_repository_encrypts_embeddings_and_samples(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NAOBOT_DATA_KEY", Fernet.generate_key().decode("utf-8"))
    settings = Settings(runtime_dir=tmp_path, robot_id="robot-test")
    repo = FaceDataRepository(settings)

    await repo.upsert_embedding("person-2", [0.25, 0.75], model_name="face-v1")
    await repo.add_sample(
        "person-2",
        b"face-sample-binary",
        media_type="image/png",
        sha256="face-sha",
    )

    assert await repo.get_embedding("person-2") == [0.25, 0.75]
    samples = await repo.list_samples("person-2")
    assert samples[0]["sha256"] == "face-sha"

    with sqlite3.connect(tmp_path / "naobot.db") as conn:
        embedding_blob = conn.execute(
            "SELECT embedding_ciphertext FROM face_embeddings WHERE person_id = ?",
            ("person-2",),
        ).fetchone()[0]
        sample_blob = conn.execute(
            "SELECT sample_ciphertext FROM face_samples WHERE person_id = ?",
            ("person-2",),
        ).fetchone()[0]

    assert b"0.25" not in embedding_blob
    assert b"face-sample-binary" not in sample_blob


@pytest.mark.asyncio
async def test_runtime_persistence_lists_people_sessions_and_deletes_person_cascade(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("NAOBOT_DATA_KEY", Fernet.generate_key().decode("utf-8"))
    settings = Settings(runtime_dir=tmp_path, robot_id="robot-test")
    persistence = RuntimePersistence(settings)
    repo = FaceDataRepository(settings, persistence=persistence)

    await persistence.upsert_person("person-a", display_name="阿甲", metadata={"role": "friend"})
    await persistence.save_agent_runtime(
        "person-a",
        "primary",
        AgentState(session_id="session-a", summary="hello"),
    )
    await persistence.upsert_session("session-a", person_id="person-a", is_guest=False)
    await repo.upsert_embedding("person-a", [0.1, 0.2], model_name="face-v1")
    await repo.add_sample("person-a", b"jpeg", media_type="image/jpeg", sha256="sha-a")

    people = await persistence.list_people()
    person = await persistence.get_person("person-a")
    sessions = await persistence.list_sessions()

    assert people[0]["person_id"] == "person-a"
    assert person is not None
    assert person["display_name"] == "阿甲"
    assert sessions[0]["session_id"] == "session-a"

    await persistence.delete_person("person-a")

    assert await persistence.get_person("person-a") is None
    assert await persistence.list_people() == []
    assert await persistence.list_sessions() == []
