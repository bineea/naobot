from __future__ import annotations

import base64
import importlib
import json
import math
import re
import sys
from array import array
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Protocol

import httpx
from agentscope.message import Base64Source, DataBlock

from .protocol import MediaFrame


class MediaBackendError(RuntimeError):
    pass


@dataclass(slots=True)
class ASRResult:
    transcript: str
    is_final: bool = True


@dataclass(slots=True)
class VisionResult:
    summary: str


@dataclass(slots=True)
class MotionEstimate:
    score: float
    method: str


@dataclass(slots=True)
class TTSResult:
    audio: bytes
    media_type: str = "audio/pcm"


@dataclass(slots=True)
class WakeWordResult:
    triggered: bool = False
    trigger: str | None = None
    greeting_detected: bool = False


@dataclass(slots=True)
class IdentityResult:
    person_id: str | None = None
    eye_contact_ms: int = 0
    greeting_detected: bool = False
    vision_summary: str = ""


class ASRProvider(Protocol):
    async def transcribe(self, audio_frames: Sequence[MediaFrame]) -> ASRResult: ...


class VisionProvider(Protocol):
    async def summarize(self, video_frames: Sequence[MediaFrame]) -> VisionResult: ...


class MotionEstimator(Protocol):
    def estimate(self, jpeg_payload: bytes) -> MotionEstimate: ...


class TTSProvider(Protocol):
    async def synthesize(self, text: str) -> TTSResult: ...


class WakeWordProvider(Protocol):
    def detect(self, audio_frames: Sequence[MediaFrame]) -> WakeWordResult | Mapping[str, Any]: ...


class IdentityProvider(Protocol):
    def identify(self, video_frames: Sequence[MediaFrame]) -> IdentityResult: ...


def _require_optional_dependency(module_name: str, feature_name: str) -> None:
    if find_spec(module_name) is None:
        raise RuntimeError(
            f"{feature_name} 不可用，请安装 `naobot[media-local]` 后再使用。"
        )


def _load_numpy():
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("numpy 不可用，无法处理媒体 PCM/JPEG 数据。") from exc
    return np


def _parse_json_response(response: httpx.Response, backend_name: str) -> dict[str, Any]:
    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise MediaBackendError(f"{backend_name} 返回了无效 JSON。") from exc
    if not isinstance(body, dict):
        raise MediaBackendError(f"{backend_name} JSON 顶层必须是对象。")
    return body


def _coerce_http_error(exc: httpx.HTTPError, backend_name: str) -> MediaBackendError:
    return MediaBackendError(f"{backend_name} 请求失败：{exc}")


def _ensure_success_status(response: httpx.Response, backend_name: str) -> None:
    if response.status_code >= 400:
        raise MediaBackendError(f"{backend_name} 请求失败：HTTP {response.status_code}")


class OpenAICompatibleASR:
    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.api_key = api_key
        self._client = client

    async def transcribe(self, audio_frames: Sequence[MediaFrame]) -> ASRResult:
        payload = b"".join(frame.payload for frame in audio_frames)
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = {"model": self.model}
        files = {"file": ("audio.pcm", payload, "application/octet-stream")}
        try:
            response = await self._post(
                f"{self.endpoint}/audio/transcriptions",
                headers=headers,
                data=data,
                files=files,
            )
        except httpx.HTTPError as exc:
            raise _coerce_http_error(exc, "ASR 后端") from exc
        _ensure_success_status(response, "ASR 后端")

        body = _parse_json_response(response, "ASR 后端")
        transcript = body.get("text")
        if not isinstance(transcript, str):
            raise MediaBackendError("ASR 后端缺少 `text` 字段。")
        return ASRResult(transcript=transcript, is_final=bool(body.get("is_final", True)))

    async def _post(self, url: str, **kwargs: Any) -> httpx.Response:
        if self._client is not None:
            return await self._client.post(url, **kwargs)
        async with httpx.AsyncClient(timeout=30) as client:
            return await client.post(url, **kwargs)


