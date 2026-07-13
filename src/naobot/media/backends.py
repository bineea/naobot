from __future__ import annotations

import base64
import importlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from agentscope.message import Base64Source, DataBlock

from .protocol import MediaFrame


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
        if self._client is not None:
            response = await self._client.post(
                f"{self.endpoint}/audio/transcriptions",
                headers=headers,
                data=data,
                files=files,
            )
        else:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{self.endpoint}/audio/transcriptions",
                    headers=headers,
                    data=data,
                    files=files,
                )
        response.raise_for_status()
        body = response.json()
        return ASRResult(transcript=str(body.get("text", "")), is_final=True)


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
        if self._client is not None:
            response = await self._client.post(
                f"{self.endpoint}/audio/speech",
                headers=headers,
                json=payload,
            )
        else:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{self.endpoint}/audio/speech",
                    headers=headers,
                    json=payload,
                )
        response.raise_for_status()
        return TTSResult(audio=response.content)


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


def _require_optional_dependency(module_name: str, feature_name: str) -> None:
    try:
        importlib.import_module(module_name)
    except ImportError as exc:
        raise RuntimeError(
            f"{feature_name} 不可用，请安装 `naobot[media-local]` 后再使用。"
        ) from exc


class FasterWhisperASR:
    def __init__(self, *, model_name: str = "base") -> None:
        _require_optional_dependency("faster_whisper", "faster-whisper")
        self.model_name = model_name

    async def transcribe(self, audio_frames: Sequence[MediaFrame]) -> ASRResult:
        raise RuntimeError("faster-whisper 适配器已加载依赖，但仍需项目侧提供本地模型装配。")


class OpenWakeWordDetector:
    def __init__(self) -> None:
        _require_optional_dependency("openwakeword", "openwakeword")

    def detect(self, audio_frames: Sequence[MediaFrame]) -> WakeWordResult:
        raise RuntimeError("openwakeword 适配器已加载依赖，但仍需项目侧提供本地模型装配。")


class OpenCVMediaPipeIdentityFacade:
    def __init__(self) -> None:
        _require_optional_dependency("mediapipe", "mediapipe")
        _require_optional_dependency("cv2", "opencv-contrib-python-headless")

    def identify(self, video_frames: Sequence[MediaFrame]) -> IdentityResult:
        raise RuntimeError("mediapipe 视觉身份适配器已加载依赖，但仍需项目侧提供本地模型装配。")


class SherpaOnnxTTS:
    def __init__(self) -> None:
        _require_optional_dependency("sherpa_onnx", "sherpa-onnx")

    async def synthesize(self, text: str) -> TTSResult:
        raise RuntimeError("sherpa-onnx TTS 适配器已加载依赖，但仍需项目侧提供本地模型装配。")
