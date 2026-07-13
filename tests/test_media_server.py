from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from agentscope.state import AgentState
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from naobot.agent import NaobotAgent
from naobot.brain import AgentScopeBrainRuntime
from naobot.interaction.session import InteractionSession
from naobot.llm import RuleBasedLLMClient
from naobot.media.backends import (
    ASRResult,
    IdentityResult,
    LocalPhraseWakeWordDetector,
    TTSResult,
    VisionResult,
    WakeWordResult,
)
from naobot.media.buffers import MediaIngressQueue
from naobot.media.protocol import MediaFrame, MediaFrameKind, MediaHello
from naobot.media.service import MediaHub, MediaService
from naobot.models import Envelope, MessageType
from naobot.runtime.registry import RuntimeRegistry
from naobot.server import create_app
from naobot.settings import Settings


class SlowFriendlyLLM(RuleBasedLLMClient):
    def __init__(self) -> None:
        self.media_blocks_seen = None

    async def decide(self, event, soul, memories, media_blocks=None):
        self.media_blocks_seen = media_blocks
        await asyncio.sleep(0.05)
        return await super().decide(event, soul, memories)


class FakeWakeWord:
    def __init__(self) -> None:
        self.calls = 0

    def detect(self, _frames):
        self.calls += 1
        return WakeWordResult(triggered=True, trigger="naobot")


class FakeIdentity:
    def __init__(self) -> None:
        self.calls = 0

    def identify(self, _frames):
        self.calls += 1
        return IdentityResult(
            person_id="person-7",
            eye_contact_ms=1_500,
            vision_summary="检测到单人并看到用户挥手",
        )


class UnknownIdentity(FakeIdentity):
    def identify(self, _frames):
        self.calls += 1
        return IdentityResult(
            person_id=None,
            eye_contact_ms=1_500,
            vision_summary="检测到单人",
        )

    def create_embedding(self, video_frames):
        return [0.1, 0.2, 0.3]


class FakeASR:
    def __init__(self) -> None:
        self.calls = 0
        self.fail_once = False
        self.frame_counts = []

    async def transcribe(self, frames):
        self.calls += 1
        self.frame_counts.append(len(frames))
        if self.fail_once:
            self.fail_once = False
            from naobot.media.backends import MediaBackendError

            raise MediaBackendError("asr failed once")
        return ASRResult(transcript="你好呀", is_final=True)


class FakeVision:
    async def summarize(self, _frames):
        return VisionResult(summary="用户拿着杯子")


class FakeTTS:
    def __init__(self) -> None:
        self.calls = []

    async def synthesize(self, text: str):
        self.calls.append(text)
        return TTSResult(audio=b"\x01\x00\x02\x00", media_type="audio/pcm")


class BytesTTS:
    def __init__(self, audio: bytes) -> None:
        self.audio = audio

    async def synthesize(self, _text: str):
        return TTSResult(audio=self.audio, media_type="audio/pcm")


class SequenceASR:
    def __init__(self, transcripts):
        self.transcripts = list(transcripts)

    async def transcribe(self, _frames):
        transcript = self.transcripts.pop(0)
        return ASRResult(transcript=transcript, is_final=True)


class StepClock:
    def __init__(self, current_ms: int) -> None:
        self.current_ms = current_ms

    def set(self, current_ms: int) -> None:
        self.current_ms = current_ms

    def __call__(self) -> int:
        return self.current_ms


class QuietWakeWord:
    def detect(self, _frames):
        return WakeWordResult()


class PassiveIdentity:
    def identify(self, _frames):
        return IdentityResult(person_id=None, eye_contact_ms=0, vision_summary="未检测到人脸")


class CountingVision(FakeVision):
    def __init__(self) -> None:
        self.calls = 0

    async def summarize(self, frames):
        self.calls += 1
        return await super().summarize(frames)


class CountingAgent:
    def __init__(self) -> None:
        self.calls = 0

    async def create_intent(self, event, media_blocks=None):
        self.calls += 1
        return Envelope(
            type=MessageType.INTENT,
            session_id=event.session_id,
            payload={"text": ""},
        )


class RecordingAgent(CountingAgent):
    def __init__(self) -> None:
        super().__init__()
        self.events = []

    async def create_intent(self, event, media_blocks=None):
        self.events.append(event)
        return await super().create_intent(event, media_blocks=media_blocks)


class BlockingAgent(CountingAgent):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()

    async def create_intent(self, event, media_blocks=None):
        self.calls += 1
        if self.calls > 1:
            return Envelope(
                type=MessageType.INTENT,
                session_id=event.session_id,
                payload={"text": ""},
            )
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return Envelope(
            type=MessageType.INTENT,
            session_id=event.session_id,
            payload={"text": ""},
        )


class SlowTouchRuntimeAgent:
    def __init__(self, registry: RuntimeRegistry) -> None:
        self.registry = registry
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.finished = asyncio.Event()
        self.session_id = None

    async def create_intent(self, event, media_blocks=None):
        self.session_id = event.session_id
        self.started.set()
        try:
            await self.release.wait()
            await self.registry.load_state(event.session_id, "primary", is_guest=True)
            return Envelope(
                type=MessageType.INTENT,
                session_id=event.session_id,
                payload={"text": ""},
            )
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        finally:
            self.finished.set()


