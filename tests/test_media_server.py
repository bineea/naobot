from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from naobot.agent import NaobotAgent
from naobot.interaction.session import InteractionSession
from naobot.llm import RuleBasedLLMClient
from naobot.media.backends import ASRResult, IdentityResult, TTSResult, VisionResult, WakeWordResult
from naobot.media.protocol import MediaFrame, MediaFrameKind, MediaHello
from naobot.media.service import MediaService
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


class FakeASR:
    async def transcribe(self, _frames):
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


def make_media_client(tmp_path, *, settings: Settings | None = None):
    settings = settings or Settings(runtime_dir=tmp_path, host_heartbeat_interval_ms=40)
    llm = SlowFriendlyLLM()
    agent = NaobotAgent(settings, llm=llm)
    media_service = MediaService(
        settings=settings,
        agent=agent,
        session=InteractionSession(tts_resume_delay_ms=settings.tts_resume_delay_ms),
        wake_word=FakeWakeWord(),
        identity=FakeIdentity(),
        asr=FakeASR(),
        vision=FakeVision(),
        tts=FakeTTS(),
    )
    return TestClient(create_app(settings, agent, media_service=media_service)), agent, media_service, llm


def test_media_websocket_rejects_invalid_token_with_1008(tmp_path) -> None:
    settings = Settings(
        runtime_dir=tmp_path,
        device_token="expected-token",
        host_heartbeat_interval_ms=40,
    )
    client, _agent, _service, _llm = make_media_client(tmp_path, settings=settings)

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
    client, agent, _service, llm = make_media_client(tmp_path)

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
        for _ in range(20):
            message = kt2.receive_json()
            if message["type"] == "intent":
                intent = message
                break
        assert intent is not None
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
    client, _agent, _service, _llm = make_media_client(tmp_path)

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
