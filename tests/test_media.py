from __future__ import annotations

import struct
import tomllib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from agentscope.message import DataBlock

from naobot.media import buffers
from naobot.media.backends import (
    ASRResult,
    CosineIdentityMatcher,
    FasterWhisperASR,
    MediaBackendError,
    OnnxFaceEmbedder,
    OpenAICompatibleASR,
    OpenAICompatibleTTS,
    OpenAICompatibleVisionProvider,
    OpenCVMediaPipeIdentityFacade,
    OpenCVMotionEstimator,
    OpenWakeWordDetector,
    SherpaOnnxTTS,
    TTSResult,
    build_vision_input_blocks,
)
from naobot.media.buffers import TimestampWindow
from naobot.media.pipeline import MediaPipeline
from naobot.media.protocol import (
    AUDIO_CAPABILITY,
    DEFAULT_MEDIA_CAPABILITIES,
    JPEG_CAPABILITY,
    NOMINAL_EVENT_VIDEO_FPS,
    NOMINAL_VIDEO_FPS,
    PCM16_MONO_16K_CAPABILITY,
    PROTOCOL_HEADER,
    PROTOCOL_MAGIC,
    PROTOCOL_VERSION,
    QVGA_CAPABILITY,
    MediaFrame,
    MediaFrameKind,
    MediaHello,
)
from naobot.media.service import MediaService
from naobot.settings import Settings


