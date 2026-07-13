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

    async def save_agent_runtime(self, person_id: str, agent_role: str, state: AgentState) -> int:
        if self.fail_save:
            raise RuntimeError("save exploded")
        return await super().save_agent_runtime(person_id, agent_role, state)


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
