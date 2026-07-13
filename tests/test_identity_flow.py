from __future__ import annotations

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

    def identify(self, video_frames):
        return IdentityResult(person_id=None, eye_contact_ms=1_500, vision_summary="检测到单人")

    def create_embedding(self, video_frames):
        self.embedding_calls.append([frame.sequence for frame in video_frames])
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