class FakeAsyncClient:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    async def post(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class FakeSegment:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeWhisperModel:
    def __init__(self) -> None:
        self.inputs = []

    def transcribe(self, audio, **kwargs):
        self.inputs.append((audio, kwargs))
        return [FakeSegment("你好"), FakeSegment(" 世界")], {"language": "zh"}


class FakeWakeWordModel:
    def __init__(self, score: float) -> None:
        self.score = score
        self.inputs = []

    def predict(self, audio):
        self.inputs.append(audio)
        return {"naobot": self.score}


class FakeSherpaEngine:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def generate(self, text: str):
        self.calls.append(text)
        return {"samples": [1, -1, 2]}


def test_media_ingress_queue_prioritizes_eou_over_old_jpeg_and_non_speech_audio() -> None:
    queue_type = getattr(buffers, "MediaIngressQueue", None)
    assert queue_type is not None, "MediaIngressQueue 尚未实现"
    queue = queue_type(maxsize=3)
    queue.put_nowait(MediaFrame.jpeg(b"jpeg", timestamp_ms=1, sequence=1))
    queue.put_nowait(
        MediaFrame.audio_pcm16(b"silence", timestamp_ms=2, sequence=2, flags=0)
    )
    queue.put_nowait(
        MediaFrame.audio_pcm16(b"speech", timestamp_ms=3, sequence=3, flags=1)
    )

    assert queue.put_nowait(
        MediaFrame.audio_pcm16(b"eou", timestamp_ms=4, sequence=4, flags=2)
    )
    assert queue.put_nowait(
        MediaFrame.audio_pcm16(b"speech-2", timestamp_ms=5, sequence=5, flags=1)
    )
    assert [queue.get_nowait().sequence for _ in range(3)] == [3, 4, 5]
    assert queue.dropped == {
        "total": 2,
        "by_kind": {"JPEG": 1, "AUDIO_PCM16": 1},
        "by_reason": {
            "evicted_oldest_jpeg": 1,
            "evicted_oldest_non_speech_audio": 1,
        },
    }


def test_media_ingress_queue_rejects_new_jpeg_when_only_speech_and_eou_are_queued() -> None:
    queue_type = getattr(buffers, "MediaIngressQueue", None)
    assert queue_type is not None, "MediaIngressQueue 尚未实现"
    queue = queue_type(maxsize=2)
    queue.put_nowait(
        MediaFrame.audio_pcm16(b"speech", timestamp_ms=1, sequence=1, flags=1)
    )
    queue.put_nowait(MediaFrame.audio_pcm16(b"eou", timestamp_ms=2, sequence=2, flags=2))

    assert queue.put_nowait(MediaFrame.jpeg(b"new", timestamp_ms=3, sequence=3)) is False
    assert [queue.get_nowait().sequence for _ in range(2)] == [1, 2]
    assert queue.dropped == {
        "total": 1,
        "by_kind": {"JPEG": 1},
        "by_reason": {"queue_full_protected": 1},
    }


def test_media_frame_roundtrip_and_strict_decode() -> None:
    frame = MediaFrame(
        kind=MediaFrameKind.AUDIO_PCM16,
        timestamp_ms=12_345,
        sequence=7,
        payload=b"\x01\x02\x03\x04",
        flags=3,
    )

    encoded = frame.encode()
    decoded = MediaFrame.decode(encoded)

    assert PROTOCOL_HEADER.size == 24
    assert encoded[:4] == b"NABM"
    assert decoded == frame
    magic_value, version_value, kind, flags, sequence, timestamp_ms, payload_length = (
        PROTOCOL_HEADER.unpack(encoded[: PROTOCOL_HEADER.size])
    )
    assert magic_value == PROTOCOL_MAGIC
    assert version_value == PROTOCOL_VERSION
    assert kind == MediaFrameKind.AUDIO_PCM16
    assert flags == 3
    assert sequence == 7
    assert timestamp_ms == 12_345
    assert payload_length == 4

    magic = bytearray(encoded)
    magic[0:4] = b"NOPE"
    with pytest.raises(ValueError, match="magic"):
        MediaFrame.decode(bytes(magic))

    version = bytearray(encoded)
    version[4] = PROTOCOL_VERSION + 1
    with pytest.raises(ValueError, match="version"):
        MediaFrame.decode(bytes(version))

    unknown_kind = bytearray(encoded)
    unknown_kind[5] = 99
    with pytest.raises(ValueError, match="kind"):
        MediaFrame.decode(bytes(unknown_kind))

    truncated = encoded[:-1]
    with pytest.raises(ValueError, match="length"):
        MediaFrame.decode(truncated)

    wrong_payload_length = bytearray(encoded)
    wrong_payload_length[20:24] = struct.pack(">I", 99)
    with pytest.raises(ValueError, match="length"):
        MediaFrame.decode(bytes(wrong_payload_length))


@pytest.mark.parametrize(
    ("factory", "size", "label"),
    [
        (MediaFrame.audio_pcm16, 64 * 1024 + 1, "AUDIO_PCM16"),
        (MediaFrame.jpeg, 256 * 1024 + 1, "JPEG"),
        (MediaFrame.tts_pcm16, 256 * 1024 + 1, "TTS_PCM16"),
    ],
)
def test_media_frame_rejects_payload_over_kind_limit(factory, size: int, label: str) -> None:
    with pytest.raises(ValueError, match=label):
        factory(b"x" * size, timestamp_ms=1, sequence=1)


def test_media_hello_defaults_publish_nominal_capabilities() -> None:
    hello = MediaHello(device_id="robot-1", token="secret", boot_id="boot-1")

    assert hello.capabilities["video"]["nominal_fps"] == NOMINAL_VIDEO_FPS
    assert hello.capabilities["video"]["event_fps"] == NOMINAL_EVENT_VIDEO_FPS
    assert hello.capabilities["video"]["resolution"] == QVGA_CAPABILITY
    assert hello.capabilities["audio"] == AUDIO_CAPABILITY
    assert hello.capabilities["audio"]["format"] == PCM16_MONO_16K_CAPABILITY
    assert hello.capabilities["image"] == JPEG_CAPABILITY
    assert hello.capabilities == DEFAULT_MEDIA_CAPABILITIES


def test_timestamp_window_rejects_out_of_order_timestamps() -> None:
    window = TimestampWindow(window_ms=1_000, timestamp_getter=lambda value: value)

    assert window.append(100) is True
    assert window.append(99) is False
    assert window.items() == [100]


def test_media_pipeline_trims_windows_by_timestamp() -> None:
    pipeline = MediaPipeline(video_window_ms=10_000, audio_window_ms=15_000)

    pipeline.push_video_frame(MediaFrame.jpeg(b"a", timestamp_ms=0, sequence=1))
    pipeline.push_video_frame(MediaFrame.jpeg(b"b", timestamp_ms=5_000, sequence=2))
    pipeline.push_video_frame(MediaFrame.jpeg(b"c", timestamp_ms=11_001, sequence=3))

    pipeline.push_audio_chunk(MediaFrame.audio_pcm16(b"a", timestamp_ms=0, sequence=1, flags=0))
    pipeline.push_audio_chunk(MediaFrame.audio_pcm16(b"b", timestamp_ms=10_000, sequence=2, flags=1))
    pipeline.push_audio_chunk(MediaFrame.audio_pcm16(b"c", timestamp_ms=16_001, sequence=3, flags=1))

    assert [frame.sequence for frame in pipeline.video_window()] == [2, 3]
    assert [chunk.frame.sequence for chunk in pipeline.audio_window()] == [2, 3]


def test_media_pipeline_enforces_backpressure_and_exposes_stats() -> None:
    pipeline = MediaPipeline(
        video_queue_limit=2,
        audio_queue_limit=3,
        video_window_ms=10_000,
        audio_window_ms=15_000,
    )

    pipeline.update_connection(True)
    pipeline.update_session("visitor-1", person_id="visitor", trigger="touch")

    pipeline.push_video_frame(MediaFrame.jpeg(b"1", timestamp_ms=100, sequence=1))
    pipeline.push_video_frame(MediaFrame.jpeg(b"2", timestamp_ms=200, sequence=2))
    pipeline.push_video_frame(MediaFrame.jpeg(b"3", timestamp_ms=300, sequence=3))

    pipeline.push_audio_chunk(MediaFrame.audio_pcm16(b"1", timestamp_ms=100, sequence=1, flags=0))
    pipeline.push_audio_chunk(MediaFrame.audio_pcm16(b"2", timestamp_ms=200, sequence=2, flags=1))
    pipeline.push_audio_chunk(MediaFrame.audio_pcm16(b"3", timestamp_ms=300, sequence=3, flags=0))
    pipeline.push_audio_chunk(MediaFrame.audio_pcm16(b"4", timestamp_ms=400, sequence=4, flags=1))
    pipeline.set_listening(True)
    pipeline.set_speaking(False)
    pipeline.set_last_transcript("你好")

    next_video = pipeline.next_video_frame()
    next_audio = pipeline.next_audio_chunk()
    stats = pipeline.stats()

    assert next_video is not None
    assert next_video.sequence == 3
    assert next_audio is not None
    assert next_audio.frame.sequence == 2
    assert [frame.sequence for frame in pipeline.video_queue()] == []
    assert [chunk.frame.sequence for chunk in pipeline.audio_queue()] == [3, 4]
    assert stats["connected"] is True
    assert stats["current_session"] == "visitor-1"
    assert stats["current_person"] == "visitor"
    assert stats["session_trigger"] == "touch"
    assert stats["audio_queue"] == 2
    assert stats["media_dropped"] == 3
    assert stats["listening"] is True
    assert stats["speaking"] is False
    assert stats["last_transcript"] == "你好"
    assert stats["video_fps"] > 0


def test_media_pipeline_rejects_out_of_order_frames_and_reads_speech_from_flags() -> None:
    pipeline = MediaPipeline(video_queue_limit=3, audio_queue_limit=3)

    pipeline.push_video_frame(MediaFrame.jpeg(b"newer", timestamp_ms=200, sequence=2))
    pipeline.push_video_frame(MediaFrame.jpeg(b"older", timestamp_ms=100, sequence=1))
    pipeline.push_audio_chunk(MediaFrame.audio_pcm16(b"speech", timestamp_ms=200, sequence=2, flags=1))
    pipeline.push_audio_chunk(MediaFrame.audio_pcm16(b"older", timestamp_ms=100, sequence=1, flags=0))

    assert [frame.sequence for frame in pipeline.video_queue()] == [2]
    assert [chunk.frame.sequence for chunk in pipeline.audio_queue()] == [2]
    assert pipeline.audio_queue()[0].is_speech is True
    assert pipeline.stats()["media_dropped"] == 2


def test_media_pipeline_global_backpressure_prefers_video_then_non_speech_then_speech() -> None:
    pipeline = MediaPipeline(video_queue_limit=2, audio_queue_limit=3)

    pipeline.push_video_frame(MediaFrame.jpeg(b"v1", timestamp_ms=1, sequence=1))
    pipeline.push_video_frame(MediaFrame.jpeg(b"v2", timestamp_ms=2, sequence=2))
    pipeline.push_audio_chunk(MediaFrame.audio_pcm16(b"a1", timestamp_ms=10, sequence=10, flags=0))
    pipeline.push_audio_chunk(MediaFrame.audio_pcm16(b"a2", timestamp_ms=11, sequence=11, flags=1))
    pipeline.push_audio_chunk(MediaFrame.audio_pcm16(b"a3", timestamp_ms=12, sequence=12, flags=1))

    pipeline.push_audio_chunk(MediaFrame.audio_pcm16(b"a4", timestamp_ms=13, sequence=13, flags=1))

    assert [frame.sequence for frame in pipeline.video_queue()] == [2]
    assert [chunk.frame.sequence for chunk in pipeline.audio_queue()] == [11, 12, 13]
    assert pipeline.stats()["media_dropped"] == 2

    pipeline.push_audio_chunk(MediaFrame.audio_pcm16(b"a5", timestamp_ms=14, sequence=14, flags=1))

    assert [frame.sequence for frame in pipeline.video_queue()] == [2]
    assert [chunk.frame.sequence for chunk in pipeline.audio_queue()] == [12, 13, 14]
    assert pipeline.stats()["media_dropped"] == 3


def test_build_vision_input_blocks_keeps_at_most_three_jpegs() -> None:
    blocks = build_vision_input_blocks([b"1", b"2", b"3", b"4"])

    assert len(blocks) == 3
    assert all(isinstance(block, DataBlock) for block in blocks)
    assert [block.source.media_type for block in blocks] == ["image/jpeg"] * 3


def test_media_pipeline_never_writes_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args, **kwargs):
        raise AssertionError("不应写磁盘")

    monkeypatch.setattr(Path, "open", fail)
    monkeypatch.setattr(Path, "write_text", fail)
    monkeypatch.setattr(Path, "write_bytes", fail)

    pipeline = MediaPipeline()
    pipeline.push_video_frame(MediaFrame.jpeg(b"v", timestamp_ms=1, sequence=1))
    pipeline.push_audio_chunk(MediaFrame.audio_pcm16(b"a", timestamp_ms=2, sequence=2, flags=1))

    assert pipeline.next_video_frame() is not None
    assert pipeline.next_audio_chunk() is not None