class GuestRuntimeStreamingAgent:
    def __init__(self, state=None) -> None:
        self.state = state or AgentState()

    async def reply_stream(self, _inputs):
        yield SimpleNamespace(
            type="TEXT_BLOCK_DELTA",
            delta='{"text":"你好","goal":"回应访客","confidence":1.0,"skills":[]}',
        )


async def create_real_guest_runtime(settings, registry, visitor_id):
    brain = AgentScopeBrainRuntime(
        settings,
        agent_factory=lambda _prompt, **kwargs: GuestRuntimeStreamingAgent(kwargs.get("state")),
        runtime_registry=registry,
    )
    agent = NaobotAgent(settings, llm=brain)
    await agent.create_intent(
        Envelope(
            type=MessageType.EVENT,
            session_id=visitor_id,
            payload={"name": "user_utterance", "transcript": "你好", "person_id": None},
        )
    )
    return agent


def make_media_client(tmp_path, *, settings: Settings | None = None):
    settings = settings or Settings(runtime_dir=tmp_path, host_heartbeat_interval_ms=40)
    llm = SlowFriendlyLLM()
    agent = NaobotAgent(settings, llm=llm)
    asr = FakeASR()
    media_service = MediaService(
        settings=settings,
        agent=agent,
        session=InteractionSession(tts_resume_delay_ms=settings.tts_resume_delay_ms),
        wake_word=FakeWakeWord(),
        identity=FakeIdentity(),
        asr=asr,
        vision=FakeVision(),
        tts=FakeTTS(),
    )
    return TestClient(create_app(settings, agent, media_service=media_service)), agent, media_service, llm, asr


class ControlledMediaWebSocket:
    def __init__(self, hello: dict | None = None) -> None:
        self.incoming = asyncio.Queue()
        if hello is not None:
            self.incoming.put_nowait({"text": json.dumps(hello)})
        self.accepted = asyncio.Event()
        self.outcome = asyncio.Event()
        self.sent_json = []
        self.close_codes = []

    async def accept(self) -> None:
        self.accepted.set()

    async def receive(self):
        return await self.incoming.get()

    async def send_json(self, payload) -> None:
        self.sent_json.append(payload)
        if payload.get("kind") == "media_ready":
            self.outcome.set()

    async def send_bytes(self, _payload) -> None:
        return None

    async def close(self, code: int) -> None:
        self.close_codes.append(code)
        self.outcome.set()

    def disconnect(self) -> None:
        self.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})


def valid_media_hello(device_id: str, *, token: str = "") -> dict:
    return {
        "device_id": device_id,
        "token": token,
        "boot_id": f"boot-{device_id}",
        "capabilities": MediaHello(
            device_id=device_id,
            token=token,
            boot_id=f"boot-{device_id}",
        ).capabilities,
    }


def receive_until_type(websocket, message_type: str, max_messages: int = 20):
    for _ in range(max_messages):
        message = websocket.receive_json()
        if message["type"] == message_type:
            return message
    raise AssertionError(f"未收到 type={message_type} 的消息")


def receive_until_kind(websocket, kind: str, max_messages: int = 20):
    for _ in range(max_messages):
        raw = websocket.receive()
        if "text" not in raw:
            continue
        import json

        message = json.loads(raw["text"])
        if message.get("kind") == kind:
            return message
    raise AssertionError(f"未收到 kind={kind} 的消息")


def test_media_websocket_rejects_invalid_token_with_1008(tmp_path) -> None:
    settings = Settings(
        runtime_dir=tmp_path,
        device_token="expected-token",
        host_heartbeat_interval_ms=40,
    )
    client, _agent, _service, _llm, _asr = make_media_client(tmp_path, settings=settings)

    with client.websocket_connect("/ws/media") as websocket:
        websocket.send_json(
            {
                "device_id": "device-1",
                "token": "wrong-token",
                "boot_id": "boot-1",
                "capabilities": MediaHello(
                    device_id="device-1",
                    token="wrong-token",
                    boot_id="boot-1",
                ).capabilities,
            }
        )
        with pytest.raises(WebSocketDisconnect) as exc:
            websocket.receive_json()

    assert exc.value.code == 1008


@pytest.mark.asyncio
async def test_silent_unauthenticated_connection_does_not_occupy_authenticated_slot(
    tmp_path,
) -> None:
    settings = Settings(runtime_dir=tmp_path, media_hello_timeout_seconds=0.1)
    _client, _agent, service, _llm, _asr = make_media_client(tmp_path, settings=settings)
    silent = ControlledMediaWebSocket()
    authenticated = ControlledMediaWebSocket(valid_media_hello("authenticated"))

    silent_task = asyncio.create_task(service.handle_websocket(silent))  # type: ignore[arg-type]
    await silent.accepted.wait()

    assert service.status()["connections"] == 0
    assert service.hub.websocket is None

    authenticated_task = asyncio.create_task(
        service.handle_websocket(authenticated)  # type: ignore[arg-type]
    )
    await asyncio.wait_for(authenticated.outcome.wait(), timeout=0.5)

    assert authenticated.sent_json[0]["kind"] == "media_ready"
    assert service.hub.websocket is authenticated
    assert service.status()["connections"] == 1

    await asyncio.wait_for(silent_task, timeout=0.5)
    assert silent.close_codes == [1008]
    assert service.hub.websocket is authenticated
    assert service.status()["connections"] == 1

    authenticated.disconnect()
    await authenticated_task


