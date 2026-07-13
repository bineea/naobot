from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
from dataclasses import dataclass, field
from typing import Any, Protocol

from fastapi import WebSocket, WebSocketDisconnect

from ..interaction.orchestrator import CompletedTurn, InteractionOrchestrator
from ..interaction.session import InteractionSession
from ..models import Envelope, MessageType, new_id, now_ms
from ..runtime.persistence import FaceDataRepository, RuntimePersistence
from ..runtime.registry import RuntimeRegistry
from ..settings import Settings
from .backends import (
    ASRProvider,
    ASRResult,
    IdentityProvider,
    IdentityResult,
    MediaBackendError,
    TTSProvider,
    TTSResult,
    VisionProvider,
    VisionResult,
    WakeWordProvider,
    WakeWordResult,
)
from .pipeline import MediaPipeline
from .protocol import MediaFrame

MEDIA_FLAG_SPEECH = 0x1
MEDIA_FLAG_END_OF_UTTERANCE = 0x2
MEDIA_FLAG_EVENT_BOOST = 0x4


class EmbeddingIdentityProvider(Protocol):
    def create_embedding(self, video_frames: list[MediaFrame]) -> list[float]: ...


@dataclass(slots=True)
class ProviderHealth:
    name: str
    configured: bool
    mode: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "mode": self.mode,
        }


@dataclass(slots=True)
class MediaProviders:
    wake_word: WakeWordProvider
    identity: IdentityProvider
    asr: ASRProvider
    vision: VisionProvider
    tts: TTSProvider
    health: dict[str, ProviderHealth]


@dataclass(slots=True)
class PendingEnrollment:
    session_id: str
    expires_at_ms: int
    awaiting_touch: bool = False
    recent_video_frames: list[MediaFrame] = field(default_factory=list)


class EnrollmentManager:
    def __init__(
        self,
        *,
        settings: Settings,
        identity: IdentityProvider,
        persistence: RuntimePersistence,
        repository: FaceDataRepository | None = None,
        confirm_window_ms: int = 10_000,
    ) -> None:
        self.settings = settings
        self.identity = identity
        self.persistence = persistence
        self.repository = repository or FaceDataRepository(settings, persistence=persistence)
        self.confirm_window_ms = confirm_window_ms
        self._pending: PendingEnrollment | None = None
        self._last_result: dict[str, Any] = {"state": "idle"}

    async def observe_turn(
        self,
        turn: CompletedTurn,
        *,
        single_person: bool,
        recent_video_frames: list[MediaFrame],
        now_ms: int,
    ) -> dict[str, Any] | None:
        self._expire(now_ms)
        transcript = str(turn.event.payload.get("transcript") or "").strip()
        session_id = turn.event.session_id
        if not transcript:
            return None
        if self._is_enrollment_request(transcript):
            if not (self.settings.data_key or os.getenv("NAOBOT_DATA_KEY")):
                return self._set_result("rejected", reason="data key 未配置")
            if not single_person:
                return self._set_result("rejected", reason="仅支持单人注册")
            if len(recent_video_frames) < 5:
                return self._set_result("rejected", reason="最近人脸帧不足 5 张")
            self._pending = PendingEnrollment(
                session_id=session_id,
                expires_at_ms=now_ms + self.confirm_window_ms,
                recent_video_frames=list(recent_video_frames[-5:]),
            )
            return self._set_result("pending", session_id=session_id)
        if (
            self._pending is not None
            and self._pending.session_id == session_id
            and self._is_confirmation(transcript)
        ):
            self._pending.awaiting_touch = True
            self._pending.recent_video_frames = list(recent_video_frames[-5:])
            return self._set_result("awaiting_touch", session_id=session_id)
        return None

    async def observe_touch(
        self,
        *,
        session_id: str,
        now_ms: int,
        recent_video_frames: list[MediaFrame],
    ) -> dict[str, Any] | None:
        self._expire(now_ms)
        if self._pending is None or self._pending.session_id != session_id:
            return None
        if not self._pending.awaiting_touch:
            return None
        frames = list(recent_video_frames[-5:] or self._pending.recent_video_frames[-5:])
        if len(frames) < 5:
            return self._set_result("rejected", reason="最近人脸帧不足 5 张")
        create_embedding = getattr(self.identity, "create_embedding", None)
        if create_embedding is None:
            return self._set_result("rejected", reason="identity provider 不支持 embedding")
        embedding = create_embedding(frames)
        person_id = new_id("person")
        await self.persistence.upsert_person(person_id, metadata={"source": "enrollment"})
        await self.repository.upsert_embedding(person_id, embedding, model_name="identity")
        for frame in frames:
            await self.repository.add_sample(
                person_id,
                frame.payload,
                media_type="image/jpeg",
                sha256=hashlib.sha256(frame.payload).hexdigest(),
            )
        self._pending = None
        return self._set_result("completed", person_id=person_id)

    async def cancel(self) -> dict[str, Any]:
        self._pending = None
        result = {"state": "cancelled", "status": "cancelled"}
        self._last_result = {"state": "idle", "status": "idle"}
        return result

    def status(self) -> dict[str, Any]:
        status = dict(self._last_result)
        if self._pending is not None:
            status.setdefault("expires_at_ms", self._pending.expires_at_ms)
        return status

    def _expire(self, now_ms: int) -> None:
        if self._pending is None:
            return
        if now_ms <= self._pending.expires_at_ms:
            return
        self._pending = None
        self._last_result = {"state": "idle"}

    def _set_result(self, status: str, **extra: Any) -> dict[str, Any]:
        self._last_result = {"state": status, "status": status, **extra}
        return dict(self._last_result)

    @staticmethod
    def _is_enrollment_request(transcript: str) -> bool:
        return "记住我" in transcript or "认识我" in transcript

    @staticmethod
    def _is_confirmation(transcript: str) -> bool:
        return transcript == "确认" or transcript.endswith("确认")