@pytest.mark.asyncio
async def test_local_faster_whisper_adapter_is_callable_with_injected_model() -> None:
    model = FakeWhisperModel()
    adapter = FasterWhisperASR(model=model)

    result = await adapter.transcribe(
        [MediaFrame.audio_pcm16(b"\x00\x80\xff\x7f", timestamp_ms=1, sequence=1, flags=1)]
    )

    assert result == ASRResult(transcript="你好 世界", is_final=True)
    assert str(model.inputs[0][0].dtype) == "float32"


def test_local_wakeword_adapter_is_callable_with_injected_model() -> None:
    model = FakeWakeWordModel(score=0.9)
    adapter = OpenWakeWordDetector(model=model, threshold=0.5, wakeword_name="naobot")

    result = adapter.detect(
        [MediaFrame.audio_pcm16(b"\x01\x00\x02\x00", timestamp_ms=1, sequence=1, flags=0)]
    )

    assert result.triggered is True
    assert result.trigger == "naobot"
    assert str(model.inputs[0].dtype) == "int16"


def test_local_identity_facade_is_callable_with_injected_components() -> None:
    adapter = OpenCVMediaPipeIdentityFacade(
        jpeg_decoder=lambda payload: {"image": payload},
        face_detector=lambda image: [{"embedding_input": image, "eye_contact": True}],
        embedder=lambda face: [0.1, 0.2],
        identity_matcher=lambda embedding: ("person-7", 0.92),
        eye_contact_estimator=lambda face: True,
    )

    result = adapter.identify([MediaFrame.jpeg(b"jpeg", timestamp_ms=1, sequence=1)])

    assert result.person_id == "person-7"
    assert result.eye_contact_ms == 1_500


