from __future__ import annotations

import sqlite3

import pytest
from cryptography.fernet import Fernet

from naobot.interaction.orchestrator import CompletedTurn
from naobot.media.backends import IdentityResult
from naobot.media.protocol import MediaFrame
from naobot.media.service import EnrollmentManager
from naobot.models import Envelope, MessageType
from naobot.runtime.persistence import FaceDataRepository, RuntimePersistence
from naobot.settings import Settings


class EnrollableIdentityProvider:
    def __init__(self) -> None:
        self.embedding_calls = []
        self.fail_after = None

    def identify(self, video_frames):
        return IdentityResult(person_id=None, eye_contact_ms=1_500, vision_summary="检测到单人")

    def create_embedding(self, video_frames):
        self.embedding_calls.append([frame.sequence for frame in video_frames])
        if self.fail_after == "embedding":
            raise RuntimeError("embedding failed")
        return [0.1, 0.2, 0.3]


def build_turn(*, transcript: str, session_id: str = "visitor-1") -> CompletedTurn:
    return CompletedTurn(
        event=Envelope(
            type=MessageType.EVENT,
            session_id=session_id,
            payload={
                "name": "user_utterance",
                "transcript": transcript,
                "person_id": None,
                "vision_summary": "检测到单人",
                "session_trigger": "wake_word",
            },
        ),
        vision_blocks=[],
    )


def build_known_turn(*, transcript: str, person_id: str = "person-1") -> CompletedTurn:
    return CompletedTurn(
        event=Envelope(
            type=MessageType.EVENT,
            session_id=person_id,
            payload={
                "name": "user_utterance",
                "transcript": transcript,
                "person_id": person_id,
                "vision_summary": "检测到单人",
                "session_trigger": "wake_word",
            },
        ),
        vision_blocks=[],
    )


