from __future__ import annotations

import importlib

import pytest

from naobot.interaction.orchestrator import InteractionOrchestrator
from naobot.interaction.session import InteractionSession
from naobot.media.backends import (
    ASRResult,
    FasterWhisperASR,
    IdentityResult,
    OpenCVMediaPipeIdentityFacade,
    OpenWakeWordDetector,
    SherpaOnnxTTS,
    TTSResult,
    VisionResult,
)
from naobot.media.pipeline import MediaPipeline
from naobot.media.protocol import MediaFrame
from naobot.models import MessageType
from naobot.settings import Settings


class SpyWakeWordProvider:
    def __init__(self, *, triggered: bool = False, greeting_detected: bool = False) -> None:
        self.triggered = triggered
        self.greeting_detected = greeting_detected
        self.calls = 0

    def detect(self, audio_frames):
        self.calls += 1
        return {
            "triggered": self.triggered,
            "trigger": "wake_word" if self.triggered else None,
            "greeting_detected": self.greeting_detected,
        }


class SpyIdentityProvider:
    def __init__(
        self,
        *,
        person_id: str | None = None,
        eye_contact_ms: int = 0,
        greeting_detected: bool = False,
        summary: str = "用户站在镜头前",
    ) -> None:
        self.person_id = person_id
        self.eye_contact_ms = eye_contact_ms
        self.greeting_detected = greeting_detected
        self.summary = summary
        self.calls = 0

    def identify(self, video_frames):
        self.calls += 1
        return IdentityResult(
            person_id=self.person_id,
            eye_contact_ms=self.eye_contact_ms,
            greeting_detected=self.greeting_detected,
            vision_summary=self.summary,
        )


class SpyASRProvider:
    def __init__(self, transcript: str = "你好，小龟") -> None:
        self.transcript = transcript
        self.calls = 0

    async def transcribe(self, audio_frames):
        self.calls += 1
        return ASRResult(transcript=self.transcript, is_final=True)


class SpyVisionProvider:
    def __init__(self, summary: str = "用户正在挥手") -> None:
        self.summary = summary
        self.calls = 0

    async def summarize(self, video_frames):
        self.calls += 1
        return VisionResult(summary=self.summary)


class SpyTTSProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def synthesize(self, text: str):
        self.calls += 1
        return TTSResult(audio=b"pcm", media_type="audio/pcm")


@pytest.mark.parametrize(
    ("trigger", "person_id"),
    [
        ("wake_word", None),
        ("touch", None),
        ("eye_contact", "person-123"),
        ("greeting", None),
    ],
)
def test_session_supports_four_activation_triggers(trigger: str, person_id: str | None) -> None:
    session = InteractionSession(session_idle_ms=30_000)

    if trigger == "wake_word":
        activated = session.activate_from_wake_word(now_ms=1_000, person_id=person_id)
    elif trigger == "touch":
        activated = session.activate_from_touch(now_ms=1_000, person_id=person_id)
    elif trigger == "eye_contact":
        activated = session.activate_from_eye_contact(
            now_ms=1_000,
            eye_contact_ms=1_500,
            person_id=person_id,
        )
    else:
        activated = session.activate_from_greeting(now_ms=1_000, person_id=person_id)

    snapshot = session.snapshot(now_ms=1_000)

    assert activated is True
    assert snapshot.active is True
    assert snapshot.session_trigger == trigger
    if person_id is None:
        assert snapshot.person_id is None
        assert snapshot.session_id.startswith("visitor-")
    else:
        assert snapshot.person_id == "person-123"
        assert snapshot.session_id == "person-123"


def test_session_idle_timeout_and_half_duplex_resume() -> None:
    session = InteractionSession(session_idle_ms=30_000, tts_resume_delay_ms=200)
    session.activate_from_touch(now_ms=1_000, person_id=None)

    assert session.snapshot(now_ms=1_001).listening is True

    session.mark_activity(now_ms=10_000)
    session.start_tts(now_ms=12_000)
    assert session.snapshot(now_ms=12_050).listening is False
    assert session.snapshot(now_ms=12_050).speaking is True

    session.end_tts(now_ms=12_100)
    assert session.snapshot(now_ms=12_299).listening is False
    assert session.snapshot(now_ms=12_299).speaking is False
    assert session.snapshot(now_ms=12_300).listening is True

    assert session.snapshot(now_ms=39_999).active is True
    assert session.snapshot(now_ms=40_001).active is False