def test_fake_decoder_detector_and_onnx_session_run_full_identity_path(monkeypatch) -> None:
    import numpy as np

    real_import = __import__("importlib").import_module

    class FakeCV2:
        COLOR_BGR2RGB = 1

        @staticmethod
        def resize(image, size):
            assert size == (2, 2)
            return image

        @staticmethod
        def cvtColor(image, _code):
            return image

    class FakeInput:
        name = "input"
        shape = [1, 3, 2, 2]

    class FakeSession:
        def __init__(self) -> None:
            self.calls = []

        def get_inputs(self):
            return [FakeInput()]

        def run(self, _outputs, feed):
            self.calls.append(feed["input"])
            return [np.asarray([[1.0, 0.0]], dtype=np.float32)]

    def fake_import(name: str, package: str | None = None):
        if name == "cv2":
            return FakeCV2
        return real_import(name, package)

    monkeypatch.setattr("naobot.media.backends.importlib.import_module", fake_import)
    session = FakeSession()
    detector_calls = []
    matcher = CosineIdentityMatcher(threshold=0.8)
    matcher.replace_embeddings([{"person_id": "person-full", "embedding": [1.0, 0.0]}])
    facade = OpenCVMediaPipeIdentityFacade(
        jpeg_decoder=lambda payload: np.full((2, 2, 3), payload[0], dtype=np.uint8),
        face_detector=lambda image: detector_calls.append(image) or [{"embedding_input": image}],
        embedder=OnnxFaceEmbedder("fake.onnx", session=session),
        identity_matcher=matcher,
        match_interval_ms=0,
    )

    result = facade.identify([MediaFrame.jpeg(b"\x7f", timestamp_ms=1, sequence=1)])

    assert result.person_id == "person-full"
    assert len(detector_calls) == 1
    assert len(session.calls) == 1