@pytest.mark.asyncio
async def test_media_hello_timeout_closes_with_1008(tmp_path) -> None:
    settings = Settings(runtime_dir=tmp_path, media_hello_timeout_seconds=0.01)
    _client, _agent, service, _llm, _asr = make_media_client(tmp_path, settings=settings)
    silent = ControlledMediaWebSocket()

    await asyncio.wait_for(
        service.handle_websocket(silent),  # type: ignore[arg-type]
        timeout=0.5,
    )

    assert silent.close_codes == [1008]
    assert service.hub.websocket is None
    assert service.status()["connections"] == 0


@pytest.mark.asyncio
async def test_invalid_token_does_not_disconnect_authenticated_connection(tmp_path) -> None:
    settings = Settings(
        runtime_dir=tmp_path,
        device_token="expected-token",
        media_hello_timeout_seconds=0.1,
    )
    _client, _agent, service, _llm, _asr = make_media_client(tmp_path, settings=settings)
    authenticated = ControlledMediaWebSocket(
        valid_media_hello("authenticated", token="expected-token")
    )
    rejected = ControlledMediaWebSocket(valid_media_hello("rejected", token="wrong-token"))

    authenticated_task = asyncio.create_task(
        service.handle_websocket(authenticated)  # type: ignore[arg-type]
    )
    await asyncio.wait_for(authenticated.outcome.wait(), timeout=0.5)
    await service.handle_websocket(rejected)  # type: ignore[arg-type]

    assert rejected.close_codes == [1008]
    assert service.hub.websocket is authenticated
    assert service.status()["connections"] == 1

    authenticated.disconnect()
    await authenticated_task


@pytest.mark.asyncio
async def test_concurrent_authenticated_connections_allow_exactly_one(tmp_path) -> None:
    settings = Settings(runtime_dir=tmp_path, media_hello_timeout_seconds=0.1)
    _client, _agent, service, _llm, _asr = make_media_client(tmp_path, settings=settings)
    sockets = [
        ControlledMediaWebSocket(valid_media_hello("device-1")),
        ControlledMediaWebSocket(valid_media_hello("device-2")),
    ]

    tasks = [
        asyncio.create_task(service.handle_websocket(socket))  # type: ignore[arg-type]
        for socket in sockets
    ]
    await asyncio.gather(
        *(asyncio.wait_for(socket.outcome.wait(), timeout=0.5) for socket in sockets)
    )

    accepted = [socket for socket in sockets if socket.sent_json]
    rejected = [socket for socket in sockets if socket.close_codes]
    assert len(accepted) == 1
    assert len(rejected) == 1
    assert rejected[0].close_codes == [1013]
    assert service.hub.websocket is accepted[0]
    assert service.status()["connections"] == 1

    accepted[0].disconnect()
    await asyncio.gather(*tasks)


def test_media_status_reports_ingress_queue_depth_and_drop_breakdown(tmp_path) -> None:
    settings = Settings(
        runtime_dir=tmp_path,
        media_video_queue_limit=1,
        media_audio_queue_limit=1,
    )
    _client, _agent, service, _llm, _asr = make_media_client(tmp_path, settings=settings)

    assert isinstance(service._frame_queue, MediaIngressQueue)
    assert service._frame_queue.put_nowait(
        MediaFrame.audio_pcm16(b"speech", timestamp_ms=1, sequence=1, flags=1)
    )
    assert service._frame_queue.put_nowait(
        MediaFrame.audio_pcm16(b"eou", timestamp_ms=2, sequence=2, flags=2)
    )
    assert not service._frame_queue.put_nowait(
        MediaFrame.jpeg(b"jpeg", timestamp_ms=3, sequence=3)
    )

    assert service.status()["queue"] == 2
    assert service.status()["dropped"] == {
        "total": 1,
        "by_kind": {"JPEG": 1},
        "by_reason": {"queue_full_protected": 1},
    }


