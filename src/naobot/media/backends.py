from __future__ import annotations

import base64
import importlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
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


class TTSProvider(Protocol):
    async def synthesize(self, text: str) -> TTSResult: ...


class WakeWordProvider(Protocol):
    def detect(self, audio_frames: Sequence[MediaFrame]) -> WakeWordResult | Mapping[str, Any]: ...


class IdentityProvider(Protocol):
    def identify(self, video_frames: Sequence[MediaFrame]) -> IdentityResult: ...


def _require_optional_dependency(module_name: str, feature_name: str) -> None:
    try:
        importlib.import_module(module_name)
    except ImportError as exc:
        raise RuntimeError(
            f"{feature_name} 不可用，请安装 `naobot[media-local]` 后再使用。"
        ) from exc


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
    ) -> None:
        if model is None and model_factory is None:
            _require_optional_dependency("openwakeword", "openwakeword")
        self._model = model
        self._model_factory = model_factory
        self.threshold = threshold
        self.wakeword_name = wakeword_name

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
        self._model = module.Model()
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
    ) -> None:
        self._jpeg_decoder = jpeg_decoder
        self._face_detector = face_detector
        self._embedder = embedder
        self._identity_matcher = identity_matcher
        self._eye_contact_estimator = eye_contact_estimator
        if self._jpeg_decoder is None or self._face_detector is None:
            _require_optional_dependency("mediapipe", "mediapipe")
            _require_optional_dependency("cv2", "opencv-contrib-python-headless")

    def identify(self, video_frames: Sequence[MediaFrame]) -> IdentityResult:
        if not video_frames:
            return IdentityResult()
        decoder = self._jpeg_decoder or self._default_jpeg_decoder
        detector = self._face_detector or self._default_face_detector
        best_person_id: str | None = None
        best_score = float("-inf")
        eye_contact = False
        total_faces = 0

        for frame in video_frames[-3:]:
            image = decoder(frame.payload)
            faces = list(detector(image) or [])
            total_faces += len(faces)
            if len(faces) == 1 and self._eye_contact_estimator is None:
                eye_contact = True
            for face in faces:
                if self._eye_contact_estimator is not None:
                    eye_contact = self._eye_contact_estimator(face) or eye_contact
                if self._embedder is None or self._identity_matcher is None:
                    continue
                embedding = self._embedder(face)
                match = self._identity_matcher(embedding)
                if match is None:
                    continue
                person_id, score = match
                if person_id is not None and score > best_score:
                    best_person_id = person_id
                    best_score = score

        if total_faces == 0:
            summary = "未检测到人脸"
        elif total_faces == 1:
            summary = "检测到单人"
        else:
            summary = f"检测到 {total_faces} 张人脸"
        return IdentityResult(
            person_id=best_person_id,
            eye_contact_ms=1_500 if eye_contact else 0,
            vision_summary=summary,
        )

    @staticmethod
    def _default_jpeg_decoder(payload: bytes) -> Any:
        np = _load_numpy()
        cv2 = importlib.import_module("cv2")
        image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise MediaBackendError("JPEG 解码失败。")
        return image

    @staticmethod
    def _default_face_detector(image: Any) -> Sequence[Any]:
        cv2 = importlib.import_module("cv2")
        mediapipe = importlib.import_module("mediapipe")
        detector = mediapipe.solutions.face_detection.FaceDetection(
            model_selection=0,
            min_detection_confidence=0.5,
        )
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        result = detector.process(rgb)
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