class OpenAICompatibleTTS:
    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str | None = None,
        voice: str = "alloy",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.voice = voice
        self._client = client

    async def synthesize(self, text: str) -> TTSResult:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {"model": self.model, "voice": self.voice, "input": text}
        try:
            response = await self._post(
                f"{self.endpoint}/audio/speech",
                headers=headers,
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise _coerce_http_error(exc, "TTS 后端") from exc
        _ensure_success_status(response, "TTS 后端")

        content_type = response.headers.get("content-type", "audio/pcm")
        if "json" in content_type:
            body = _parse_json_response(response, "TTS 后端")
            message = body.get("error", {}).get("message") if isinstance(body.get("error"), dict) else None
            raise MediaBackendError(
                f"TTS 后端返回了 JSON 错误载荷：{message or body}"
            )
        if not response.content:
            raise MediaBackendError("TTS 后端返回了空音频。")
        return TTSResult(audio=response.content, media_type=content_type.split(";")[0])

    async def _post(self, url: str, **kwargs: Any) -> httpx.Response:
        if self._client is not None:
            return await self._client.post(url, **kwargs)
        async with httpx.AsyncClient(timeout=30) as client:
            return await client.post(url, **kwargs)


class OpenAICompatibleVisionProvider:
    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str | None = None,
        prompt: str = "请用一句话总结画面中与当前对话相关的信息。",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.prompt = prompt
        self._client = client

    async def summarize(self, video_frames: Sequence[MediaFrame]) -> VisionResult:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        content: list[dict[str, Any]] = [{"type": "text", "text": self.prompt}]
        for block in build_vision_input_blocks([frame.payload for frame in video_frames[:3]]):
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{block.source.media_type};base64,{block.source.data}",
                    },
                }
            )
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
        }
        try:
            response = await self._post(
                f"{self.endpoint}/chat/completions",
                headers=headers,
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise _coerce_http_error(exc, "Vision 后端") from exc
        _ensure_success_status(response, "Vision 后端")

        body = _parse_json_response(response, "Vision 后端")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise MediaBackendError("Vision 后端缺少 `choices` 字段。")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            raise MediaBackendError("Vision 后端缺少 `message` 字段。")
        content_value = message.get("content")
        if isinstance(content_value, str):
            return VisionResult(summary=content_value)
        if isinstance(content_value, list):
            texts = [
                item.get("text", "")
                for item in content_value
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            if texts:
                return VisionResult(summary="".join(texts))
        raise MediaBackendError("Vision 后端缺少可解析的 `message.content` 文本。")

    async def _post(self, url: str, **kwargs: Any) -> httpx.Response:
        if self._client is not None:
            return await self._client.post(url, **kwargs)
        async with httpx.AsyncClient(timeout=30) as client:
            return await client.post(url, **kwargs)


def build_vision_input_blocks(jpeg_frames: Sequence[bytes]) -> list[DataBlock]:
    blocks: list[DataBlock] = []
    for index, payload in enumerate(jpeg_frames[:3]):
        blocks.append(
            DataBlock(
                name=f"frame-{index + 1}.jpg",
                source=Base64Source(
                    data=base64.b64encode(payload).decode("ascii"),
                    media_type="image/jpeg",
                ),
            )
        )
    return blocks


def coerce_wake_word_result(result: WakeWordResult | Mapping[str, Any]) -> WakeWordResult:
    if isinstance(result, WakeWordResult):
        return result
    return WakeWordResult(
        triggered=bool(result.get("triggered", False)),
        trigger=str(result.get("trigger") or "") or None,
        greeting_detected=bool(result.get("greeting_detected", False)),
    )


class PCM16VoiceActivityDetector:
    """纯本地 PCM16 能量 VAD；firmware 标志存在时不覆盖。"""

    def __init__(
        self,
        *,
        rms_threshold: float = 500.0,
        sample_rate_hz: int = 16_000,
        end_silence_ms: int = 400,
    ) -> None:
        if rms_threshold < 0:
            raise ValueError("rms_threshold must be non-negative")
        if sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive")
        if end_silence_ms <= 0:
            raise ValueError("end_silence_ms must be positive")
        self.rms_threshold = float(rms_threshold)
        self.sample_rate_hz = sample_rate_hz
        self.end_silence_ms = end_silence_ms
        self._in_speech = False
        self._silence_ms = 0.0

    def annotate(self, frame: MediaFrame) -> MediaFrame:
        firmware_flags = frame.flags & 0x3
        if firmware_flags:
            self._in_speech = frame.is_speech and not frame.is_end_of_utterance
            self._silence_ms = 0.0
            return frame

        samples = self._samples(frame.payload)
        if not samples:
            return frame
        rms = math.sqrt(math.fsum(float(value) ** 2 for value in samples) / len(samples))
        duration_ms = len(samples) * 1_000.0 / self.sample_rate_hz
        if rms >= self.rms_threshold:
            self._in_speech = True
            self._silence_ms = 0.0
            return MediaFrame(
                kind=frame.kind,
                timestamp_ms=frame.timestamp_ms,
                sequence=frame.sequence,
                payload=frame.payload,
                flags=frame.flags | 0x1,
            )
        if not self._in_speech:
            return frame
        self._silence_ms += duration_ms
        if self._silence_ms < self.end_silence_ms:
            return frame
        self._in_speech = False
        self._silence_ms = 0.0
        return MediaFrame(
            kind=frame.kind,
            timestamp_ms=frame.timestamp_ms,
            sequence=frame.sequence,
            payload=frame.payload,
            flags=frame.flags | 0x2,
        )

    @staticmethod
    def _samples(payload: bytes) -> array:
        usable = len(payload) - (len(payload) % 2)
        if usable <= 0:
            return array("h")
        samples = array("h")
        samples.frombytes(payload[:usable])
        if sys.byteorder != "little":
            samples.byteswap()
        return samples


class LocalPhraseWakeWordDetector:
    """在本机转写已结束的短音频，并仅接受明确唤醒词或问候。"""

    _DEFAULT_WAKE_PHRASES = ("naobot", "脑宝", "小龟")
    _DEFAULT_GREETINGS = ("你好", "您好", "嗨", "哈喽", "早上好", "下午好", "晚上好")

    def __init__(
        self,
        *,
        transcriber: Callable[[Sequence[MediaFrame]], str] | None = None,
        model_name: str | None = None,
        model: Any | None = None,
        model_factory: Callable[[], Any] | None = None,
        wake_phrases: Sequence[str] | None = None,
        greetings: Sequence[str] | None = None,
        max_frames: int = 200,
    ) -> None:
        if model_name and transcriber is None and model is None and model_factory is None:
            _require_optional_dependency("faster_whisper", "faster-whisper 本地短语检测")
        self._transcriber = transcriber
        self.model_name = model_name
        self._model = model
        self._model_factory = model_factory
        self.wake_phrases = tuple(wake_phrases or self._DEFAULT_WAKE_PHRASES)
        self.greetings = tuple(greetings or self._DEFAULT_GREETINGS)
        self.max_frames = max(1, max_frames)
        self._audio_buffer: list[MediaFrame] = []

    @property
    def configured(self) -> bool:
        return any(
            value is not None
            for value in (self._transcriber, self.model_name, self._model, self._model_factory)
        )

    def detect(self, audio_frames: Sequence[MediaFrame]) -> WakeWordResult:
        for frame in audio_frames:
            if frame.is_speech or self._audio_buffer:
                self._audio_buffer.append(frame)
                self._audio_buffer = self._audio_buffer[-self.max_frames :]
            if not frame.is_end_of_utterance:
                continue
            buffered = list(self._audio_buffer)
            self._audio_buffer.clear()
            if not self.configured or not buffered:
                return WakeWordResult()
            transcript = self._normalize(self._transcribe(buffered))
            if not transcript:
                return WakeWordResult()
            greeting = any(
                transcript == phrase
                or (transcript.startswith(phrase) and len(transcript) <= len(phrase) + 4)
                for phrase in map(self._normalize, self.greetings)
            )
            if greeting:
                return WakeWordResult(greeting_detected=True, trigger="local_greeting")
            for phrase in map(self._normalize, self.wake_phrases):
                if phrase and phrase in transcript:
                    return WakeWordResult(triggered=True, trigger=f"local_phrase:{phrase}")
            return WakeWordResult()
        return WakeWordResult()

    def _transcribe(self, frames: Sequence[MediaFrame]) -> str:
        if self._transcriber is not None:
            return str(self._transcriber(frames) or "")
        np = _load_numpy()
        pcm = b"".join(frame.payload for frame in frames)
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _ = self._get_model().transcribe(audio)
        return "".join(getattr(segment, "text", str(segment)) for segment in segments).strip()

    def _get_model(self) -> Any:
        if self._model is not None:
            return self._model
        if self._model_factory is not None:
            self._model = self._model_factory()
            return self._model
        if not self.model_name:
            raise MediaBackendError("本地短语模型未配置。")
        module = importlib.import_module("faster_whisper")
        self._model = module.WhisperModel(self.model_name)
        return self._model

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", text).lower()


class CompositeWakeWordDetector:
    def __init__(self, providers: Sequence[WakeWordProvider]) -> None:
        self.providers = tuple(providers)

    def detect(self, audio_frames: Sequence[MediaFrame]) -> WakeWordResult:
        greeting: WakeWordResult | None = None
        for provider in self.providers:
            result = coerce_wake_word_result(provider.detect(audio_frames))
            if result.triggered:
                return result
            if result.greeting_detected:
                greeting = result
        return greeting or WakeWordResult()


class CosineIdentityMatcher:
    def __init__(self, *, threshold: float = 0.78) -> None:
        if not math.isfinite(threshold) or not -1.0 <= threshold <= 1.0:
            raise ValueError("threshold must be between -1 and 1")
        self.threshold = float(threshold)
        self._embeddings: tuple[tuple[str, tuple[float, ...]], ...] = ()

    def replace_embeddings(self, embeddings: Sequence[Mapping[str, Any]]) -> None:
        resolved: list[tuple[str, tuple[float, ...]]] = []
        for item in embeddings:
            person_id = str(item.get("person_id") or "")
            vector = tuple(self._finite_vector(item.get("embedding", ())))
            if person_id and vector:
                resolved.append((person_id, vector))
        self._embeddings = tuple(resolved)

    def __call__(self, embedding: Sequence[float]) -> tuple[str, float] | None:
        candidate = tuple(self._finite_vector(embedding))
        best: tuple[str, float] | None = None
        for person_id, enrolled in self._embeddings:
            score = self._cosine(candidate, enrolled)
            if score is None or (best is not None and score <= best[1]):
                continue
            best = (person_id, score)
        if best is None or best[1] < self.threshold:
            return None
        return best

    @staticmethod
    def _cosine(left: Sequence[float], right: Sequence[float]) -> float | None:
        if not left or len(left) != len(right):
            return None
        left_norm = math.sqrt(math.fsum(value * value for value in left))
        right_norm = math.sqrt(math.fsum(value * value for value in right))
        if left_norm == 0.0 or right_norm == 0.0:
            return None
        return math.fsum(a * b for a, b in zip(left, right, strict=True)) / (
            left_norm * right_norm
        )

    @staticmethod
    def _finite_vector(values: Sequence[Any]) -> list[float]:
        vector = [float(value) for value in values]
        if any(not math.isfinite(value) for value in vector):
            raise ValueError("identity embedding values must be finite")
        return vector


class OnnxFaceEmbedder:
    """惰性创建 ONNX Runtime session 的本地人脸 embedding 适配器。"""

    def __init__(
        self,
        model_path: str,
        *,
        session: Any | None = None,
        session_factory: Callable[[], Any] | None = None,
    ) -> None:
        if session is None and session_factory is None:
            if not Path(model_path).is_file():
                raise RuntimeError(f"ONNX identity model 文件不存在：{model_path}")
            _require_optional_dependency("onnxruntime", "ONNX identity model")
        self.model_path = model_path
        self._session = session
        self._session_factory = session_factory

    def __call__(self, image: Any) -> list[float]:
        np = _load_numpy()
        cv2 = importlib.import_module("cv2")
        session = self._get_session()
        model_input = session.get_inputs()[0]
        shape = list(model_input.shape)
        height = int(shape[-2]) if isinstance(shape[-2], int) else 112
        width = int(shape[-1]) if isinstance(shape[-1], int) else 112
        resized = cv2.resize(image, (width, height))
        if getattr(resized, "ndim", 0) == 2:
            resized = np.stack([resized] * 3, axis=-1)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        tensor = ((rgb.astype(np.float32) - 127.5) / 128.0).transpose(2, 0, 1)[None, ...]
        output = session.run(None, {model_input.name: tensor})[0]
        vector = np.asarray(output, dtype=np.float32).reshape(-1)
        if not bool(np.isfinite(vector).all()):
            raise MediaBackendError("ONNX identity model 返回了 non-finite embedding。")
        norm = float(np.linalg.norm(vector))
        if norm <= 0.0:
            raise MediaBackendError("ONNX identity model 返回了零向量。")
        return (vector / norm).tolist()

    def _get_session(self) -> Any:
        if self._session is not None:
            return self._session
        if self._session_factory is not None:
            self._session = self._session_factory()
            return self._session
        try:
            module = importlib.import_module("onnxruntime")
            self._session = module.InferenceSession(
                self.model_path,
                providers=["CPUExecutionProvider"],
            )
        except (ImportError, OSError, RuntimeError) as exc:
            raise MediaBackendError(f"本地 ONNX identity model 加载失败：{exc}") from exc
        return self._session


class OpenCVMotionEstimator:
    """将 JPEG 解码为低分辨率灰度特征，并对相邻特征计算 MAD。"""

    def __init__(self, *, thumbnail_size: tuple[int, int] = (32, 24)) -> None:
        width, height = thumbnail_size
        if width <= 0 or height <= 0:
            raise ValueError("thumbnail_size must be positive")
        self.thumbnail_size = (width, height)
        self._previous_feature: Any | None = None

    @property
    def retained_feature_shape(self) -> tuple[int, ...] | None:
        shape = getattr(self._previous_feature, "shape", None)
        return tuple(shape) if shape is not None else None

    def estimate(self, jpeg_payload: bytes) -> MotionEstimate:
        try:
            np = _load_numpy()
            cv2 = importlib.import_module("cv2")
        except (ImportError, RuntimeError):
            self._previous_feature = None
            return MotionEstimate(score=0.0, method="unavailable")
        encoded = np.frombuffer(jpeg_payload, dtype=np.uint8)
        image = cv2.imdecode(encoded, cv2.IMREAD_GRAYSCALE)
        if image is None:
            self._previous_feature = None
            return MotionEstimate(score=0.0, method="unavailable")
        feature = cv2.resize(
            image,
            self.thumbnail_size,
            interpolation=cv2.INTER_AREA,
        )
        score = 0.0
        if self._previous_feature is not None:
            difference = cv2.absdiff(self._previous_feature, feature)
            score = float(np.mean(difference)) / 255.0
        self._previous_feature = feature.copy()
        return MotionEstimate(
            score=round(min(max(score, 0.0), 1.0), 4),
            method="opencv_gray_mad",
        )


class FasterWhisperASR:
    def __init__(
        self,
        *,
        model_name: str = "base",
        model: Any | None = None,
        model_factory: Callable[[], Any] | None = None,
    ) -> None:
        if model is None and model_factory is None:
            _require_optional_dependency("faster_whisper", "faster-whisper")
        self.model_name = model_name
        self._model = model
        self._model_factory = model_factory

    async def transcribe(self, audio_frames: Sequence[MediaFrame]) -> ASRResult:
        np = _load_numpy()
        pcm = b"".join(frame.payload for frame in audio_frames)
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _ = self._get_model().transcribe(audio)
        transcript = "".join(getattr(segment, "text", str(segment)) for segment in segments).strip()
        return ASRResult(transcript=transcript, is_final=True)

    def _get_model(self) -> Any:
        if self._model is not None:
            return self._model
        if self._model_factory is not None:
            self._model = self._model_factory()
            return self._model
        module = importlib.import_module("faster_whisper")
        self._model = module.WhisperModel(self.model_name)
        return self._model


class OpenWakeWordDetector:
    def __init__(
        self,
        *,
        model: Any | None = None,
        model_factory: Callable[[], Any] | None = None,
        threshold: float = 0.5,
        wakeword_name: str | None = None,
        model_path: str | None = None,
    ) -> None:
        if model is None and model_factory is None:
            _require_optional_dependency("openwakeword", "openwakeword")
        self._model = model
        self._model_factory = model_factory
        self.threshold = threshold
        self.wakeword_name = wakeword_name
        self.model_path = model_path

    def detect(self, audio_frames: Sequence[MediaFrame]) -> WakeWordResult:
        np = _load_numpy()
        pcm = np.frombuffer(b"".join(frame.payload for frame in audio_frames), dtype=np.int16)
        predictions = self._get_model().predict(pcm)
        if not isinstance(predictions, Mapping) or not predictions:
            return WakeWordResult(triggered=False)
        scores: dict[str, float] = {}
        for name, value in predictions.items():
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                score = float(value[-1]) if value else 0.0
            else:
                score = float(value)
            scores[str(name)] = score
        selected_name = self.wakeword_name or max(scores, key=scores.get)
        score = scores.get(selected_name, 0.0)
        return WakeWordResult(
            triggered=score >= self.threshold,
            trigger=selected_name if score >= self.threshold else None,
        )

    def _get_model(self) -> Any:
        if self._model is not None:
            return self._model
        if self._model_factory is not None:
            self._model = self._model_factory()
            return self._model
        module = importlib.import_module("openwakeword")
        kwargs = {"wakeword_models": [self.model_path]} if self.model_path else {}
        self._model = module.Model(**kwargs)
        return self._model


class OpenCVMediaPipeIdentityFacade:
    def __init__(
        self,
        *,
        jpeg_decoder: Callable[[bytes], Any] | None = None,
        face_detector: Callable[[Any], Sequence[Any]] | None = None,
        embedder: Callable[[Any], Any] | None = None,
        identity_matcher: Callable[[Any], tuple[str | None, float] | None] | None = None,
        eye_contact_estimator: Callable[[Any], bool] | None = None,
        match_interval_ms: int = 1_000,
        enrollment_similarity_threshold: float = 0.8,
    ) -> None:
        self._jpeg_decoder = jpeg_decoder
        self._face_detector = face_detector
        self._embedder = embedder
        self._identity_matcher = identity_matcher
        self._eye_contact_estimator = eye_contact_estimator
        self.match_interval_ms = max(0, match_interval_ms)
        if (
            not math.isfinite(enrollment_similarity_threshold)
            or not -1.0 <= enrollment_similarity_threshold <= 1.0
        ):
            raise ValueError("enrollment_similarity_threshold must be between -1 and 1")
        self.enrollment_similarity_threshold = float(enrollment_similarity_threshold)
        self._last_match_at_ms: int | None = None
        self._default_detector: Any | None = None
        if self._jpeg_decoder is None or self._face_detector is None:
            _require_optional_dependency("mediapipe", "mediapipe")
            _require_optional_dependency("cv2", "opencv-contrib-python-headless")

    def identify(self, video_frames: Sequence[MediaFrame]) -> IdentityResult:
        if not video_frames:
            return IdentityResult()
        decoder = self._jpeg_decoder or self._default_jpeg_decoder
        detector = self._face_detector or self._default_face_detector
        newest_timestamp_ms = video_frames[-1].timestamp_ms
        should_match = self._should_match(newest_timestamp_ms)
        best_person_id: str | None = None
        best_score = float("-inf")
        eye_contact = False
        max_faces = 0

        for frame in video_frames[-3:]:
            image = decoder(frame.payload)
            faces = list(detector(image) or [])
            max_faces = max(max_faces, len(faces))
            if len(faces) == 1 and self._eye_contact_estimator is None:
                eye_contact = True
            for face in faces:
                if self._eye_contact_estimator is not None:
                    eye_contact = self._eye_contact_estimator(face) or eye_contact
                if (
                    not should_match
                    or len(faces) != 1
                    or self._embedder is None
                    or self._identity_matcher is None
                ):
                    continue
                embedding = self._embedder(self._embedding_input(image, face))
                match = self._identity_matcher(embedding)
                should_match = False
                self._last_match_at_ms = newest_timestamp_ms
                if match is None:
                    continue
                person_id, score = match
                if person_id is not None and score > best_score:
                    best_person_id = person_id
                    best_score = score

        if max_faces == 0:
            summary = "未检测到人脸"
            best_person_id = None
        elif max_faces == 1:
            summary = "检测到单人"
        else:
            summary = f"检测到 {max_faces} 张人脸"
            best_person_id = None
        return IdentityResult(
            person_id=best_person_id,
            eye_contact_ms=1_500 if eye_contact else 0,
            vision_summary=summary,
        )

    def create_embedding(self, video_frames: Sequence[MediaFrame]) -> list[float]:
        if len(video_frames) != 5:
            raise ValueError("identity enrollment requires exactly 5 frames")
        if self._embedder is None:
            raise MediaBackendError("identity embedder 未配置。")
        decoder = self._jpeg_decoder or self._default_jpeg_decoder
        detector = self._face_detector or self._default_face_detector
        vectors: list[list[float]] = []
        for frame in video_frames:
            image = decoder(frame.payload)
            faces = list(detector(image) or [])
            if len(faces) != 1:
                raise MediaBackendError("五帧注册要求每帧恰好检测到一张人脸。")
            try:
                vector = CosineIdentityMatcher._finite_vector(
                    self._embedder(self._embedding_input(image, faces[0]))
                )
            except ValueError as exc:
                raise MediaBackendError(str(exc)) from exc
            if vectors and len(vector) != len(vectors[0]):
                raise MediaBackendError("identity embedding 维度不一致。")
            vector_norm = math.sqrt(math.fsum(value * value for value in vector))
            if vector_norm == 0.0:
                raise MediaBackendError("identity embedding 为零向量。")
            vectors.append([value / vector_norm for value in vector])
        average = [
            math.fsum(vector[index] for vector in vectors) / len(vectors)
            for index in range(len(vectors[0]))
        ]
        norm = math.sqrt(math.fsum(value * value for value in average))
        if norm == 0.0:
            raise MediaBackendError("identity embedding 为零向量。")
        center = [value / norm for value in average]
        similarities = [
            CosineIdentityMatcher._cosine(vector, center) for vector in vectors
        ]
        similarities.extend(
            CosineIdentityMatcher._cosine(left, right)
            for index, left in enumerate(vectors)
            for right in vectors[index + 1 :]
        )
        if any(
            score is None or score < self.enrollment_similarity_threshold
            for score in similarities
        ):
            raise MediaBackendError("五帧注册未通过同一人一致性校验。")
        return center

    def refresh_embeddings(self, embeddings: Sequence[Mapping[str, Any]]) -> None:
        replace_embeddings = getattr(self._identity_matcher, "replace_embeddings", None)
        if callable(replace_embeddings):
            replace_embeddings(embeddings)
        self._last_match_at_ms = None

    def _should_match(self, timestamp_ms: int) -> bool:
        return (
            self._last_match_at_ms is None
            or timestamp_ms < self._last_match_at_ms
            or timestamp_ms - self._last_match_at_ms >= self.match_interval_ms
        )

    @staticmethod
    def _embedding_input(image: Any, face: Any) -> Any:
        if isinstance(face, Mapping) and "embedding_input" in face:
            return face["embedding_input"]
        bounding_box = getattr(getattr(face, "location_data", None), "relative_bounding_box", None)
        shape = getattr(image, "shape", None)
        if bounding_box is None or shape is None or len(shape) < 2:
            return image
        height, width = shape[:2]
        x1 = max(0, int(bounding_box.xmin * width))
        y1 = max(0, int(bounding_box.ymin * height))
        x2 = min(width, int((bounding_box.xmin + bounding_box.width) * width))
        y2 = min(height, int((bounding_box.ymin + bounding_box.height) * height))
        if x2 <= x1 or y2 <= y1:
            return image
        return image[y1:y2, x1:x2]

    @staticmethod
    def _default_jpeg_decoder(payload: bytes) -> Any:
        np = _load_numpy()
        cv2 = importlib.import_module("cv2")
        image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise MediaBackendError("JPEG 解码失败。")
        return image

    def _default_face_detector(self, image: Any) -> Sequence[Any]:
        cv2 = importlib.import_module("cv2")
        mediapipe = importlib.import_module("mediapipe")
        if self._default_detector is None:
            self._default_detector = mediapipe.solutions.face_detection.FaceDetection(
                model_selection=0,
                min_detection_confidence=0.5,
            )
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        result = self._default_detector.process(rgb)
        return list(result.detections or [])


class SherpaOnnxTTS:
    def __init__(
        self,
        *,
        engine: Any | None = None,
        engine_factory: Callable[[], Any] | None = None,
    ) -> None:
        if engine is None and engine_factory is None:
            _require_optional_dependency("sherpa_onnx", "sherpa-onnx")
        self._engine = engine
        self._engine_factory = engine_factory

    async def synthesize(self, text: str) -> TTSResult:
        np = _load_numpy()
        engine = self._get_engine()
        if hasattr(engine, "generate"):
            generated = engine.generate(text)
        elif hasattr(engine, "synthesize"):
            generated = engine.synthesize(text)
        else:
            raise MediaBackendError("SherpaOnnxTTS engine 缺少 generate/synthesize 接口。")

        if isinstance(generated, bytes):
            return TTSResult(audio=generated)
        if isinstance(generated, Mapping):
            if isinstance(generated.get("audio"), bytes):
                return TTSResult(audio=generated["audio"])
            samples = generated.get("samples")
            if samples is not None:
                return TTSResult(audio=np.asarray(samples, dtype=np.int16).tobytes())
        samples = getattr(generated, "samples", None)
        if samples is not None:
            return TTSResult(audio=np.asarray(samples, dtype=np.int16).tobytes())
        raise MediaBackendError("SherpaOnnxTTS 返回了无法识别的音频结构。")

    def _get_engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        if self._engine_factory is not None:
            self._engine = self._engine_factory()
            return self._engine
        raise MediaBackendError("SherpaOnnxTTS 需要注入 engine 或 engine_factory。")