class NullWakeWordProvider:
    def detect(self, _audio_frames) -> WakeWordResult:
        return WakeWordResult(triggered=False)


class NullIdentityProvider:
    def identify(self, video_frames) -> IdentityResult:
        summary = "检测到单人" if len(video_frames[-3:]) == 1 else "未检测到稳定单人"
        return IdentityResult(person_id=None, eye_contact_ms=0, vision_summary=summary)


class NullASRProvider:
    async def transcribe(self, _audio_frames) -> ASRResult:
        raise MediaBackendError("ASR capability 未配置。")


class LocalVisionSummaryProvider:
    def __init__(self, identity: IdentityProvider) -> None:
        self.identity = identity

    async def summarize(self, video_frames) -> VisionResult:
        identity = self.identity.identify(video_frames)
        return VisionResult(summary=identity.vision_summary or "检测到单人")


class NullTTSProvider:
    async def synthesize(self, _text: str) -> TTSResult:
        raise MediaBackendError("TTS capability 未配置。")


class MediaHub:
    def __init__(self) -> None:
        self.websocket: WebSocket | None = None
        self._send_lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.websocket = websocket

    def disconnect(self, websocket: WebSocket) -> None:
        if self.websocket is websocket:
            self.websocket = None

    async def send_json(self, payload: dict[str, Any]) -> bool:
        async with self._send_lock:
            if self.websocket is None:
                return False
            try:
                await self.websocket.send_json(payload)
            except (RuntimeError, WebSocketDisconnect):
                self.websocket = None
                return False
            return True

    async def send_binary(self, payload: bytes) -> bool:
        async with self._send_lock:
            if self.websocket is None:
                return False
            try:
                await self.websocket.send_bytes(payload)
            except (RuntimeError, WebSocketDisconnect):
                self.websocket = None
                return False
            return True