def test_default_mediapipe_detector_instance_is_cached(monkeypatch) -> None:
    factory_calls = []

    class Detector:
        def process(self, _image):
            return SimpleNamespace(detections=[{"face": 1}])

    class FakeCV2:
        COLOR_BGR2RGB = 1

        @staticmethod
        def cvtColor(image, _code):
            return image

    fake_mediapipe = SimpleNamespace(
        solutions=SimpleNamespace(
            face_detection=SimpleNamespace(
                FaceDetection=lambda **_kwargs: factory_calls.append(1) or Detector()
            )
        )
    )
    real_import = __import__("importlib").import_module

    def fake_import(name: str, package: str | None = None):
        if name == "cv2":
            return FakeCV2
        if name == "mediapipe":
            return fake_mediapipe
        return real_import(name, package)

    monkeypatch.setattr("naobot.media.backends.find_spec", lambda _name: object())
    monkeypatch.setattr("naobot.media.backends.importlib.import_module", fake_import)
    facade = OpenCVMediaPipeIdentityFacade(jpeg_decoder=lambda payload: payload)

    facade.identify([MediaFrame.jpeg(b"one", timestamp_ms=1, sequence=1)])
    facade.identify([MediaFrame.jpeg(b"two", timestamp_ms=2, sequence=2)])

    assert factory_calls == [1]


def test_opencv_motion_estimator_uses_gray_thumbnail_mad(monkeypatch) -> None:
    import numpy as np

    class FakeCV2:
        IMREAD_GRAYSCALE = 0
        INTER_AREA = 1

        @staticmethod
        def imdecode(encoded, _mode):
            return np.full((4, 4), int(encoded[0]), dtype=np.uint8)

        @staticmethod
        def resize(image, size, interpolation):
            assert interpolation == FakeCV2.INTER_AREA
            return np.full((size[1], size[0]), int(image[0, 0]), dtype=np.uint8)

        @staticmethod
        def absdiff(left, right):
            return np.abs(left.astype(np.int16) - right.astype(np.int16)).astype(np.uint8)

    real_import = __import__("importlib").import_module
    monkeypatch.setattr(
        "naobot.media.backends.importlib.import_module",
        lambda name, package=None: FakeCV2 if name == "cv2" else real_import(name, package),
    )
    estimator = OpenCVMotionEstimator(thumbnail_size=(32, 24))

    first = estimator.estimate(b"\x00jpeg-a")
    second = estimator.estimate(b"\xffjpeg-b")

    assert first.method == "opencv_gray_mad"
    assert first.score == 0.0
    assert second.score == pytest.approx(1.0)
    assert estimator.retained_feature_shape == (24, 32)