@pytest.mark.asyncio
async def test_media_ingress_queue_full_sends_protocol_error(tmp_path) -> None:
    settings = Settings(
        runtime_dir=tmp_path,
        media_video_queue_limit=1,
        media_audio_queue_limit=1,
    )
    _client, _agent, service, _llm, _asr = make_media_client(tmp_path, settings=settings)
    release_worker = asyncio.Event()

    async def blocked_frame_worker() -> None:
        await release_worker.wait()

    service._frame_worker = blocked_frame_worker  # type: ignore[method-assign]
    websocket = ControlledMediaWebSocket(valid_media_hello("queue-full"))
    handler = asyncio.create_task(
        service.handle_websocket(websocket)  # type: ignore[arg-type]
    )
    await asyncio.wait_for(websocket.outcome.wait(), timeout=0.5)

    try:
        for frame in (
            MediaFrame.audio_pcm16(b"speech", timestamp_ms=1, sequence=1, flags=1),
            MediaFrame.audio_pcm16(b"eou", timestamp_ms=2, sequence=2, flags=2),
            MediaFrame.jpeg(b"rejected", timestamp_ms=3, sequence=3),
        ):
            websocket.incoming.put_nowait(
                {"type": "websocket.receive", "bytes": frame.encode()}
            )

        async def ingress_rejected_frame() -> None:
            while service.status()["dropped"]["total"] == 0:
                await asyncio.sleep(0)

        await asyncio.wait_for(ingress_rejected_frame(), timeout=0.5)

        assert {
            "kind": "media_error",
            "code": "MEDIA_QUEUE_FULL",
            "message": "媒体入口队列已满",
        } in websocket.sent_json
    finally:
        release_worker.set()
        websocket.disconnect()
        await asyncio.gather(handler, return_exceptions=True)


@pytest.mark.asyncio
async def test_queued_turn_keeps_person_binding_when_session_switches_under_backpressure(
    tmp_path,
) -> None:
    session = InteractionSession()
    session.activate_from_touch(now_ms=1_000, person_id="person-a")
    agent = RecordingAgent()
    service = MediaService(
        settings=Settings(runtime_dir=tmp_path, local_vad_enabled=False),
        agent=agent,
        session=session,
        wake_word=QuietWakeWord(),
        identity=PassiveIdentity(),
        asr=FakeASR(),
        vision=FakeVision(),
        tts=FakeTTS(),
        clock=StepClock(1_001),
    )

    queued_turn = await service._observe_frame(
        MediaFrame.audio_pcm16(b"person-a", timestamp_ms=1, sequence=1, flags=3)
    )
    assert queued_turn is not None
    service._turn_queue.put_nowait(queued_turn)

    assert session.switch_person(now_ms=1_002, person_id="person-b") is True
    await service._process_turn(service._turn_queue.get_nowait())

    assert len(agent.events) == 1
    assert agent.events[0].session_id == "person-a"
    assert agent.events[0].payload["person_id"] == "person-a"
    assert agent.events[0].payload["session_trigger"] == "touch"


@pytest.mark.asyncio
async def test_slow_agent_does_not_block_ingress_and_disconnect_cleans_workers(tmp_path) -> None:
    settings = Settings(
        runtime_dir=tmp_path,
        media_video_queue_limit=4,
        media_audio_queue_limit=8,
        local_vad_enabled=False,
    )
    agent = BlockingAgent()
    service = MediaService(
        settings=settings,
        agent=agent,
        wake_word=FakeWakeWord(),
        identity=FakeIdentity(),
        asr=FakeASR(),
        vision=FakeVision(),
        tts=FakeTTS(),
    )
    websocket = ControlledMediaWebSocket(valid_media_hello("slow-agent"))
    handler = asyncio.create_task(
        service.handle_websocket(websocket)  # type: ignore[arg-type]
    )
    await asyncio.wait_for(websocket.outcome.wait(), timeout=0.5)

    try:
        websocket.incoming.put_nowait(
            {
                "type": "websocket.receive",
                "bytes": MediaFrame.audio_pcm16(
                    b"wake", timestamp_ms=1, sequence=1, flags=1
                ).encode(),
            }
        )
        websocket.incoming.put_nowait(
            {
                "type": "websocket.receive",
                "bytes": MediaFrame.audio_pcm16(
                    b"eou", timestamp_ms=2, sequence=2, flags=3
                ).encode(),
            }
        )
        await asyncio.wait_for(agent.started.wait(), timeout=0.5)

        websocket.incoming.put_nowait(
            {
                "type": "websocket.receive",
                "bytes": MediaFrame.jpeg(
                    b"new-jpeg", timestamp_ms=3, sequence=3
                ).encode(),
            }
        )
        websocket.incoming.put_nowait(
            {
                "type": "websocket.receive",
                "bytes": MediaFrame.audio_pcm16(
                    b"speech", timestamp_ms=4, sequence=4, flags=1
                ).encode(),
            }
        )
        websocket.incoming.put_nowait(
            {
                "type": "websocket.receive",
                "bytes": MediaFrame.audio_pcm16(
                    b"eou-2", timestamp_ms=5, sequence=5, flags=3
                ).encode(),
            }
        )

        async def second_turn_was_observed() -> None:
            while True:
                turn_queue = getattr(service, "_turn_queue", None)
                if (
                    service.pipeline.video_window()
                    and service.pipeline.video_window()[-1].sequence == 3
                    and turn_queue is not None
                    and turn_queue.qsize() == 1
                ):
                    return
                await asyncio.sleep(0.01)

        await asyncio.wait_for(second_turn_was_observed(), timeout=0.5)

        websocket.disconnect()
        await asyncio.wait_for(handler, timeout=0.5)

        assert agent.cancelled.is_set()
        assert getattr(service, "_frame_worker_task", None) is None
        assert getattr(service, "_turn_worker_task", None) is None
        assert service._frame_queue.empty()
        assert service._turn_queue.empty()
        assert service._audio_turn == []
    finally:
        if not handler.done():
            agent.release.set()
            websocket.disconnect()
            await asyncio.gather(handler, return_exceptions=True)


