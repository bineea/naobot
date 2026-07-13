from __future__ import annotations

import asyncio

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from naobot.agent import NaobotAgent
from naobot.interaction.session import InteractionSession
from naobot.llm import RuleBasedLLMClient
from naobot.media.backends import ASRResult, IdentityResult, TTSResult, VisionResult, WakeWordResult
from naobot.media.protocol import MediaFrame, MediaFrameKind, MediaHello
from naobot.media.service import MediaHub, MediaService
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
