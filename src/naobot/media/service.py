from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import secrets
from collections.abc import Awaitable, Callable
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
    CompositeWakeWordDetector,
    CosineIdentityMatcher,
    FasterWhisperASR,
    IdentityProvider,
    IdentityResult,
    LocalPhraseWakeWordDetector,
    MediaBackendError,
    OnnxFaceEmbedder,
    OpenAICompatibleASR,
    OpenAICompatibleTTS,
    OpenAICompatibleVisionProvider,
    OpenCVMediaPipeIdentityFacade,
    OpenWakeWordDetector,
    PCM16VoiceActivityDetector,
    SherpaOnnxTTS,
    TTSProvider,
    TTSResult,
    VisionProvider,
    VisionResult,
    WakeWordProvider,
    WakeWordResult,
)
from .buffers import MediaIngressQueue
from .pipeline import MediaPipeline
from .protocol import MediaFrame

MEDIA_FLAG_SPEECH = 0x1
MEDIA_FLAG_END_OF_UTTERANCE = 0x2
MEDIA_FLAG_EVENT_BOOST = 0x4
TTS_CHUNK_BYTES = 8_192
PCM16_BYTES_PER_SECOND = 32_000
MEDIA_TURN_QUEUE_CAPACITY = 4


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


@dataclass(slots=True)
class MediaTurnSnapshot:
    audio_frames: list[MediaFrame]
    video_frames: list[MediaFrame]
    now_ms: int
    session_id: str
    person_id: str | None
    trigger: str | None