@pytest.mark.asyncio
async def test_disconnect_cancels_and_waits_for_slow_touch_intent(tmp_path) -> None:
    class RecordingRobotHub:
        def __init__(self) -> None:
            self.intents = []

        async def send_intent(self, intent) -> None:
            self.intents.append(intent)

    registry = RuntimeRegistry(Settings(runtime_dir=tmp_path))
    agent = SlowTouchRuntimeAgent(registry)
    robot_hub = RecordingRobotHub()
    service = MediaService(
        settings=Settings(runtime_dir=tmp_path),
        agent=agent,
        wake_word=QuietWakeWord(),
        identity=PassiveIdentity(),
        asr=FakeASR(),
        vision=FakeVision(),
        tts=FakeTTS(),
        runtime_registry=registry,
    )
    service.attach(robot_hub=robot_hub)
    websocket = ControlledMediaWebSocket(valid_media_hello("slow-touch"))
    handler = asyncio.create_task(
        service.handle_websocket(websocket)  # type: ignore[arg-type]
    )
    await asyncio.wait_for(websocket.outcome.wait(), timeout=0.5)

    try:
        websocket.incoming.put_nowait(
            {
                "type": "websocket.receive",
                "text": json.dumps({"kind": "touch_head"}),
            }
        )
        await asyncio.wait_for(agent.started.wait(), timeout=0.5)

        websocket.disconnect()
        await asyncio.wait_for(handler, timeout=0.5)

        assert agent.cancelled.is_set()
        agent.release.set()
        await asyncio.sleep(0)
        assert registry.loaded_count() == 0
        assert robot_hub.intents == []
    finally:
        agent.release.set()
        if not handler.done():
            websocket.disconnect()
            await asyncio.gather(handler, return_exceptions=True)
        await asyncio.wait_for(agent.finished.wait(), timeout=0.5)
        if agent.session_id is not None:
            await registry.destroy_guest_runtime(agent.session_id)


def test_media_turn_streams_tts_and_forwards_intent(tmp_path) -> None:
    client, agent, _service, llm, _asr = make_media_client(tmp_path)

    with client.websocket_connect("/ws/kt2") as kt2, client.websocket_connect("/ws/media") as media:
        assert kt2.receive_json()["type"] == "heartbeat"
        media.send_json(
            {
                "device_id": "device-1",
                "token": "",
                "boot_id": "boot-1",
                "capabilities": MediaHello(device_id="device-1", token="", boot_id="boot-1").capabilities,
            }
        )
        assert media.receive_json()["kind"] == "media_ready"

        media.send_bytes(MediaFrame.jpeg(b"jpeg-1", timestamp_ms=100, sequence=1).encode())
        media.send_bytes(MediaFrame.jpeg(b"jpeg-2", timestamp_ms=120, sequence=2).encode())
        media.send_bytes(MediaFrame.jpeg(b"jpeg-3", timestamp_ms=140, sequence=3).encode())
        media.send_bytes(MediaFrame.audio_pcm16(b"wake", timestamp_ms=150, sequence=4, flags=1).encode())
        media.send_bytes(
            MediaFrame.audio_pcm16(b"speech", timestamp_ms=200, sequence=5, flags=3).encode()
        )

        intent = None
        intent = receive_until_type(kt2, "intent")
        assert intent["payload"]["text"]

        tts_start = media.receive_json()
        binary = media.receive_bytes()
        tts_end = media.receive_json()
        tts_frame = MediaFrame.decode(binary)

    assert tts_start["text"]
    assert tts_end["kind"] == "tts_end"
    assert tts_frame.kind == MediaFrameKind.TTS_PCM16
    assert llm.media_blocks_seen is not None
    assert "jpeg-1" not in str(agent.logs)


@pytest.mark.asyncio
async def test_large_tts_is_chunked_sequenced_and_paced(tmp_path, monkeypatch) -> None:
    settings = Settings(runtime_dir=tmp_path)
    session = InteractionSession()
    session.activate_from_touch(now_ms=1_000, person_id=None)
    service = MediaService(
        settings=settings,
        agent=CountingAgent(),
        session=session,
        wake_word=QuietWakeWord(),
        identity=PassiveIdentity(),
        asr=FakeASR(),
        vision=FakeVision(),
        tts=BytesTTS(b"x" * (70 * 1024)),
        clock=StepClock(1_000),
    )
    sent = []
    sleeps = []

    async def capture_json(payload):
        sent.append(("json", payload))
        return True

    async def capture_binary(payload):
        sent.append(("binary", MediaFrame.decode(payload)))
        return True

    async def capture_sleep(delay):
        sleeps.append(delay)

    service.hub.send_json = capture_json  # type: ignore[method-assign]
    service.hub.send_binary = capture_binary  # type: ignore[method-assign]
    monkeypatch.setattr("naobot.media.service.asyncio.sleep", capture_sleep)

    await service._speak_control_text("长语音", 1_000)

    frames = [payload for kind, payload in sent if kind == "binary"]
    assert sent[0] == ("json", {"kind": "tts_start", "text": "长语音"})
    assert sent[-1] == ("json", {"kind": "tts_end"})
    assert len(frames) == 9
    assert all(frame.kind == MediaFrameKind.TTS_PCM16 for frame in frames)
    assert all(len(frame.payload) <= 8_192 for frame in frames)
    assert [frame.sequence for frame in frames] == list(range(len(frames)))
    assert sleeps == [pytest.approx(8_192 / 32_000)] * (len(frames) - 1)