def test_opencv_motion_estimator_marks_missing_dependency_unavailable(monkeypatch) -> None:
    real_import = __import__("importlib").import_module

    def missing_cv2(name: str, package: str | None = None):
        if name == "cv2":
            raise ImportError("cv2")
        return real_import(name, package)

    monkeypatch.setattr("naobot.media.backends.importlib.import_module", missing_cv2)

    result = OpenCVMotionEstimator().estimate(b"jpeg")

    assert result.method == "unavailable"
    assert result.score == 0.0


def test_media_local_pins_legacy_mediapipe_solutions_version() -> None:
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert "mediapipe==0.10.21" in config["project"]["optional-dependencies"]["media-local"]


def test_identity_model_path_is_lazily_assembled_or_clearly_degraded(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("naobot.media.backends.find_spec", lambda _name: object())
    missing = tmp_path / "missing.onnx"

    degraded = MediaService._build_default_providers(
        Settings(identity_model_path=str(missing))
    )

    assert degraded.health["identity"].configured is False

    model_path = tmp_path / "identity.onnx"
    model_path.write_bytes(b"placeholder")
    configured = MediaService._build_default_providers(
        Settings(identity_model_path=str(model_path))
    )

    assert configured.health["identity"].configured is True
    assert isinstance(configured.identity, OpenCVMediaPipeIdentityFacade)


def test_missing_local_wake_dependency_is_safely_disabled(monkeypatch) -> None:
    monkeypatch.setattr("naobot.media.backends.find_spec", lambda _name: None)

    providers = MediaService._build_default_providers(
        Settings(wake_model_path="wake.onnx")
    )

    assert providers.health["wake_word"].configured is False


@pytest.mark.asyncio
async def test_local_sherpa_tts_adapter_is_callable_with_injected_engine() -> None:
    engine = FakeSherpaEngine()
    adapter = SherpaOnnxTTS(engine=engine)

    result = await adapter.synthesize("你好")

    assert result == TTSResult(audio=b"\x01\x00\xff\xff\x02\x00", media_type="audio/pcm")
    assert engine.calls == ["你好"]


@pytest.mark.asyncio
async def test_openai_asr_raises_media_backend_error_on_missing_text_field() -> None:
    response = httpx.Response(200, json={"unexpected": "value"})
    adapter = OpenAICompatibleASR(
        endpoint="https://api.example.com/v1",
        model="asr-1",
        client=FakeAsyncClient(response),
    )

    with pytest.raises(MediaBackendError, match="text"):
        await adapter.transcribe(
            [MediaFrame.audio_pcm16(b"\x00\x00", timestamp_ms=1, sequence=1, flags=1)]
        )


@pytest.mark.asyncio
async def test_openai_tts_raises_media_backend_error_on_json_error_payload() -> None:
    response = httpx.Response(
        200,
        json={"error": {"message": "bad tts"}},
        headers={"content-type": "application/json"},
    )
    adapter = OpenAICompatibleTTS(
        endpoint="https://api.example.com/v1",
        model="tts-1",
        client=FakeAsyncClient(response),
    )

    with pytest.raises(MediaBackendError, match="bad tts"):
        await adapter.synthesize("你好")


@pytest.mark.asyncio
async def test_openai_vision_provider_raises_media_backend_error_on_bad_response() -> None:
    response = httpx.Response(200, json={"choices": []})
    adapter = OpenAICompatibleVisionProvider(
        endpoint="https://api.example.com/v1",
        model="vision-1",
        client=FakeAsyncClient(response),
    )

    with pytest.raises(MediaBackendError, match="choices"):
        await adapter.summarize([MediaFrame.jpeg(b"jpeg", timestamp_ms=1, sequence=1)])