def test_touch_keeps_existing_session_id_when_session_already_active() -> None:
    session = InteractionSession(session_idle_ms=30_000, tts_resume_delay_ms=200)
    session.activate_from_wake_word(now_ms=1_000, person_id=None)

    before = session.snapshot(now_ms=1_000).session_id
    session.activate_from_touch(now_ms=1_100, person_id=None)
    after = session.snapshot(now_ms=1_100).session_id

    assert before == after
    assert session.snapshot(now_ms=1_100).session_trigger == "touch"


def test_session_gate_uses_monotonic_now_ms() -> None:
    session = InteractionSession(session_idle_ms=30_000, tts_resume_delay_ms=200)
    session.activate_from_touch(now_ms=10_000, person_id=None)

    session.mark_activity(now_ms=9_000)
    assert session.snapshot(now_ms=9_500).active is True
    assert session.snapshot(now_ms=39_999).active is True
    assert session.snapshot(now_ms=40_001).active is False


@pytest.mark.asyncio
async def test_orchestrator_blocks_cloud_providers_before_session_activation() -> None:
    wake = SpyWakeWordProvider(triggered=False, greeting_detected=False)
    identity = SpyIdentityProvider(person_id=None, eye_contact_ms=0, greeting_detected=False)
    asr = SpyASRProvider()
    vision = SpyVisionProvider()
    tts = SpyTTSProvider()

    orchestrator = InteractionOrchestrator(
        settings=Settings(),
        pipeline=MediaPipeline(),
        session=InteractionSession(),
        wake_word=wake,
        identity=identity,
        asr=asr,
        vision=vision,
        tts=tts,
    )

    await orchestrator.observe_audio(
        [MediaFrame.audio_pcm16(b"audio", timestamp_ms=100, sequence=1)],
        now_ms=100,
    )
    await orchestrator.observe_video(
        [MediaFrame.jpeg(b"jpeg", timestamp_ms=100, sequence=1)],
        now_ms=100,
    )
    result = await orchestrator.speak_text("你好", now_ms=101)

    assert wake.calls == 1
    assert identity.calls == 1
    assert asr.calls == 0
    assert vision.calls == 0
    assert tts.calls == 0
    assert result is None


@pytest.mark.asyncio
async def test_orchestrator_converges_completed_utterance_into_event_envelope() -> None:
    orchestrator = InteractionOrchestrator(
        settings=Settings(),
        pipeline=MediaPipeline(),
        session=InteractionSession(),
        wake_word=SpyWakeWordProvider(triggered=True),
        identity=SpyIdentityProvider(person_id="person-42", eye_contact_ms=1_600),
        asr=SpyASRProvider(transcript="帮我记住今天下午开会"),
        vision=SpyVisionProvider(summary="用户正看着镜头并举手"),
        tts=SpyTTSProvider(),
    )

    await orchestrator.observe_video(
        [MediaFrame.jpeg(b"jpeg-1", timestamp_ms=100, sequence=1)],
        now_ms=100,
    )
    await orchestrator.observe_audio(
        [MediaFrame.audio_pcm16(b"wake", timestamp_ms=120, sequence=1)],
        now_ms=120,
    )

    envelope = await orchestrator.complete_utterance(
        audio_frames=[MediaFrame.audio_pcm16(b"speech", timestamp_ms=150, sequence=2)],
        video_frames=[
            MediaFrame.jpeg(b"jpeg-1", timestamp_ms=100, sequence=1),
            MediaFrame.jpeg(b"jpeg-2", timestamp_ms=160, sequence=2),
            MediaFrame.jpeg(b"jpeg-3", timestamp_ms=170, sequence=3),
            MediaFrame.jpeg(b"jpeg-4", timestamp_ms=180, sequence=4),
        ],
        now_ms=180,
    )

    stats = orchestrator.pipeline.stats()

    assert envelope is not None
    assert envelope.type == MessageType.EVENT
    assert envelope.payload["name"] == "user_utterance"
    assert envelope.payload["transcript"] == "帮我记住今天下午开会"
    assert envelope.payload["person_id"] == "person-42"
    assert envelope.payload["vision_summary"] == "用户正看着镜头并举手"
    assert len(envelope.payload["media_refs"]) == 3
    assert envelope.payload["session_trigger"] == "wake_word"
    assert stats["current_session"] == "person-42"
    assert stats["current_person"] == "person-42"
    assert stats["session_trigger"] == "wake_word"
    assert stats["last_transcript"] == "帮我记住今天下午开会"