@pytest.mark.asyncio
async def test_empty_tts_still_sends_start_then_end(tmp_path) -> None:
    session = InteractionSession()
    session.activate_from_touch(now_ms=1_000, person_id=None)
    service = MediaService(
        settings=Settings(runtime_dir=tmp_path),
        agent=CountingAgent(),
        session=session,
        wake_word=QuietWakeWord(),
        identity=PassiveIdentity(),
        asr=FakeASR(),
        vision=FakeVision(),
        tts=BytesTTS(b""),
        clock=StepClock(1_000),
    )
    sent_json = []
    sent_binary = []

    async def capture_json(payload):
        sent_json.append(payload)
        return True

    async def capture_binary(payload):
        sent_binary.append(payload)
        return True

    service.hub.send_json = capture_json  # type: ignore[method-assign]
    service.hub.send_binary = capture_binary  # type: ignore[method-assign]

    await service._speak_control_text("空语音", 1_000)

    assert sent_json == [
        {"kind": "tts_start", "text": "空语音"},
        {"kind": "tts_end"},
    ]
    assert sent_binary == []


def test_media_flood_does_not_block_kt2_heartbeat_and_bad_frame_only_returns_media_error(tmp_path) -> None:
    client, _agent, _service, _llm, _asr = make_media_client(tmp_path)

    with client.websocket_connect("/ws/kt2") as kt2, client.websocket_connect("/ws/media") as media:
        assert kt2.receive_json()["type"] == "heartbeat"
        media.send_json(
            {
                "device_id": "device-1",
                "token": "",
                "boot_id": "boot-1",
                "capabilities": MediaHello(device_id="device-1", token="", boot_id="boot-1").capabilities,
            }
        )
        assert media.receive_json()["kind"] == "media_ready"

        media.send_bytes(b"not-a-valid-frame")
        error = media.receive_json()
        for sequence in range(1, 40):
            media.send_bytes(
                MediaFrame.jpeg(b"x", timestamp_ms=sequence * 10, sequence=sequence).encode()
            )

        messages = [kt2.receive_json() for _ in range(4)]

    assert error["code"] == "INVALID_MEDIA_FRAME"
    assert any(message["type"] == "heartbeat" for message in messages)


def test_media_worker_continues_after_backend_error_and_keeps_next_turn(tmp_path) -> None:
    client, _agent, _service, _llm, asr = make_media_client(tmp_path)
    asr.fail_once = True

    with client.websocket_connect("/ws/kt2") as kt2, client.websocket_connect("/ws/media") as media:
        assert kt2.receive_json()["type"] == "heartbeat"
        media.send_json(
            {
                "device_id": "device-1",
                "token": "",
                "boot_id": "boot-1",
                "capabilities": MediaHello(device_id="device-1", token="", boot_id="boot-1").capabilities,
            }
        )
        assert media.receive_json()["kind"] == "media_ready"

        media.send_bytes(MediaFrame.audio_pcm16(b"wake", timestamp_ms=100, sequence=1, flags=1).encode())
        media.send_bytes(MediaFrame.audio_pcm16(b"speech", timestamp_ms=120, sequence=2, flags=3).encode())
        error = media.receive_json()
        assert error["kind"] == "media_error"

        media.send_bytes(MediaFrame.audio_pcm16(b"wake2", timestamp_ms=130, sequence=3, flags=1).encode())
        media.send_bytes(MediaFrame.audio_pcm16(b"speech2", timestamp_ms=140, sequence=4, flags=3).encode())

        intent = receive_until_type(kt2, "intent", max_messages=30)

    assert asr.calls >= 2
    assert asr.frame_counts == [2, 2]
    assert intent["type"] == "intent"


@pytest.mark.asyncio
async def test_media_send_failure_keeps_connection_owned_until_handler_cleanup() -> None:
    class BrokenWebSocket:
        async def send_json(self, _payload):
            raise RuntimeError("closed")

    hub = MediaHub()
    websocket = BrokenWebSocket()
    hub.websocket = websocket  # type: ignore[assignment]

    assert await hub.send_json({"kind": "ping"}) is False
    assert hub.websocket is websocket