class MediaService:
    def __init__(
        self,
        *,
        settings: Settings,
        agent,
        session: InteractionSession | None = None,
        wake_word: WakeWordProvider | None = None,
        identity: IdentityProvider | None = None,
        asr: ASRProvider | None = None,
        vision: VisionProvider | None = None,
        tts: TTSProvider | None = None,
        pipeline: MediaPipeline | None = None,
        runtime_registry: RuntimeRegistry | None = None,
        persistence: RuntimePersistence | None = None,
        enrollment_manager: EnrollmentManager | None = None,
        provider_health: dict[str, ProviderHealth] | None = None,
    ) -> None:
        self.settings = settings
        self.agent = agent
        self.session = session or InteractionSession(
            session_idle_ms=settings.session_idle_ms,
            tts_resume_delay_ms=settings.tts_resume_delay_ms,
        )
        self.pipeline = pipeline or MediaPipeline(
            video_window_ms=settings.media_video_window_ms,
            audio_window_ms=settings.media_audio_window_ms,
            video_queue_limit=settings.media_video_queue_limit,
            audio_queue_limit=settings.media_audio_queue_limit,
        )
        self.wake_word = wake_word or NullWakeWordProvider()
        self.identity = identity or NullIdentityProvider()
        self.asr = asr or NullASRProvider()
        self.vision = vision or LocalVisionSummaryProvider(self.identity)
        self.tts = tts or NullTTSProvider()
        self.runtime_registry = runtime_registry or getattr(
            agent,
            "runtime_registry",
            RuntimeRegistry(settings),
        )
        self.persistence = persistence or getattr(
            self.runtime_registry,
            "persistence",
            RuntimePersistence(settings),
        )
        self.enrollment = enrollment_manager or EnrollmentManager(
            settings=settings,
            identity=self.identity,
            persistence=self.persistence,
        )
        self.orchestrator = InteractionOrchestrator(
            settings=settings,
            pipeline=self.pipeline,
            session=self.session,
            wake_word=self.wake_word,
            identity=self.identity,
            asr=self.asr,
            vision=self.vision,
            tts=self.tts,
        )
        self.provider_health = provider_health or {
            "wake_word": ProviderHealth("wake_word", True, "local"),
            "identity": ProviderHealth("identity", True, "local"),
            "asr": ProviderHealth("asr", not isinstance(self.asr, NullASRProvider), "cloud"),
            "vision": ProviderHealth("vision", True, "local"),
            "tts": ProviderHealth("tts", not isinstance(self.tts, NullTTSProvider), "cloud"),
        }
        self.hub = MediaHub()
        self.robot_hub = None
        self.dashboard_hub = None
        self._frame_queue: asyncio.Queue[MediaFrame] = asyncio.Queue(
            maxsize=max(1, settings.media_audio_queue_limit + settings.media_video_queue_limit)
        )
        self._worker: asyncio.Task | None = None
        self._audio_turn: list[MediaFrame] = []
        self._connections = 0
        self._current_boot_id: str | None = None

    def attach(self, *, robot_hub, dashboard_hub=None) -> None:
        self.robot_hub = robot_hub
        self.dashboard_hub = dashboard_hub

    async def handle_websocket(self, websocket: WebSocket) -> None:
        await self.hub.connect(websocket)
        self._connections += 1
        self.pipeline.update_connection(True)
        try:
            hello = await self._receive_hello(websocket)
            self._current_boot_id = str(hello.get("boot_id") or "")
            await self.hub.send_json({"kind": "media_ready", "boot_id": self._current_boot_id})
            self._worker = asyncio.create_task(self._frame_worker())
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    raise WebSocketDisconnect(message.get("code", 1000))
                if "bytes" in message:
                    try:
                        frame = MediaFrame.decode(message["bytes"])
                    except ValueError as exc:
                        await self._send_media_error("INVALID_MEDIA_FRAME", str(exc))
                        continue
                    try:
                        self._frame_queue.put_nowait(frame)
                    except asyncio.QueueFull:
                        await self._send_media_error("MEDIA_QUEUE_FULL", "媒体队列已满")
                    continue
                if "text" in message:
                    await self._handle_control_json(message["text"])
        except WebSocketDisconnect:
            pass
        finally:
            if self._worker is not None:
                self._worker.cancel()
                await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
            self.hub.disconnect(websocket)
            self._connections = max(0, self._connections - 1)
            self.pipeline.update_connection(self._connections > 0)

    async def list_people(self) -> list[dict[str, Any]]:
        return await self.persistence.list_people()

    async def list_sessions(self) -> list[dict[str, Any]]:
        return await self.persistence.list_sessions()

    async def reset_person_runtime(self, person_id: str) -> None:
        await self.runtime_registry.reset_person_runtime(person_id)

    async def delete_person(self, person_id: str) -> None:
        await self.runtime_registry.reset_person_runtime(person_id)
        await self.persistence.delete_person(person_id)

    async def cancel_enrollment(self) -> dict[str, Any]:
        return await self.enrollment.cancel()

    def status(self) -> dict[str, Any]:
        stats = self.pipeline.stats()
        configured = [health.configured for health in self.provider_health.values()]
        status = "ok" if all(configured) else "degraded"
        return {
            "connections": self._connections,
            "fps": stats["video_fps"],
            "queue": self._frame_queue.qsize(),
            "dropped": stats["media_dropped"],
            "listening": stats["listening"],
            "speaking": stats["speaking"],
            "current_person": stats["current_person"],
            "current_session": stats["current_session"],
            "session_trigger": stats["session_trigger"],
            "enrollment": self.enrollment.status(),
            "provider_status": {
                "status": status,
                "components": {
                    name: health.as_dict() for name, health in self.provider_health.items()
                },
            },
        }

    async def _receive_hello(self, websocket: WebSocket) -> dict[str, Any]:
        message = await websocket.receive()
        if "text" not in message:
            await websocket.close(code=1008)
            raise WebSocketDisconnect(code=1008)
        try:
            payload = json.loads(message["text"])
        except json.JSONDecodeError as exc:
            await websocket.close(code=1008)
            raise WebSocketDisconnect(code=1008) from exc
        if not isinstance(payload, dict):
            await websocket.close(code=1008)
            raise WebSocketDisconnect(code=1008)
        token = str(payload.get("token") or "")
        if self.settings.device_token is not None and not secrets.compare_digest(
            token,
            self.settings.device_token,
        ):
            await websocket.close(code=1008)
            raise WebSocketDisconnect(code=1008)
        if not payload.get("device_id") or not payload.get("boot_id"):
            await websocket.close(code=1008)
            raise WebSocketDisconnect(code=1008)
        return payload

    async def _handle_control_json(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            await self._send_media_error("INVALID_CONTROL_JSON", "控制消息必须为 JSON")
            return
        if not isinstance(payload, dict):
            await self._send_media_error("INVALID_CONTROL_JSON", "控制消息必须为对象")
            return
        kind = str(payload.get("kind") or "")
        current_ms = now_ms()
        if kind == "touch_head":
            self.orchestrator.observe_touch(now_ms=current_ms, person_id=payload.get("person_id"))
            session_id = self.session.snapshot(now_ms=current_ms).session_id or "visitor-touch"
            action = await self.enrollment.observe_touch(
                session_id=session_id,
                now_ms=current_ms,
                recent_video_frames=self.pipeline.video_window()[-5:],
            )
            if action is not None and action.get("status") == "completed":
                await self.hub.send_json({"kind": "enrollment", **action})
                return
            asyncio.create_task(self._emit_touch_intent(session_id=session_id))
            return
        if kind == "enrollment_cancel":
            await self.cancel_enrollment()
            await self.hub.send_json({"kind": "enrollment", **self.enrollment.status()})
            return
        if kind == "ping":
            await self.hub.send_json({"kind": "pong"})
            return
        await self._send_media_error("INVALID_CONTROL_KIND", f"不支持的控制消息 kind={kind}")

    async def _frame_worker(self) -> None:
        while True:
            frame = await self._frame_queue.get()
            await self._handle_frame(frame)

    async def _handle_frame(self, frame: MediaFrame) -> None:
        current_ms = frame.timestamp_ms or now_ms()
        if frame.kind.name == "JPEG":
            await self.orchestrator.observe_video([frame], now_ms=current_ms)
            return
        if frame.kind.name != "AUDIO_PCM16":
            await self._send_media_error("INVALID_MEDIA_KIND", f"不支持的媒体帧类型 {frame.kind.name}")
            return
        await self.orchestrator.observe_audio([frame], now_ms=current_ms)
        self._audio_turn.append(frame)
        if not frame.is_end_of_utterance:
            return
        turn = await self.orchestrator.complete_turn(
            audio_frames=list(self._audio_turn),
            video_frames=self.pipeline.video_window()[-5:],
            now_ms=current_ms,
        )
        self._audio_turn.clear()
        if turn is None:
            return
        turn.event.priority = 6 if any(item.event_boosted for item in turn.audio_frames) else 3
        enrollment = await self.enrollment.observe_turn(
            turn,
            single_person=turn.single_person,
            recent_video_frames=self.pipeline.video_window()[-5:],
            now_ms=current_ms,
        )
        if enrollment is not None:
            if enrollment["status"] == "pending":
                await self._speak_control_text("请先口头说确认，再摸摸我的头。", current_ms)
            await self.hub.send_json({"kind": "enrollment", **enrollment})
            if enrollment["status"] != "awaiting_touch":
                return
        intent = await self.agent.create_intent(turn.event, media_blocks=turn.vision_blocks)
        if self.robot_hub is not None:
            await self.robot_hub.send_intent(intent)
        await self._speak_intent_text(intent, current_ms)

    async def _emit_touch_intent(self, *, session_id: str) -> None:
        event = Envelope(
            type=MessageType.EVENT,
            session_id=session_id,
            payload={"name": "touch_head"},
        )
        intent = await self.agent.create_intent(event)
        if self.robot_hub is not None:
            await self.robot_hub.send_intent(intent)

    async def _speak_control_text(self, text: str, current_ms: int) -> None:
        if not text:
            return
        try:
            audio = await self.orchestrator.speak_text(text, now_ms=current_ms)
            if audio is None:
                return
            await self.hub.send_json({"kind": "tts_start", "text": text})
            await self.hub.send_binary(
                MediaFrame.tts_pcm16(
                    audio.audio,
                    timestamp_ms=current_ms,
                    sequence=0,
                ).encode()
            )
            await self.hub.send_json({"kind": "tts_end"})
        except MediaBackendError as exc:
            await self._send_media_error("TTS_ERROR", str(exc))
        finally:
            self.orchestrator.finish_tts(now_ms=now_ms())

    async def _speak_intent_text(self, intent: Envelope, current_ms: int) -> None:
        text = str(intent.payload.get("text") or "")
        if not text:
            return
        await self._speak_control_text(text, current_ms)

    async def _send_media_error(self, code: str, message: str) -> None:
        await self.hub.send_json({"kind": "media_error", "code": code, "message": message})