@pytest.mark.asyncio
async def test_orchestrator_exposes_completed_turn_without_raw_jpeg_in_event_payload() -> None:
    orchestrator = InteractionOrchestrator(
        settings=Settings(),
        pipeline=MediaPipeline(),
        session=InteractionSession(),
        wake_word=SpyWakeWordProvider(triggered=True),
        identity=SpyIdentityProvider(person_id="person-7", eye_contact_ms=1_600),
        asr=SpyASRProvider(transcript="请看看这个"),
        vision=SpyVisionProvider(summary="用户拿着盒子"),
        tts=SpyTTSProvider(),
    )

    await orchestrator.observe_audio(
        [MediaFrame.audio_pcm16(b"wake", timestamp_ms=120, sequence=1)],
        now_ms=120,
    )

    turn = await orchestrator.complete_turn(
        audio_frames=[MediaFrame.audio_pcm16(b"speech", timestamp_ms=150, sequence=2)],
        video_frames=[
            MediaFrame.jpeg(b"jpeg-1", timestamp_ms=160, sequence=1),
            MediaFrame.jpeg(b"jpeg-2", timestamp_ms=170, sequence=2),
            MediaFrame.jpeg(b"jpeg-3", timestamp_ms=180, sequence=3),
            MediaFrame.jpeg(b"jpeg-4", timestamp_ms=190, sequence=4),
        ],
        now_ms=190,
    )

    assert turn is not None
    assert turn.event.payload["vision_summary"] == "用户拿着盒子"
    assert "jpeg-1" not in str(turn.event.payload)
    assert len(turn.vision_blocks) == 3


@pytest.mark.asyncio
async def test_orchestrator_rebuilds_session_when_video_identity_changes() -> None:
    session = InteractionSession()
    session.activate_from_wake_word(now_ms=100, person_id="person-a")
    orchestrator = InteractionOrchestrator(
        settings=Settings(),
        pipeline=MediaPipeline(),
        session=session,
        wake_word=SpyWakeWordProvider(),
        identity=SpyIdentityProvider(person_id="person-b", summary="检测到单人"),
        asr=SpyASRProvider(),
        vision=SpyVisionProvider(),
        tts=SpyTTSProvider(),
    )

    await orchestrator.observe_video(
        [MediaFrame.jpeg(b"person-b", timestamp_ms=200, sequence=1)],
        now_ms=200,
    )

    snapshot = session.snapshot(now_ms=200)
    stats = orchestrator.pipeline.stats()
    assert snapshot.session_id == "person-b"
    assert snapshot.person_id == "person-b"
    assert stats["current_session"] == "person-b"
    assert stats["current_person"] == "person-b"


@pytest.mark.asyncio
async def test_orchestrator_rebuilds_runtime_when_turn_identity_changes() -> None:
    session = InteractionSession()
    session.activate_from_wake_word(now_ms=100, person_id="person-a")
    orchestrator = InteractionOrchestrator(
        settings=Settings(),
        pipeline=MediaPipeline(),
        session=session,
        wake_word=SpyWakeWordProvider(),
        identity=SpyIdentityProvider(person_id="person-b", summary="检测到单人"),
        asr=SpyASRProvider(transcript="我是 B"),
        vision=SpyVisionProvider(summary="B 正在镜头前"),
        tts=SpyTTSProvider(),
    )

    turn = await orchestrator.complete_turn(
        audio_frames=[MediaFrame.audio_pcm16(b"speech-b", timestamp_ms=200, sequence=1)],
        video_frames=[MediaFrame.jpeg(b"person-b", timestamp_ms=200, sequence=1)],
        now_ms=200,
    )

    snapshot = session.snapshot(now_ms=200)
    stats = orchestrator.pipeline.stats()
    assert turn is not None
    assert turn.event.session_id == "person-b"
    assert turn.event.payload["person_id"] == "person-b"
    assert snapshot.session_id == "person-b"
    assert snapshot.person_id == "person-b"
    assert stats["current_session"] == "person-b"
    assert stats["current_person"] == "person-b"