@pytest.mark.asyncio
async def test_enrollment_manager_requires_pending_confirm_touch_and_five_frames(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("NAOBOT_DATA_KEY", Fernet.generate_key().decode("utf-8"))
    settings = Settings(runtime_dir=tmp_path, robot_id="robot-test")
    identity = EnrollableIdentityProvider()
    persistence = RuntimePersistence(settings)
    repository = FaceDataRepository(settings, persistence=persistence)
    manager = EnrollmentManager(
        settings=settings,
        identity=identity,
        persistence=persistence,
        repository=repository,
    )
    recent_frames = [
        MediaFrame.jpeg(f"jpeg-{index}".encode("ascii"), timestamp_ms=index, sequence=index)
        for index in range(1, 6)
    ]

    prompt = await manager.observe_turn(
        build_turn(transcript="请记住我"),
        single_person=True,
        recent_video_frames=recent_frames,
        now_ms=1_000,
    )
    confirmed = await manager.observe_turn(
        build_turn(transcript="确认"),
        single_person=True,
        recent_video_frames=recent_frames,
        now_ms=1_500,
    )
    finished = await manager.observe_touch(
        session_id="visitor-1",
        now_ms=1_800,
        recent_video_frames=recent_frames,
    )

    assert prompt is not None
    assert prompt["status"] == "pending"
    assert confirmed is not None
    assert confirmed["status"] == "awaiting_touch"
    assert finished["status"] == "completed"
    assert identity.embedding_calls == [[1, 2, 3, 4, 5]]
    assert (await persistence.list_people())[0]["person_id"] == finished["person_id"]
    assert len(await repository.list_samples(finished["person_id"])) == 5


@pytest.mark.asyncio
async def test_enrollment_manager_rejects_when_data_key_or_frames_missing(tmp_path) -> None:
    settings = Settings(runtime_dir=tmp_path, robot_id="robot-test")
    identity = EnrollableIdentityProvider()
    persistence = RuntimePersistence(settings)
    manager = EnrollmentManager(
        settings=settings,
        identity=identity,
        persistence=persistence,
    )
    frames = [MediaFrame.jpeg(b"jpeg", timestamp_ms=1, sequence=1)]

    rejected = await manager.observe_turn(
        build_turn(transcript="认识我"),
        single_person=True,
        recent_video_frames=frames,
        now_ms=1_000,
    )

    assert rejected is not None
    assert rejected["status"] == "rejected"
    assert "data key" in rejected["reason"]
    assert identity.embedding_calls == []


@pytest.mark.asyncio
async def test_enrollment_manager_cancel_clears_pending_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NAOBOT_DATA_KEY", Fernet.generate_key().decode("utf-8"))
    settings = Settings(runtime_dir=tmp_path, robot_id="robot-test")
    manager = EnrollmentManager(
        settings=settings,
        identity=EnrollableIdentityProvider(),
        persistence=RuntimePersistence(settings),
    )
    frames = [
        MediaFrame.jpeg(f"jpeg-{index}".encode("ascii"), timestamp_ms=index, sequence=index)
        for index in range(1, 6)
    ]

    await manager.observe_turn(
        build_turn(transcript="记住我"),
        single_person=True,
        recent_video_frames=frames,
        now_ms=1_000,
    )
    cancelled = await manager.cancel()

    assert cancelled["status"] == "cancelled"
    assert manager.status()["state"] == "idle"


@pytest.mark.asyncio
async def test_enrollment_manager_rejects_known_person_registration(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NAOBOT_DATA_KEY", Fernet.generate_key().decode("utf-8"))
    settings = Settings(runtime_dir=tmp_path, robot_id="robot-test")
    manager = EnrollmentManager(
        settings=settings,
        identity=EnrollableIdentityProvider(),
        persistence=RuntimePersistence(settings),
    )
    frames = [
        MediaFrame.jpeg(f"jpeg-{index}".encode("ascii"), timestamp_ms=index, sequence=index)
        for index in range(1, 6)
    ]

    rejected = await manager.observe_turn(
        build_known_turn(transcript="请记住我"),
        single_person=True,
        recent_video_frames=frames,
        now_ms=1_000,
    )

    assert rejected is not None
    assert rejected["status"] == "rejected"
    assert "unknown" in rejected["reason"]


@pytest.mark.asyncio
async def test_enrollment_manager_uses_host_time_not_device_relative_timestamp(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NAOBOT_DATA_KEY", Fernet.generate_key().decode("utf-8"))
    settings = Settings(runtime_dir=tmp_path, robot_id="robot-test", tts_resume_delay_ms=200)
    manager = EnrollmentManager(
        settings=settings,
        identity=EnrollableIdentityProvider(),
        persistence=RuntimePersistence(settings),
    )
    frames = [
        MediaFrame.jpeg(f"jpeg-{index}".encode("ascii"), timestamp_ms=1_000 + index, sequence=index)
        for index in range(1, 6)
    ]

    pending = await manager.observe_turn(
        build_turn(transcript="记住我"),
        single_person=True,
        recent_video_frames=frames,
        now_ms=10_000,
    )
    confirmed = await manager.observe_turn(
        build_turn(transcript="确认"),
        single_person=True,
        recent_video_frames=frames,
        now_ms=10_050,
    )
    finished = await manager.observe_touch(
        session_id="visitor-1",
        now_ms=10_300,
        recent_video_frames=frames,
    )

    assert pending["status"] == "pending"
    assert confirmed["status"] == "awaiting_touch"
    assert finished["status"] == "completed"


@pytest.mark.asyncio
async def test_runtime_persistence_enrollment_is_atomic_when_sample_insert_fails(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("NAOBOT_DATA_KEY", Fernet.generate_key().decode("utf-8"))
    settings = Settings(runtime_dir=tmp_path, robot_id="robot-test")
    persistence = RuntimePersistence(settings)
    samples = [
        {
            "sample_bytes": f"jpeg-{index}".encode("ascii"),
            "media_type": "image/jpeg" if index != 3 else None,
            "sha256": f"sha-{index}",
        }
        for index in range(1, 6)
    ]

    with pytest.raises(sqlite3.IntegrityError):
        await persistence.enroll_person_atomic(
            person_id="person-bad",
            embedding=[0.1, 0.2, 0.3],
            model_name="identity",
            samples=samples,  # type: ignore[arg-type]
        )

    with sqlite3.connect(tmp_path / "naobot.db") as conn:
        people = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
        embeddings = conn.execute("SELECT COUNT(*) FROM face_embeddings").fetchone()[0]
        samples = conn.execute("SELECT COUNT(*) FROM face_samples").fetchone()[0]

    assert people == 0
    assert embeddings == 0
    assert samples == 0
