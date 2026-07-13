from __future__ import annotations

import struct

import pytest

from naobot.interaction.orchestrator import InteractionOrchestrator
from naobot.interaction.session import InteractionSession
from naobot.media.backends import (
    ASRResult,
    IdentityResult,
    LocalPhraseWakeWordDetector,
    PCM16VoiceActivityDetector,
    TTSResult,
    VisionResult,
    WakeWordResult,
)
from naobot.media.pipeline import MediaPipeline
from naobot.media.protocol import MediaFrame
from naobot.settings import Settings


def pcm16(value: int, samples: int = 10) -> bytes:
    return struct.pack(f"<{samples}h", *([value] * samples))


def test_pcm16_vad_keeps_silence_quiet_and_ends_speech_after_silence() -> None:
    vad = PCM16VoiceActivityDetector(
        rms_threshold=500,
        sample_rate_hz=1_000,
        end_silence_ms=20,
    )

    quiet = vad.annotate(
        MediaFrame.audio_pcm16(pcm16(0), timestamp_ms=0, sequence=1)
    )
    speech = vad.annotate(
        MediaFrame.audio_pcm16(pcm16(2_000), timestamp_ms=10, sequence=2)
    )
    trailing_1 = vad.annotate(
        MediaFrame.audio_pcm16(pcm16(0), timestamp_ms=20, sequence=3)
    )
    trailing_2 = vad.annotate(
        MediaFrame.audio_pcm16(pcm16(0), timestamp_ms=30, sequence=4)
    )

    assert quiet.flags == 0
    assert speech.is_speech is True
    assert speech.is_end_of_utterance is False
    assert trailing_1.flags == 0
    assert trailing_2.is_end_of_utterance is True


def test_pcm16_vad_preserves_firmware_speech_and_eou_flags() -> None:
    vad = PCM16VoiceActivityDetector(rms_threshold=500)
    firmware_frame = MediaFrame.audio_pcm16(
        pcm16(0),
        timestamp_ms=10,
        sequence=1,
        flags=0x3,
    )

    annotated = vad.annotate(firmware_frame)

    assert annotated is firmware_frame
    assert annotated.is_speech is True
    assert annotated.is_end_of_utterance is True


def test_local_phrase_detector_supports_fake_and_is_disabled_without_configuration() -> None:
    greeting = LocalPhraseWakeWordDetector(transcriber=lambda _frames: "你好，小龟")
    disabled = LocalPhraseWakeWordDetector()
    frames = [
        MediaFrame.audio_pcm16(pcm16(1_000), timestamp_ms=0, sequence=1, flags=0x1),
        MediaFrame.audio_pcm16(pcm16(0), timestamp_ms=20, sequence=2, flags=0x2),
    ]

    greeting_result = WakeWordResult()
    disabled_result = WakeWordResult()
    for frame in frames:
        greeting_result = greeting.detect([frame])
        disabled_result = disabled.detect([frame])

    assert greeting_result.triggered is False
    assert greeting_result.greeting_detected is True
    assert disabled_result == WakeWordResult()


def test_local_phrase_detector_treats_injected_model_factory_as_configured() -> None:
    class Segment:
        text = "小龟"

    class Model:
        def transcribe(self, _audio):
            return [Segment()], None

    detector = LocalPhraseWakeWordDetector(model_factory=Model)
    result = detector.detect(
        [
            MediaFrame.audio_pcm16(
                pcm16(1_000),
                timestamp_ms=0,
                sequence=1,
                flags=0x3,
            )
        ]
    )

    assert result.triggered is True
    assert result.trigger == "local_phrase:小龟"


class QuietWakeWord:
    def detect(self, _frames):
        return WakeWordResult()


class TemporalIdentity:
    def __init__(self) -> None:
        self.calls = 0

    def identify(self, _frames):
        self.calls += 1
        return IdentityResult(person_id=None, vision_summary="检测到单人")


class SpyASR:
    def __init__(self) -> None:
        self.calls = 0

    async def transcribe(self, _frames):
        self.calls += 1
        return ASRResult(transcript="不应调用")


class SpyVision:
    def __init__(self) -> None:
        self.calls = 0

    async def summarize(self, _frames):
        self.calls += 1
        return VisionResult(summary="不应调用")


class SpyTTS:
    async def synthesize(self, _text):
        return TTSResult(audio=b"pcm")


@pytest.mark.asyncio
async def test_local_temporal_summary_runs_at_one_hz_in_ram_before_activation() -> None:
    identity = TemporalIdentity()
    asr = SpyASR()
    vision = SpyVision()
    orchestrator = InteractionOrchestrator(
        settings=Settings(temporal_summary_interval_ms=1_000),
        pipeline=MediaPipeline(),
        session=InteractionSession(),
        wake_word=QuietWakeWord(),
        identity=identity,
        asr=asr,
        vision=vision,
        tts=SpyTTS(),
    )

    await orchestrator.observe_video(
        [MediaFrame.jpeg(b"frame-a", timestamp_ms=0, sequence=1)],
        now_ms=0,
    )
    first = dict(orchestrator.last_temporal_summary or {})
    await orchestrator.observe_video(
        [MediaFrame.jpeg(b"frame-b", timestamp_ms=500, sequence=2)],
        now_ms=500,
    )
    assert orchestrator.last_temporal_summary == first
    await orchestrator.observe_video(
        [MediaFrame.jpeg(b"frame-c", timestamp_ms=1_000, sequence=3)],
        now_ms=1_000,
    )

    summary = orchestrator.last_temporal_summary
    assert summary is not None
    assert summary["timestamp_ms"] == 1_000
    assert summary["scene_summary"] == "检测到单人"
    assert 0.0 <= summary["motion_score"] <= 1.0
    assert asr.calls == 0
    assert vision.calls == 0


@pytest.mark.asyncio
async def test_active_visitor_session_upgrades_to_registered_identity() -> None:
    class KnownIdentity:
        def identify(self, _frames):
            return IdentityResult(
                person_id="person-known",
                eye_contact_ms=1_500,
                vision_summary="检测到单人",
            )

    session = InteractionSession()
    session.activate_from_touch(now_ms=1_000, person_id=None)
    assert session.snapshot(now_ms=1_000).session_id == "visitor-1000"
    orchestrator = InteractionOrchestrator(
        settings=Settings(),
        pipeline=MediaPipeline(),
        session=session,
        wake_word=QuietWakeWord(),
        identity=KnownIdentity(),
        asr=SpyASR(),
        vision=SpyVision(),
        tts=SpyTTS(),
    )

    await orchestrator.observe_video(
        [MediaFrame.jpeg(b"known", timestamp_ms=1_100, sequence=1)],
        now_ms=1_100,
    )

    snapshot = session.snapshot(now_ms=1_100)
    assert snapshot.person_id == "person-known"
    assert snapshot.session_id == "person-known"