@pytest.mark.asyncio
async def test_orchestrator_keeps_bound_runtime_for_unknown_turn_identity() -> None:
    session = InteractionSession()
    session.activate_from_wake_word(now_ms=100, person_id="person-a")
    orchestrator = InteractionOrchestrator(
        settings=Settings(),
        pipeline=MediaPipeline(),
        session=session,
        wake_word=SpyWakeWordProvider(),
        identity=SpyIdentityProvider(person_id=None, summary="检测到单人"),
        asr=SpyASRProvider(transcript="继续"),
        vision=SpyVisionProvider(summary="用户仍在镜头前"),
        tts=SpyTTSProvider(),
    )

    turn = await orchestrator.complete_turn(
        audio_frames=[MediaFrame.audio_pcm16(b"speech", timestamp_ms=200, sequence=1)],
        video_frames=[MediaFrame.jpeg(b"unknown", timestamp_ms=200, sequence=1)],
        now_ms=200,
    )

    snapshot = session.snapshot(now_ms=200)
    assert turn is not None
    assert turn.event.session_id == "person-a"
    assert turn.event.payload["person_id"] == "person-a"
    assert snapshot.session_id == "person-a"
    assert snapshot.person_id == "person-a"


@pytest.mark.asyncio
async def test_orchestrator_does_not_reactivate_session_on_followup_wakeword() -> None:
    orchestrator = InteractionOrchestrator(
        settings=Settings(),
        pipeline=MediaPipeline(),
        session=InteractionSession(),
        wake_word=SpyWakeWordProvider(triggered=True),
        identity=SpyIdentityProvider(person_id=None, eye_contact_ms=0),
        asr=SpyASRProvider(transcript="确认"),
        vision=SpyVisionProvider(summary="检测到单人"),
        tts=SpyTTSProvider(),
    )

    await orchestrator.observe_audio(
        [MediaFrame.audio_pcm16(b"wake", timestamp_ms=100, sequence=1, flags=1)],
        now_ms=10_000,
    )
    first_session = orchestrator.session.snapshot(now_ms=10_000).session_id

    await orchestrator.observe_audio(
        [MediaFrame.audio_pcm16(b"confirm", timestamp_ms=120, sequence=2, flags=3)],
        now_ms=10_200,
    )
    second_session = orchestrator.session.snapshot(now_ms=10_200).session_id

    assert first_session == second_session


def test_local_media_adapters_fail_with_clear_runtime_error_when_optional_deps_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import_module = importlib.import_module

    def fake_import(name: str, package: str | None = None):
        blocked = {
            "faster_whisper",
            "openwakeword",
            "cv2",
            "mediapipe",
            "sherpa_onnx",
        }
        if name in blocked:
            raise ImportError(name)
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", fake_import)

    with pytest.raises(RuntimeError, match="faster-whisper"):
        FasterWhisperASR(model_name="base")
    with pytest.raises(RuntimeError, match="openwakeword"):
        OpenWakeWordDetector()
    with pytest.raises(RuntimeError, match="mediapipe"):
        OpenCVMediaPipeIdentityFacade()
    with pytest.raises(RuntimeError, match="sherpa-onnx"):
        SherpaOnnxTTS()


def test_settings_from_env_includes_media_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAOBOT_DEVICE_TOKEN", "device-token")
    monkeypatch.setenv("NAOBOT_SESSION_IDLE_MS", "45000")
    monkeypatch.setenv("NAOBOT_VIDEO_EVENT_FPS", "18")
    monkeypatch.setenv("NAOBOT_MEDIA_AUDIO_QUEUE_LIMIT", "88")
    monkeypatch.setenv("NAOBOT_ASR_ENDPOINT", "https://asr.example.com/v1")
    monkeypatch.setenv("NAOBOT_TTS_MODEL", "tts-1")
    monkeypatch.setenv("NAOBOT_VISION_API_KEY", "vision-key")

    settings = Settings.from_env()

    assert settings.device_token == "device-token"
    assert settings.data_key is None
    assert settings.session_idle_ms == 45_000
    assert settings.video_fps == 10
    assert settings.video_event_fps == 18
    assert settings.media_video_window_ms == 10_000
    assert settings.media_audio_window_ms == 15_000
    assert settings.media_video_queue_limit == 20
    assert settings.media_audio_queue_limit == 88
    assert settings.asr_endpoint == "https://asr.example.com/v1"
    assert settings.tts_model == "tts-1"
    assert settings.vision_api_key == "vision-key"
