from __future__ import annotations

import sqlite3
import threading

import pytest
from cryptography.fernet import Fernet

from naobot.interaction.orchestrator import CompletedTurn
from naobot.media.backends import (
    CosineIdentityMatcher,
    IdentityResult,
    MediaBackendError,
    OpenCVMediaPipeIdentityFacade,
)
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


@pytest.mark.asyncio
async def test_enrollment_refreshes_matcher_and_matches_registered_person_immediately(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("NAOBOT_DATA_KEY", Fernet.generate_key().decode("utf-8"))
    settings = Settings(runtime_dir=tmp_path, robot_id="robot-test")
    persistence = RuntimePersistence(settings)
    repository = FaceDataRepository(settings, persistence=persistence)
    matcher = CosineIdentityMatcher(threshold=0.8)
    facade = OpenCVMediaPipeIdentityFacade(
        jpeg_decoder=lambda payload: payload,
        face_detector=lambda image: [{"embedding_input": image}],
        embedder=lambda face_input: [0.0, 1.0] if face_input == b"unknown" else [1.0, 0.0],
        identity_matcher=matcher,
        match_interval_ms=0,
    )

    async def refresh() -> None:
        facade.refresh_embeddings(await repository.list_embeddings(model_name="identity"))

    manager = EnrollmentManager(
        settings=settings,
        identity=facade,
        persistence=persistence,
        repository=repository,
        on_identity_changed=refresh,
    )
    frames = [
        MediaFrame.jpeg(f"known-{index}".encode(), timestamp_ms=index, sequence=index)
        for index in range(1, 6)
    ]
    await manager.observe_turn(
        build_turn(transcript="记住我"),
        single_person=True,
        recent_video_frames=frames,
        now_ms=1_000,
    )
    await manager.observe_turn(
        build_turn(transcript="确认"),
        single_person=True,
        recent_video_frames=frames,
        now_ms=1_100,
    )
    completed = await manager.observe_touch(
        session_id="visitor-1",
        now_ms=1_200,
        recent_video_frames=frames,
    )

    known = facade.identify([MediaFrame.jpeg(b"known-probe", timestamp_ms=2_000, sequence=6)])
    unknown = facade.identify([MediaFrame.jpeg(b"unknown", timestamp_ms=2_001, sequence=7)])
    assert completed is not None
    assert known.person_id == completed["person_id"]
    assert unknown.person_id is None


@pytest.mark.asyncio
async def test_face_repository_lists_decrypted_embeddings_for_matcher_cache(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("NAOBOT_DATA_KEY", Fernet.generate_key().decode("utf-8"))
    settings = Settings(runtime_dir=tmp_path, robot_id="robot-test")
    persistence = RuntimePersistence(settings)
    repository = FaceDataRepository(settings, persistence=persistence)
    await repository.upsert_embedding("person-a", [1.0, 0.0], model_name="identity")
    await repository.upsert_embedding("person-b", [0.0, 1.0], model_name="other")

    embeddings = await repository.list_embeddings(model_name="identity")

    assert embeddings == [{"person_id": "person-a", "embedding": [1.0, 0.0]}]


def test_cosine_matcher_treats_low_similarity_as_unknown() -> None:
    matcher = CosineIdentityMatcher(threshold=0.8)
    matcher.replace_embeddings(
        [
            {"person_id": "person-a", "embedding": [1.0, 0.0]},
            {"person_id": "person-b", "embedding": [0.0, 1.0]},
        ]
    )

    assert matcher([0.99, 0.01]) == ("person-a", pytest.approx(0.9999, abs=0.001))
    assert matcher([0.7, 0.7]) is None


def test_identity_facade_creates_one_embedding_from_exactly_five_frames() -> None:
    vectors = {
        b"1": [1.0, 0.0],
        b"2": [0.99, 0.01],
        b"3": [1.0, 0.02],
        b"4": [0.98, 0.01],
        b"5": [1.0, 0.01],
    }
    facade = OpenCVMediaPipeIdentityFacade(
        jpeg_decoder=lambda payload: payload,
        face_detector=lambda image: [{"embedding_input": image}],
        embedder=lambda face_input: vectors[face_input],
    )
    frames = [
        MediaFrame.jpeg(str(index).encode("ascii"), timestamp_ms=index, sequence=index)
        for index in range(1, 6)
    ]

    embedding = facade.create_embedding(frames)

    assert embedding[0] > 0.99
    assert embedding[1] < 0.02
    with pytest.raises(ValueError, match="exactly 5"):
        facade.create_embedding(frames[:4])


def test_identity_facade_rejects_five_frames_from_different_people() -> None:
    vectors = {
        b"1": [1.0, 0.0],
        b"2": [1.0, 0.0],
        b"3": [1.0, 0.0],
        b"4": [0.0, 1.0],
        b"5": [0.0, 1.0],
    }
    facade = OpenCVMediaPipeIdentityFacade(
        jpeg_decoder=lambda payload: payload,
        face_detector=lambda image: [{"embedding_input": image}],
        embedder=lambda face_input: vectors[face_input],
        enrollment_similarity_threshold=0.8,
    )
    frames = [
        MediaFrame.jpeg(str(index).encode("ascii"), timestamp_ms=index, sequence=index)
        for index in range(1, 6)
    ]

    with pytest.raises(MediaBackendError, match="同一人"):
        facade.create_embedding(frames)


def test_identity_facade_match_interval_never_reuses_known_identity() -> None:
    calls = []
    matcher = CosineIdentityMatcher(threshold=0.9)
    matcher.replace_embeddings([{"person_id": "person-a", "embedding": [1.0, 0.0]}])
    facade = OpenCVMediaPipeIdentityFacade(
        jpeg_decoder=lambda payload: payload,
        face_detector=lambda image: [{"embedding_input": image}],
        embedder=lambda face_input: calls.append(face_input)
        or ([1.0, 0.0] if face_input == b"known" else [0.0, 1.0]),
        identity_matcher=matcher,
        match_interval_ms=1_000,
    )

    first = facade.identify([MediaFrame.jpeg(b"known", timestamp_ms=1_000, sequence=1)])
    second = facade.identify([MediaFrame.jpeg(b"unknown", timestamp_ms=1_500, sequence=2)])

    assert first.person_id == "person-a"
    assert second.person_id is None
    assert calls == [b"known"]


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_matcher_and_registration_reject_non_finite_embeddings(bad_value: float) -> None:
    matcher = CosineIdentityMatcher(threshold=0.8)
    with pytest.raises(ValueError, match="finite"):
        matcher.replace_embeddings([{"person_id": "person-a", "embedding": [bad_value, 1.0]}])
    matcher.replace_embeddings([{"person_id": "person-a", "embedding": [1.0, 0.0]}])
    with pytest.raises(ValueError, match="finite"):
        matcher([bad_value, 0.0])

    facade = OpenCVMediaPipeIdentityFacade(
        jpeg_decoder=lambda payload: payload,
        face_detector=lambda image: [{"embedding_input": image}],
        embedder=lambda _face_input: [bad_value, 1.0],
    )
    frames = [
        MediaFrame.jpeg(str(index).encode("ascii"), timestamp_ms=index, sequence=index)
        for index in range(1, 6)
    ]
    with pytest.raises(MediaBackendError, match="finite"):
        facade.create_embedding(frames)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
async def test_persistence_rejects_non_finite_embeddings_before_writing(
    tmp_path,
    monkeypatch,
    bad_value: float,
) -> None:
    monkeypatch.setenv("NAOBOT_DATA_KEY", Fernet.generate_key().decode("utf-8"))
    settings = Settings(runtime_dir=tmp_path, robot_id="robot-test")
    persistence = RuntimePersistence(settings)
    repository = FaceDataRepository(settings, persistence=persistence)

    with pytest.raises(ValueError, match="finite"):
        await repository.upsert_embedding(
            "person-bad",
            [1.0, bad_value],
            model_name="identity",
        )
    with pytest.raises(ValueError, match="finite"):
        await persistence.enroll_person_atomic(
            person_id="person-bad",
            embedding=[1.0, bad_value],
            model_name="identity",
            samples=[
                {
                    "sample_bytes": f"jpeg-{index}".encode(),
                    "media_type": "image/jpeg",
                    "sha256": f"sha-{index}",
                }
                for index in range(5)
            ],
        )

    assert await persistence.list_people() == []


@pytest.mark.asyncio
async def test_enrollment_embedding_runs_off_loop_and_rejects_inconsistent_frames(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("NAOBOT_DATA_KEY", Fernet.generate_key().decode("utf-8"))
    loop_thread = threading.get_ident()

    class InconsistentIdentity(EnrollableIdentityProvider):
        def __init__(self) -> None:
            super().__init__()
            self.embedding_thread = None

        def create_embedding(self, video_frames):
            self.embedding_thread = threading.get_ident()
            raise MediaBackendError("五帧注册未通过同一人一致性校验。")

    settings = Settings(runtime_dir=tmp_path, robot_id="robot-test")
    persistence = RuntimePersistence(settings)
    identity = InconsistentIdentity()
    manager = EnrollmentManager(
        settings=settings,
        identity=identity,
        persistence=persistence,
    )
    frames = [
        MediaFrame.jpeg(f"jpeg-{index}".encode(), timestamp_ms=index, sequence=index)
        for index in range(1, 6)
    ]
    await manager.observe_turn(
        build_turn(transcript="记住我"),
        single_person=True,
        recent_video_frames=frames,
        now_ms=1_000,
    )
    await manager.observe_turn(
        build_turn(transcript="确认"),
        single_person=True,
        recent_video_frames=frames,
        now_ms=1_100,
    )

    result = await manager.observe_touch(
        session_id="visitor-1",
        now_ms=1_200,
        recent_video_frames=frames,
    )

    assert result is not None
    assert result["status"] == "rejected"
    assert "同一人" in result["reason"]
    assert identity.embedding_thread != loop_thread
    assert await persistence.list_people() == []