class EnrollmentManager:
    def __init__(
        self,
        *,
        settings: Settings,
        identity: IdentityProvider,
        persistence: RuntimePersistence,
        repository: FaceDataRepository | None = None,
        confirm_window_ms: int = 10_000,
        on_identity_changed: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.settings = settings
        self.identity = identity
        self.persistence = persistence
        self.repository = repository or FaceDataRepository(settings, persistence=persistence)
        self.confirm_window_ms = confirm_window_ms
        self._on_identity_changed = on_identity_changed
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
            if turn.event.payload.get("person_id"):
                return self._set_result("rejected", reason="only unknown person can enroll")
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
        try:
            embedding = await asyncio.to_thread(create_embedding, frames)
        except (MediaBackendError, ValueError) as exc:
            self._pending = None
            return self._set_result("rejected", reason=str(exc))
        person_id = new_id("person")
        await self.persistence.enroll_person_atomic(
            person_id=person_id,
            embedding=embedding,
            model_name="identity",
            metadata={"source": "enrollment"},
            samples=[
                {
                    "sample_bytes": frame.payload,
                    "media_type": "image/jpeg",
                    "sha256": hashlib.sha256(frame.payload).hexdigest(),
                }
                for frame in frames
            ],
        )
        if self._on_identity_changed is not None:
            await self._on_identity_changed()
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
                return False
            return True

    async def send_binary(self, payload: bytes) -> bool:
        async with self._send_lock:
            if self.websocket is None:
                return False
            try:
                await self.websocket.send_bytes(payload)
            except (RuntimeError, WebSocketDisconnect):
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
        clock: Callable[[], int] | None = None,
        vad: PCM16VoiceActivityDetector | None = None,
    ) -> None:
        self.settings = settings
        self.agent = agent
        self._clock = clock or now_ms
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
        self.vad = vad
        if self.vad is None and settings.local_vad_enabled:
            self.vad = PCM16VoiceActivityDetector(
                rms_threshold=settings.vad_rms_threshold,
                end_silence_ms=settings.vad_end_silence_ms,
            )
        if all(provider is None for provider in (wake_word, identity, asr, vision, tts)):
            providers = self._build_default_providers(settings)
            self.wake_word = providers.wake_word
            self.identity = providers.identity
            self.asr = providers.asr
            self.vision = providers.vision
            self.tts = providers.tts
            resolved_health = providers.health
        else:
            self.wake_word = wake_word or NullWakeWordProvider()
            self.identity = identity or NullIdentityProvider()
            self.asr = asr or NullASRProvider()
            self.vision = vision or LocalVisionSummaryProvider(self.identity)
            self.tts = tts or NullTTSProvider()
            resolved_health = provider_health or {
                "wake_word": ProviderHealth("wake_word", True, "local"),
                "identity": ProviderHealth("identity", True, "local"),
                "asr": ProviderHealth(
                    "asr",
                    not isinstance(self.asr, NullASRProvider),
                    "cloud",
                ),
                "vision": ProviderHealth("vision", True, "local"),
                "tts": ProviderHealth(
                    "tts",
                    not isinstance(self.tts, NullTTSProvider),
                    "cloud",
                ),
            }
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
        self.face_repository = FaceDataRepository(settings, persistence=self.persistence)
        self._identity_cache_loaded = False
        self._identity_cache_lock = asyncio.Lock()
        self.enrollment = enrollment_manager or EnrollmentManager(
            settings=settings,
            identity=self.identity,
            persistence=self.persistence,
            repository=self.face_repository,
            on_identity_changed=self._refresh_identity_cache,
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
        self.provider_health = resolved_health
        self.hub = MediaHub()
        self.robot_hub = None
        self.dashboard_hub = None
        self._frame_queue = MediaIngressQueue(
            maxsize=max(1, settings.media_audio_queue_limit + settings.media_video_queue_limit)
        )
        self._turn_queue: asyncio.Queue[MediaTurnSnapshot] = asyncio.Queue(
            maxsize=MEDIA_TURN_QUEUE_CAPACITY
        )
        self._worker: asyncio.Task | None = None
        self._frame_worker_task: asyncio.Task | None = None
        self._turn_worker_task: asyncio.Task | None = None
        self._audio_turn: list[MediaFrame] = []
        self._connections = 0
        self._current_boot_id: str | None = None
        self._connection_lock = asyncio.Lock()
        initial_snapshot = self.session.snapshot(now_ms=self._clock())
        self._tracked_session_id = initial_snapshot.session_id if initial_snapshot.active else None

    def attach(self, *, robot_hub, dashboard_hub=None) -> None:
        self.robot_hub = robot_hub
        self.dashboard_hub = dashboard_hub

    async def handle_websocket(self, websocket: WebSocket) -> None:
        await websocket.accept()
        frame_worker: asyncio.Task | None = None
        turn_worker: asyncio.Task | None = None
        connection_tasks: set[asyncio.Task] = set()
        registered = False
        try:
            hello = await self._receive_hello(websocket)
            async with self._connection_lock:
                if self.hub.websocket is not None:
                    await websocket.close(code=1013)
                    return
                await self.hub.connect(websocket)
                self._connections += 1
                self.pipeline.update_connection(True)
                registered = True
            self._current_boot_id = str(hello.get("boot_id") or "")
            self.pipeline.reset_stream()
            self._audio_turn.clear()
            self._frame_queue.clear()
            self._clear_turn_queue()
            await self.hub.send_json({"kind": "media_ready", "boot_id": self._current_boot_id})
            frame_worker = asyncio.create_task(self._frame_worker(), name="media-frame-worker")
            turn_worker = asyncio.create_task(self._turn_worker(), name="media-turn-worker")
            self._worker = frame_worker
            self._frame_worker_task = frame_worker
            self._turn_worker_task = turn_worker
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
                    if not self._frame_queue.put_nowait(frame):
                        await self._send_media_error(
                            "MEDIA_QUEUE_FULL",
                            "媒体入口队列已满",
                        )
                    continue
                if "text" in message:
                    await self._handle_control_json(
                        message["text"],
                        connection_tasks=connection_tasks,
                        owner_websocket=websocket,
                    )
        except WebSocketDisconnect:
            pass
        finally:
            if registered:
                async with self._connection_lock:
                    self.hub.disconnect(websocket)
                    self._connections = max(0, self._connections - 1)
                    self.pipeline.update_connection(self._connections > 0)
                workers = [
                    worker for worker in (frame_worker, turn_worker) if worker is not None
                ]
                pending_tasks = [*workers, *connection_tasks]
                for task in pending_tasks:
                    task.cancel()
                if pending_tasks:
                    await asyncio.gather(*pending_tasks, return_exceptions=True)
                if self._worker is frame_worker:
                    self._worker = None
                if self._frame_worker_task is frame_worker:
                    self._frame_worker_task = None
                if self._turn_worker_task is turn_worker:
                    self._turn_worker_task = None
                self._frame_queue.clear()
                self._clear_turn_queue()
                self._audio_turn.clear()
                await self._destroy_tracked_guest_runtime()

    async def list_people(self) -> list[dict[str, Any]]:
        return await self.persistence.list_people()

    async def list_sessions(self) -> list[dict[str, Any]]:
        return await self.persistence.list_sessions()

    async def reset_person_runtime(self, person_id: str) -> None:
        await self.runtime_registry.reset_person_runtime(person_id)

    async def delete_person(self, person_id: str) -> None:
        await self.runtime_registry.reset_person_runtime(person_id)
        await self.persistence.delete_person(person_id)
        await self._refresh_identity_cache()

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
            "dropped": self._frame_queue.dropped,
            "listening": stats["listening"],
            "speaking": stats["speaking"],
            "current_person": stats["current_person"],
            "current_session": stats["current_session"],
            "session_trigger": stats["session_trigger"],
            "last_temporal_summary": (
                dict(self.orchestrator.last_temporal_summary)
                if self.orchestrator.last_temporal_summary is not None
                else None
            ),
            "enrollment": self.enrollment.status(),
            "provider_status": {
                "status": status,
                "components": {
                    name: health.as_dict() for name, health in self.provider_health.items()
                },
            },
        }

    async def _receive_hello(self, websocket: WebSocket) -> dict[str, Any]:
        try:
            message = await asyncio.wait_for(
                websocket.receive(),
                timeout=self.settings.media_hello_timeout_seconds,
            )
        except TimeoutError as exc:
            await websocket.close(code=1008)
            raise WebSocketDisconnect(code=1008) from exc
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

    async def route_touch_event(self, *, name: str, person_id: str | None = None) -> bool:
        """控制 WS 触摸事件桥接到媒体注册/会话。

        与 _handle_control_json 的 touch_head 分支对称，但不调用 _emit_touch_intent——
        控制 WS 路径的 intent 由 server.ws_kt2 的 event_worker 产生。返回 True 表示
        被注册流程消费（caller 应跳过 intent 创建），False 表示应正常产 intent。
        """
        if name not in ("touch_head", "touch_back"):
            return False
        current_ms = self._clock()
        self.orchestrator.observe_touch(now_ms=current_ms, person_id=person_id)
        await self._sync_guest_runtime(current_ms)
        if name == "touch_back":
            return False  # touch_back 无注册语义，激活会话后仍产 intent
        session_id = self.session.snapshot(now_ms=current_ms).session_id or "visitor-touch"
        action = await self.enrollment.observe_touch(
            session_id=session_id,
            now_ms=current_ms,
            recent_video_frames=self.pipeline.video_window()[-5:],
        )
        if action is not None and action.get("status") == "completed":
            await self.hub.send_json({"kind": "enrollment", **action})
            return True  # 注册完成，消费触摸
        return False  # 无 pending 或注册未完成，正常产 intent

    async def _handle_control_json(
        self,
        raw: str,
        *,
        connection_tasks: set[asyncio.Task] | None = None,
        owner_websocket: WebSocket | None = None,
    ) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            await self._send_media_error("INVALID_CONTROL_JSON", "控制消息必须为 JSON")
            return
        if not isinstance(payload, dict):
            await self._send_media_error("INVALID_CONTROL_JSON", "控制消息必须为对象")
            return
        kind = str(payload.get("kind") or "")
        current_ms = self._clock()
        if kind == "touch_head":
            self.orchestrator.observe_touch(now_ms=current_ms, person_id=payload.get("person_id"))
            await self._sync_guest_runtime(current_ms)
            session_id = self.session.snapshot(now_ms=current_ms).session_id or "visitor-touch"
            action = await self.enrollment.observe_touch(
                session_id=session_id,
                now_ms=current_ms,
                recent_video_frames=self.pipeline.video_window()[-5:],
            )
            if action is not None and action.get("status") == "completed":
                await self.hub.send_json({"kind": "enrollment", **action})
                return
            task = asyncio.create_task(
                self._emit_touch_intent(
                    session_id=session_id,
                    owner_websocket=owner_websocket,
                ),
                name="media-touch-intent",
            )
            if connection_tasks is not None:
                connection_tasks.add(task)
                task.add_done_callback(connection_tasks.discard)
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
            try:
                snapshot = await self._observe_frame(frame)
                if snapshot is not None:
                    try:
                        self._turn_queue.put_nowait(snapshot)
                    except asyncio.QueueFull:
                        await self._send_media_error("MEDIA_TURN_QUEUE_FULL", "对话队列已满")
            except MediaBackendError as exc:
                await self._send_media_error("MEDIA_BACKEND_ERROR", str(exc))
            except Exception as exc:
                await self._send_media_error("MEDIA_WORKER_ERROR", f"{type(exc).__name__}: {exc}")

    async def _turn_worker(self) -> None:
        while True:
            snapshot = await self._turn_queue.get()
            try:
                await self._process_turn(snapshot)
            except MediaBackendError as exc:
                await self._send_media_error("MEDIA_BACKEND_ERROR", str(exc))
            except Exception as exc:
                await self._send_media_error("MEDIA_TURN_WORKER_ERROR", f"{type(exc).__name__}: {exc}")

    async def _handle_frame(self, frame: MediaFrame) -> None:
        snapshot = await self._observe_frame(frame)
        if snapshot is not None:
            await self._process_turn(snapshot)

    async def _observe_frame(self, frame: MediaFrame) -> MediaTurnSnapshot | None:
        current_ms = self._clock()
        await self._sync_guest_runtime(current_ms)
        if frame.kind.name == "JPEG":
            await self._ensure_identity_cache()
            await self.orchestrator.observe_video([frame], now_ms=current_ms)
            await self._sync_guest_runtime(current_ms)
            return None
        if frame.kind.name != "AUDIO_PCM16":
            await self._send_media_error("INVALID_MEDIA_KIND", f"不支持的媒体帧类型 {frame.kind.name}")
            return None
        if self.vad is not None:
            frame = self.vad.annotate(frame)
        await self.orchestrator.observe_audio([frame], now_ms=current_ms)
        await self._sync_guest_runtime(current_ms)
        if frame.is_speech or self._audio_turn:
            self._audio_turn.append(frame)
            self._audio_turn = self._audio_turn[-self.settings.media_audio_queue_limit :]
        if not frame.is_end_of_utterance:
            return None
        audio_turn = list(self._audio_turn)
        self._audio_turn.clear()
        if not audio_turn:
            return None
        session_snapshot = self.session.snapshot(now_ms=current_ms)
        if (
            not session_snapshot.active
            or not session_snapshot.listening
            or session_snapshot.session_id is None
        ):
            return None
        return MediaTurnSnapshot(
            audio_frames=audio_turn,
            video_frames=list(self.pipeline.video_window()[-5:]),
            now_ms=current_ms,
            session_id=session_snapshot.session_id,
            person_id=session_snapshot.person_id,
            trigger=session_snapshot.session_trigger,
        )

    async def _process_turn(self, snapshot: MediaTurnSnapshot) -> None:
        turn = await self.orchestrator.complete_turn(
            audio_frames=snapshot.audio_frames,
            video_frames=snapshot.video_frames,
            now_ms=snapshot.now_ms,
            session_id=snapshot.session_id,
            person_id=snapshot.person_id,
            session_trigger=snapshot.trigger,
        )
        if turn is None:
            return
        turn.event.priority = 6 if any(item.event_boosted for item in turn.audio_frames) else 3
        enrollment = await self.enrollment.observe_turn(
            turn,
            single_person=turn.single_person,
            recent_video_frames=snapshot.video_frames,
            now_ms=snapshot.now_ms,
        )
        if enrollment is not None:
            if enrollment["status"] == "pending":
                await self._speak_control_text("请先口头说确认，再摸摸我的头。", snapshot.now_ms)
            await self.hub.send_json({"kind": "enrollment", **enrollment})
            if enrollment["status"] != "awaiting_touch":
                return
        intent = await self.agent.create_intent(turn.event, media_blocks=turn.vision_blocks)
        if self.robot_hub is not None:
            await self.robot_hub.send_intent(intent)
        await self._speak_intent_text(intent, snapshot.now_ms)

    def _clear_turn_queue(self) -> None:
        while True:
            try:
                self._turn_queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def _ensure_identity_cache(self) -> None:
        if self._identity_cache_loaded:
            return
        async with self._identity_cache_lock:
            if self._identity_cache_loaded:
                return
            await self._refresh_identity_cache()

    async def _refresh_identity_cache(self) -> None:
        refresh = getattr(self.identity, "refresh_embeddings", None)
        if not callable(refresh):
            self._identity_cache_loaded = True
            return
        if not (self.settings.data_key or os.getenv("NAOBOT_DATA_KEY")):
            refresh([])
            self._identity_cache_loaded = True
            return
        embeddings = await self.face_repository.list_embeddings(model_name="identity")
        refresh(embeddings)
        self._identity_cache_loaded = True

    async def _sync_guest_runtime(self, current_ms: int) -> None:
        snapshot = self.session.snapshot(now_ms=current_ms)
        current_session_id = snapshot.session_id if snapshot.active else None
        expired_session_id = self._tracked_session_id
        if (
            expired_session_id is not None
            and expired_session_id != current_session_id
            and expired_session_id.lower().startswith(("visitor", "guest"))
        ):
            await self.runtime_registry.destroy_guest_runtime(expired_session_id)
        self._tracked_session_id = current_session_id

    async def _destroy_tracked_guest_runtime(self) -> None:
        session_id = self._tracked_session_id
        self._tracked_session_id = None
        if session_id is not None and session_id.lower().startswith(("visitor", "guest")):
            await self.runtime_registry.destroy_guest_runtime(session_id)

    async def _emit_touch_intent(
        self,
        *,
        session_id: str,
        owner_websocket: WebSocket | None = None,
    ) -> None:
        event = Envelope(
            type=MessageType.EVENT,
            session_id=session_id,
            payload={"name": "touch_head"},
        )
        intent = await self.agent.create_intent(event)
        if owner_websocket is not None and self.hub.websocket is not owner_websocket:
            return
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
            chunks = [
                audio.audio[offset : offset + TTS_CHUNK_BYTES]
                for offset in range(0, len(audio.audio), TTS_CHUNK_BYTES)
            ]
            for sequence, chunk in enumerate(chunks):
                if sequence:
                    await asyncio.sleep(len(chunks[sequence - 1]) / PCM16_BYTES_PER_SECOND)
                await self.hub.send_binary(
                    MediaFrame.tts_pcm16(
                        chunk,
                        timestamp_ms=current_ms,
                        sequence=sequence,
                    ).encode()
                )
            await self.hub.send_json({"kind": "tts_end"})
        except MediaBackendError as exc:
            await self._send_media_error("TTS_ERROR", str(exc))
        finally:
            self.orchestrator.finish_tts(now_ms=self._clock())

    async def _speak_intent_text(self, intent: Envelope, current_ms: int) -> None:
        text = str(intent.payload.get("text") or "")
        if not text:
            return
        await self._speak_control_text(text, current_ms)

    async def _send_media_error(self, code: str, message: str) -> None:
        await self.hub.send_json({"kind": "media_error", "code": code, "message": message})

    @staticmethod
    def _build_default_providers(settings: Settings) -> MediaProviders:
        wake_providers: list[WakeWordProvider] = []
        if settings.wake_model_path:
            try:
                wake_providers.append(
                    OpenWakeWordDetector(model_path=settings.wake_model_path)
                )
            except RuntimeError:
                pass
        if settings.local_phrase_model:
            try:
                wake_providers.append(
                    LocalPhraseWakeWordDetector(model_name=settings.local_phrase_model)
                )
            except RuntimeError:
                pass
        if not wake_providers:
            wake_word = NullWakeWordProvider()
            wake_health = ProviderHealth("wake_word", False, "local")
        elif len(wake_providers) == 1:
            wake_word = wake_providers[0]
            wake_health = ProviderHealth("wake_word", True, "local")
        else:
            wake_word = CompositeWakeWordDetector(wake_providers)
            wake_health = ProviderHealth("wake_word", True, "local")

        if settings.identity_model_path:
            try:
                identity = OpenCVMediaPipeIdentityFacade(
                    embedder=OnnxFaceEmbedder(settings.identity_model_path),
                    identity_matcher=CosineIdentityMatcher(
                        threshold=settings.identity_match_threshold
                    ),
                    match_interval_ms=settings.identity_match_interval_ms,
                    enrollment_similarity_threshold=(
                        settings.identity_enrollment_similarity_threshold
                    ),
                )
                identity_health = ProviderHealth("identity", True, "local")
            except RuntimeError:
                identity = NullIdentityProvider()
                identity_health = ProviderHealth("identity", False, "local")
        else:
            identity = NullIdentityProvider()
            identity_health = ProviderHealth("identity", False, "local")

        if settings.asr_endpoint and settings.asr_model:
            asr: ASRProvider = OpenAICompatibleASR(
                endpoint=settings.asr_endpoint,
                model=settings.asr_model,
                api_key=settings.asr_api_key,
            )
            asr_health = ProviderHealth("asr", True, "cloud")
        elif settings.asr_model:
            try:
                asr = FasterWhisperASR(model_name=settings.asr_model)
                asr_health = ProviderHealth("asr", True, "local")
            except RuntimeError:
                asr = NullASRProvider()
                asr_health = ProviderHealth("asr", False, "local")
        else:
            asr = NullASRProvider()
            asr_health = ProviderHealth("asr", False, "local")

        if settings.vision_endpoint and settings.vision_model:
            vision: VisionProvider = OpenAICompatibleVisionProvider(
                endpoint=settings.vision_endpoint,
                model=settings.vision_model,
                api_key=settings.vision_api_key,
            )
            vision_health = ProviderHealth("vision", True, "cloud")
        else:
            vision = LocalVisionSummaryProvider(identity)
            vision_health = ProviderHealth(
                "vision",
                not isinstance(identity, NullIdentityProvider),
                "local",
            )

        if settings.tts_endpoint and settings.tts_model:
            tts: TTSProvider = OpenAICompatibleTTS(
                endpoint=settings.tts_endpoint,
                model=settings.tts_model,
                api_key=settings.tts_api_key,
                voice=settings.tts_voice,
            )
            tts_health = ProviderHealth("tts", True, "cloud")
        elif settings.sherpa_onnx_model_path and settings.sherpa_onnx_tokens_path:
            try:
                tts = SherpaOnnxTTS(
                    engine_factory=lambda: MediaService._build_sherpa_engine(settings)
                )
                tts_health = ProviderHealth("tts", True, "local")
            except RuntimeError:
                tts = NullTTSProvider()
                tts_health = ProviderHealth("tts", False, "local")
        else:
            tts = NullTTSProvider()
            tts_health = ProviderHealth("tts", False, "local")

        return MediaProviders(
            wake_word=wake_word,
            identity=identity,
            asr=asr,
            vision=vision,
            tts=tts,
            health={
                "wake_word": wake_health,
                "identity": identity_health,
                "asr": asr_health,
                "vision": vision_health,
                "tts": tts_health,
            },
        )

    @staticmethod
    def _build_sherpa_engine(settings: Settings):
        module = importlib.import_module("sherpa_onnx")
        required = (
            "OfflineTts",
            "OfflineTtsConfig",
            "OfflineTtsModelConfig",
            "OfflineTtsVitsModelConfig",
        )
        if not all(hasattr(module, name) for name in required):
            raise MediaBackendError("Sherpa local TTS 自动装配不可用。")
        vits = module.OfflineTtsVitsModelConfig(
            model=settings.sherpa_onnx_model_path,
            tokens=settings.sherpa_onnx_tokens_path,
            lexicon=settings.sherpa_onnx_lexicon_path or "",
            data_dir=settings.sherpa_onnx_data_dir or "",
        )
        model = module.OfflineTtsModelConfig(
            vits=vits,
            provider="cpu",
            num_threads=settings.sherpa_onnx_num_threads,
            debug=False,
        )
        config = module.OfflineTtsConfig(
            model=model,
            rule_fsts=settings.sherpa_onnx_rule_fsts or "",
            max_num_sentences=1,
        )
        if hasattr(config, "validate") and not config.validate():
            raise MediaBackendError("Sherpa local TTS 配置无效。")
        return module.OfflineTts(config)