def test_media_pipeline_accepts_reset_device_timestamps_after_stream_reset(tmp_path) -> None:
    _client, _agent, service, _llm, _asr = make_media_client(tmp_path)

    assert service.pipeline.push_video_frame(
        MediaFrame.jpeg(b"old-boot", timestamp_ms=5_000, sequence=1)
    )
    service.pipeline.reset_stream()
    assert service.pipeline.push_video_frame(
        MediaFrame.jpeg(b"new-boot", timestamp_ms=10, sequence=1)
    )

    assert [frame.timestamp_ms for frame in service.pipeline.video_window()] == [10]


def test_media_service_allows_only_one_device_connection(tmp_path) -> None:
    client, _agent, _service, _llm, _asr = make_media_client(tmp_path)

    with client.websocket_connect("/ws/media") as media1:
        media1.send_json(
            {
                "device_id": "device-1",
                "token": "",
                "boot_id": "boot-1",
                "capabilities": MediaHello(device_id="device-1", token="", boot_id="boot-1").capabilities,
            }
        )
        assert media1.receive_json()["kind"] == "media_ready"

        with client.websocket_connect("/ws/media") as media2:
            media2.send_json(
                {
                    "device_id": "device-2",
                    "token": "",
                    "boot_id": "boot-2",
                    "capabilities": MediaHello(device_id="device-2", token="", boot_id="boot-2").capabilities,
                }
            )
            with pytest.raises(WebSocketDisconnect) as exc:
                media2.receive_json()

        assert exc.value.code in {1008, 1013}


@pytest.mark.asyncio
async def test_media_session_time_uses_host_clock_for_tts_resume_and_enrollment(tmp_path) -> None:
    clock = StepClock(10_000)
    settings = Settings(
        runtime_dir=tmp_path,
        tts_resume_delay_ms=200,
        data_key=Fernet.generate_key().decode("utf-8"),
    )
    agent = NaobotAgent(settings, llm=SlowFriendlyLLM())
    media_service = MediaService(
        settings=settings,
        agent=agent,
        session=InteractionSession(tts_resume_delay_ms=settings.tts_resume_delay_ms),
        wake_word=FakeWakeWord(),
        identity=UnknownIdentity(),
        asr=SequenceASR(["记住我", "确认"]),
        vision=FakeVision(),
        tts=FakeTTS(),
        clock=clock,
    )
    sent_json = []
    sent_binary = []

    async def capture_json(payload):
        sent_json.append(payload)
        return True

    async def capture_binary(payload):
        sent_binary.append(MediaFrame.decode(payload))
        return True

    media_service.hub.send_json = capture_json  # type: ignore[method-assign]
    media_service.hub.send_binary = capture_binary  # type: ignore[method-assign]

    for sequence in range(1, 6):
        await media_service._handle_frame(
            MediaFrame.jpeg(
                f"jpeg-{sequence}".encode("ascii"),
                timestamp_ms=1_000 + sequence,
                sequence=sequence,
            )
        )

    clock.set(10_001)
    await media_service._handle_frame(
        MediaFrame.audio_pcm16(b"wake", timestamp_ms=1_000, sequence=10, flags=1)
    )
    await media_service._handle_frame(
        MediaFrame.audio_pcm16(b"utter", timestamp_ms=1_001, sequence=11, flags=3)
    )

    assert any(item.get("kind") == "enrollment" and item.get("status") == "pending" for item in sent_json)
    assert any(item.get("kind") == "tts_start" for item in sent_json)
    assert sent_binary and sent_binary[0].kind == MediaFrameKind.TTS_PCM16

    clock.set(10_202)
    await media_service._handle_frame(
        MediaFrame.audio_pcm16(b"confirm", timestamp_ms=1_010, sequence=12, flags=3)
    )
    assert any(item.get("kind") == "enrollment" and item.get("status") == "awaiting_touch" for item in sent_json)

    clock.set(10_203)
    await media_service._handle_control_json('{"kind":"touch_head"}')

    assert any(item.get("kind") == "enrollment" and item.get("status") == "completed" for item in sent_json)


@pytest.mark.asyncio
async def test_media_service_never_calls_cloud_or_agent_before_activation(tmp_path) -> None:
    settings = Settings(runtime_dir=tmp_path)
    agent = CountingAgent()
    asr = FakeASR()
    vision = CountingVision()
    service = MediaService(
        settings=settings,
        agent=agent,
        wake_word=QuietWakeWord(),
        identity=PassiveIdentity(),
        asr=asr,
        vision=vision,
        tts=FakeTTS(),
    )

    await service._handle_frame(
        MediaFrame.audio_pcm16(b"speech", timestamp_ms=100, sequence=1, flags=0x3)
    )

    assert asr.calls == 0
    assert vision.calls == 0
    assert agent.calls == 0


@pytest.mark.asyncio
async def test_host_vad_does_not_accumulate_silent_audio_before_speech(tmp_path) -> None:
    service = MediaService(
        settings=Settings(runtime_dir=tmp_path, vad_rms_threshold=500),
        agent=CountingAgent(),
        wake_word=QuietWakeWord(),
        identity=PassiveIdentity(),
        asr=FakeASR(),
        vision=CountingVision(),
        tts=FakeTTS(),
    )

    for sequence in range(1, 101):
        await service._handle_frame(
            MediaFrame.audio_pcm16(
                b"\x00\x00" * 160,
                timestamp_ms=sequence * 10,
                sequence=sequence,
            )
        )

    assert service._audio_turn == []


@pytest.mark.asyncio
async def test_local_greeting_activates_before_cloud_turn(tmp_path) -> None:
    settings = Settings(runtime_dir=tmp_path)
    agent = CountingAgent()
    asr = FakeASR()
    vision = CountingVision()
    wake = LocalPhraseWakeWordDetector(transcriber=lambda _frames: "你好，小龟")
    service = MediaService(
        settings=settings,
        agent=agent,
        wake_word=wake,
        identity=PassiveIdentity(),
        asr=asr,
        vision=vision,
        tts=FakeTTS(),
    )

    await service._handle_frame(
        MediaFrame.audio_pcm16(b"hello", timestamp_ms=100, sequence=1, flags=0x1)
    )
    assert asr.calls == 0
    await service._handle_frame(
        MediaFrame.audio_pcm16(b"end", timestamp_ms=120, sequence=2, flags=0x3)
    )

    snapshot = service.session.snapshot(now_ms=120)
    assert snapshot.active is True
    assert snapshot.session_trigger == "greeting"
    assert asr.calls == 1
    assert vision.calls == 1
    assert agent.calls == 1


@pytest.mark.asyncio
async def test_media_status_exposes_ram_only_temporal_summary(tmp_path, monkeypatch) -> None:
    def reject_media_write(*_args, **_kwargs):
        raise AssertionError("环境感知媒体不应落盘")

    monkeypatch.setattr("pathlib.Path.write_bytes", reject_media_write)
    settings = Settings(runtime_dir=tmp_path, temporal_summary_interval_ms=1_000)
    service = MediaService(
        settings=settings,
        agent=CountingAgent(),
        wake_word=QuietWakeWord(),
        identity=PassiveIdentity(),
        asr=FakeASR(),
        vision=CountingVision(),
        tts=FakeTTS(),
        clock=StepClock(1_000),
    )

    await service._handle_frame(MediaFrame.jpeg(b"jpeg", timestamp_ms=1, sequence=1))

    summary = service.status()["last_temporal_summary"]
    assert summary["timestamp_ms"] == 1_000
    assert summary["scene_summary"] == "未检测到人脸"
    assert not list(tmp_path.glob("*.jpg"))
    assert not list(tmp_path.glob("*.pcm"))


@pytest.mark.asyncio
async def test_expired_visitor_session_destroys_guest_runtime(tmp_path) -> None:
    clock = StepClock(1_000)
    settings = Settings(runtime_dir=tmp_path, session_idle_ms=10)
    registry = RuntimeRegistry(settings)
    session = InteractionSession(session_idle_ms=10)
    session.activate_from_touch(now_ms=1_000, person_id=None)
    visitor_id = session.snapshot(now_ms=1_000).session_id
    assert visitor_id is not None
    agent = await create_real_guest_runtime(settings, registry, visitor_id)
    service = MediaService(
        settings=settings,
        agent=agent,
        session=session,
        wake_word=QuietWakeWord(),
        identity=PassiveIdentity(),
        asr=FakeASR(),
        vision=CountingVision(),
        tts=FakeTTS(),
        runtime_registry=registry,
        clock=clock,
    )
    assert registry.loaded_count() == 1

    clock.set(1_011)
    await service._handle_frame(MediaFrame.jpeg(b"jpeg", timestamp_ms=10, sequence=1))

    assert service.session.snapshot(now_ms=1_011).active is False
    assert registry.loaded_count() == 0


@pytest.mark.asyncio
async def test_media_websocket_disconnect_destroys_real_guest_runtime(tmp_path) -> None:
    class DisconnectingWebSocket:
        def __init__(self) -> None:
            self.messages = [
                {
                    "text": json.dumps(
                        {"device_id": "device-1", "token": "", "boot_id": "boot-1"}
                    )
                },
                {"type": "websocket.disconnect", "code": 1000},
            ]

        async def accept(self):
            return None

        async def receive(self):
            return self.messages.pop(0)

        async def send_json(self, _payload):
            return None

        async def close(self, code):
            return None

    clock = StepClock(1_000)
    settings = Settings(
        runtime_dir=tmp_path,
        llm_base_url="http://example.test/v1",
        llm_model="test",
    )
    registry = RuntimeRegistry(settings)
    session = InteractionSession()
    session.activate_from_touch(now_ms=1_000, person_id=None)
    visitor_id = session.snapshot(now_ms=1_000).session_id
    assert visitor_id is not None
    agent = await create_real_guest_runtime(settings, registry, visitor_id)
    service = MediaService(
        settings=settings,
        agent=agent,
        session=session,
        wake_word=QuietWakeWord(),
        identity=PassiveIdentity(),
        asr=FakeASR(),
        vision=CountingVision(),
        tts=FakeTTS(),
        runtime_registry=registry,
        clock=clock,
    )
    assert registry.loaded_count() == 1

    await service.handle_websocket(DisconnectingWebSocket())  # type: ignore[arg-type]

    assert registry.loaded_count() == 0
