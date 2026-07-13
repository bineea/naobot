from __future__ import annotations

import importlib
import inspect
import json
import struct
import sys
from pathlib import Path

import pytest

FIRMWARE_ROOT = Path(__file__).resolve().parents[1] / "firmware" / "esp32"
if str(FIRMWARE_ROOT) not in sys.path:
    sys.path.insert(0, str(FIRMWARE_ROOT))

board = importlib.import_module("boards.n16r8_44pin")
firmware_main = importlib.import_module("main")

from media.client import MediaClient, MediaQueue, VideoScheduler  # noqa: E402
from media.devices import AudioInput, AudioOutput, Camera  # noqa: E402
from media.protocol import (  # noqa: E402
    FLAG_END_OF_UTTERANCE,
    FLAG_EVENT_BOOST,
    FLAG_SPEECH,
    HEADER_SIZE,
    KIND_AUDIO_PCM16,
    KIND_JPEG,
    KIND_TTS_PCM16,
    MAX_AUDIO_PCM16_PAYLOAD,
    MAX_JPEG_PAYLOAD,
    MAX_TTS_PCM16_PAYLOAD,
    MediaFrame,
)
from media.websocket import OP_BINARY, OP_TEXT, MediaWebSocket  # noqa: E402

from naobot.media.protocol import MediaFrame as HostMediaFrame  # noqa: E402


class FakeCameraModule:
    FRAME_QVGA = 5
    PIXFORMAT_JPEG = 4
    CAMERA_FB_IN_PSRAM = 0
    CAMERA_GRAB_LATEST = 1

    def __init__(self) -> None:
        self.config = None
        self.psram_dma = None
        self.frames = [b"jpeg-a", b"jpeg-b"]

    def init(self, config):
        self.config = dict(config)
        return True

    def set_psram_dma(self, enabled):
        self.psram_dma = enabled
        return True

    def capture(self):
        return self.frames.pop(0) if self.frames else None

    def psram_free(self):
        return 7_654_321


class FakeI2S:
    RX = 1
    TX = 2
    MONO = 3
    B16 = 16
    instances = []

    def __init__(self, bus_id, **kwargs) -> None:
        self.bus_id = bus_id
        self.kwargs = kwargs
        self.writes = []
        self.__class__.instances.append(self)

    def readinto(self, buffer):
        buffer[:] = b"\x01\x02" * (len(buffer) // 2)
        return len(buffer)

    def write(self, payload):
        self.writes.append(bytes(payload))
        return len(payload)


class FakeTransport:
    def __init__(self, connect_result=True, incoming=None) -> None:
        self.connect_result = connect_result
        self.connected = False
        self.sent_text = []
        self.sent_binary = []
        self.incoming = list(incoming or [])
        self.closed = False

    def connect(self):
        self.connected = self.connect_result
        return self.connected

    def send_text(self, text):
        self.sent_text.append(text)
        return True

    def send_binary(self, payload):
        self.sent_binary.append(bytes(payload))
        return True

    def recv_frame(self):
        if self.incoming:
            return self.incoming.pop(0)
        return None

    def close(self):
        self.closed = True
        self.connected = False


class ExplodingTransport(FakeTransport):
    def send_binary(self, payload):
        raise OSError("link down")


def pin_number(value):
    return value


def test_n16r8_44pin_board_profile_matches_fixed_wiring() -> None:
    assert board.BOARD_NAME == "ESP32-S3-N16R8-44PIN"
    assert board.FLASH_MB == 16
    assert board.PSRAM_MB == 8
    assert board.CAMERA_PINS == {
        "d0": 4,
        "d1": 5,
        "d2": 10,
        "d3": 11,
        "d4": 12,
        "d5": 13,
        "d6": 14,
        "d7": 18,
        "xclk": 21,
        "pclk": 38,
        "vsync": 39,
        "href": 40,
        "sccb_sda": 8,
        "sccb_scl": 9,
    }
    assert board.INMP441_PINS == {"sck": 41, "ws": 42, "sd": 47}
    assert board.MAX98357A_PINS == {"bclk": 19, "lrc": 20, "din": 45}
    assert board.TOUCH_PINS == {"head": 1, "back": 2}
    assert board.SERVO_PINS == {"lf": 6, "rf": 7, "lr": 15, "rr": 16}
    assert board.I2C_PINS == {"sda": 8, "scl": 9}
    assert board.BUZZER_PIN == 17
    assert board.AVOID_PINS == (35, 36, 37, 48)
    assert board.USB_UART_BRIDGE == "CH343"
    assert not set(board.used_pins()) & set(board.AVOID_PINS)


def test_firmware_protocol_is_byte_compatible_with_host() -> None:
    firmware_frame = MediaFrame(
        KIND_AUDIO_PCM16,
        timestamp_ms=12_345,
        sequence=7,
        payload=b"\x01\x02\x03\x04",
        flags=FLAG_SPEECH | FLAG_END_OF_UTTERANCE | FLAG_EVENT_BOOST,
    )
    assert HEADER_SIZE == struct.calcsize(">4sBBHIQI") == 24
    encoded = firmware_frame.encode()
    host_encoded = HostMediaFrame.audio_pcm16(
        firmware_frame.payload,
        timestamp_ms=12_345,
        sequence=7,
        flags=7,
    ).encode()
    magic, version, kind, flags, sequence, timestamp_ms, payload_length = struct.unpack(
        ">4sBBHIQI", encoded[:HEADER_SIZE]
    )
    assert (magic, version, kind, flags) == (b"NABM", 1, KIND_AUDIO_PCM16, 7)
    assert sequence == 7
    assert timestamp_ms == 12_345
    assert payload_length == 4
    assert encoded == host_encoded
    assert encoded == struct.pack(">4sBBHIQI", b"NABM", 1, 1, 7, 7, 12_345, 4) + firmware_frame.payload
    assert MediaFrame.decode(encoded).payload == firmware_frame.payload
    assert (KIND_AUDIO_PCM16, KIND_JPEG, KIND_TTS_PCM16) == (1, 2, 3)


@pytest.mark.parametrize(
    ("kind", "limit"),
    [
        (KIND_AUDIO_PCM16, MAX_AUDIO_PCM16_PAYLOAD),
        (KIND_JPEG, MAX_JPEG_PAYLOAD),
        (KIND_TTS_PCM16, MAX_TTS_PCM16_PAYLOAD),
    ],
)
def test_firmware_protocol_enforces_host_payload_limits(kind, limit) -> None:
    MediaFrame(kind, 1, 1, b"x" * limit)
    with pytest.raises(ValueError, match="payload"):
        MediaFrame(kind, 1, 2, b"x" * (limit + 1))


def test_firmware_protocol_rejects_invalid_sequence_and_truncated_payload() -> None:
    with pytest.raises(ValueError, match="sequence"):
        MediaFrame(KIND_JPEG, 1, -1, b"jpeg")
    with pytest.raises(ValueError, match="sequence"):
        MediaFrame(KIND_JPEG, 1, 0x1_0000_0000, b"jpeg")

    encoded = MediaFrame(KIND_JPEG, 1, 1, b"jpeg").encode()
    with pytest.raises(ValueError, match="length"):
        MediaFrame.decode(encoded[:-1])


def test_camera_configures_qvga_jpeg_double_buffer_psram_dma_latest() -> None:
    module = FakeCameraModule()
    camera = Camera(camera_module=module)

    assert camera.available is True
    assert module.config["frame_size"] == module.FRAME_QVGA
    assert module.config["pixel_format"] == module.PIXFORMAT_JPEG
    assert module.config["jpeg_quality"] == 12
    assert module.config["fb_count"] == 2
    assert module.config["fb_location"] == module.CAMERA_FB_IN_PSRAM
    assert module.config["grab_mode"] == module.CAMERA_GRAB_LATEST
    assert module.config["sccb_i2c_port"] == 0
    assert module.config["reuse_sccb_i2c"] is True
    assert module.psram_dma is True
    assert camera.capture() == b"jpeg-a"
    assert camera.psram_free() == 7_654_321


def test_media_devices_degrade_safely_without_micropython_modules() -> None:
    camera = Camera(camera_module=None)
    audio_in = AudioInput(i2s_class=None, pin_factory=None)
    audio_out = AudioOutput(i2s_class=None, pin_factory=None)

    assert camera.available is False
    assert camera.capture() is None
    assert camera.psram_free() == 0
    assert audio_in.available is False
    assert audio_in.read_chunk() is None
    assert audio_out.available is False
    assert audio_out.write(b"pcm") == 0


def test_i2s_devices_use_pcm16_mono_16khz_and_fixed_pins() -> None:
    FakeI2S.instances = []
    audio_in = AudioInput(i2s_class=FakeI2S, pin_factory=pin_number)
    audio_out = AudioOutput(i2s_class=FakeI2S, pin_factory=pin_number)

    rx, tx = FakeI2S.instances
    assert rx.kwargs == {
        "sck": 41,
        "ws": 42,
        "sd": 47,
        "mode": FakeI2S.RX,
        "bits": FakeI2S.B16,
        "format": FakeI2S.MONO,
        "rate": 16_000,
        "ibuf": 8_000,
    }
    assert tx.kwargs == {
        "sck": 19,
        "ws": 20,
        "sd": 45,
        "mode": FakeI2S.TX,
        "bits": FakeI2S.B16,
        "format": FakeI2S.MONO,
        "rate": 16_000,
        "ibuf": 8_000,
    }
    assert audio_in.read_chunk() == b"\x01\x02" * 320
    assert audio_out.write(b"pcm16") == 5
    assert tx.writes == [b"pcm16"]


def test_video_scheduler_runs_at_normal_and_event_fps() -> None:
    scheduler = VideoScheduler()

    assert scheduler.should_capture(0, event_boost=False) is True
    assert scheduler.should_capture(99, event_boost=False) is False
    assert scheduler.should_capture(100, event_boost=False) is True

    scheduler.reset()
    assert scheduler.should_capture(0, event_boost=True) is True
    assert scheduler.should_capture(66, event_boost=True) is False
    assert scheduler.should_capture(67, event_boost=True) is True


def test_media_queue_drops_old_video_before_non_speech_audio() -> None:
    queue = MediaQueue(max_items=3)
    speech_1 = MediaFrame(KIND_AUDIO_PCM16, 1, 1, b"s1", FLAG_SPEECH)
    video_2 = MediaFrame(KIND_JPEG, 2, 2, b"v2")
    video_3 = MediaFrame(KIND_JPEG, 3, 3, b"v3")
    speech_4 = MediaFrame(KIND_AUDIO_PCM16, 4, 4, b"s4", FLAG_SPEECH)
    for frame in (speech_1, video_2, video_3):
        assert queue.put(frame) is True

    assert queue.put(speech_4) is True
    assert [frame.sequence for frame in queue.items()] == [1, 3, 4]
    assert queue.dropped_video == 1

    queue = MediaQueue(max_items=3)
    quiet_2 = MediaFrame(KIND_AUDIO_PCM16, 2, 2, b"q")
    for frame in (speech_1, quiet_2, speech_4):
        assert queue.put(frame) is True
    assert queue.put(MediaFrame(KIND_AUDIO_PCM16, 5, 5, b"s5", FLAG_SPEECH)) is True
    assert [frame.sequence for frame in queue.items()] == [1, 4, 5]
    assert queue.dropped_audio == 1


def test_media_queue_preserves_queued_speech_when_no_drop_candidate() -> None:
    queue = MediaQueue(max_items=2)
    queue.put(MediaFrame(KIND_AUDIO_PCM16, 1, 1, b"s1", FLAG_SPEECH))
    queue.put(MediaFrame(KIND_AUDIO_PCM16, 2, 2, b"s2", FLAG_SPEECH))

    assert queue.put(MediaFrame(KIND_JPEG, 3, 3, b"v3")) is False
    assert [frame.sequence for frame in queue.items()] == [1, 2]
    assert queue.dropped_total == 1


def test_media_websocket_supports_large_masked_binary_frames() -> None:
    payload = b"x" * (256 * 1024)
    frame = MediaWebSocket("ws://host:8765/ws/media")._encode_frame(payload, OP_BINARY)

    assert frame[0] == 0x80 | OP_BINARY
    assert frame[1] == 0x80 | 127
    length = int.from_bytes(frame[2:10], "big")
    mask = frame[10:14]
    decoded = bytes(frame[14 + i] ^ mask[i % 4] for i in range(length))
    assert length == len(payload)
    assert decoded == payload


def test_media_websocket_closes_on_fatal_receive_error() -> None:
    class ResetSocket:
        def __init__(self):
            self.closed = False

        def recv(self, _size):
            raise OSError(104, "connection reset")

        def close(self):
            self.closed = True

    websocket = MediaWebSocket("ws://host:8765/ws/media")
    websocket.sock = ResetSocket()
    websocket.connected = True

    assert websocket.recv_frame() is None
    assert websocket.connected is False
    assert websocket.sock is None


def make_client(transport, *, state=None, camera=None, audio_in=None, audio_out=None):
    return MediaClient(
        "ws://host:8765/ws/media",
        device_id="robot-1",
        token="secret",
        boot_id="boot-1",
        camera=camera or Camera(camera_module=FakeCameraModule()),
        audio_input=audio_in or AudioInput(i2s_class=FakeI2S, pin_factory=pin_number),
        audio_output=audio_out or AudioOutput(i2s_class=FakeI2S, pin_factory=pin_number),
        transport_factory=lambda _url: transport,
        state=state,
    )


def test_media_client_sends_identity_hello_before_binary_media() -> None:
    transport = FakeTransport()
    client = make_client(transport)

    assert client.step(0) is True

    hello = json.loads(transport.sent_text[0])
    assert hello == {
        "kind": "media_hello",
        "device_id": "robot-1",
        "token": "secret",
        "boot_id": "boot-1",
        "capabilities": {
            "video": {
                "nominal_fps": 10,
                "event_fps": 15,
                "resolution": {"width": 320, "height": 240},
            },
            "audio": {
                "format": {"sample_rate_hz": 16_000, "channels": 1, "encoding": "pcm16"}
            },
            "image": {"encoding": "jpeg"},
        },
    }
    assert transport.sent_binary
    sequences = [MediaFrame.decode(raw).sequence for raw in transport.sent_binary]
    assert sequences == sorted(sequences)


def test_media_client_reconnects_after_disconnect_without_raising() -> None:
    transports = [FakeTransport(connect_result=False), FakeTransport(connect_result=True)]
    state = {}
    client = MediaClient(
        "ws://host:8765/ws/media",
        device_id="robot-1",
        token="secret",
        boot_id="boot-1",
        camera=Camera(camera_module=None),
        audio_input=AudioInput(i2s_class=None, pin_factory=None),
        audio_output=AudioOutput(i2s_class=None, pin_factory=None),
        transport_factory=lambda _url: transports.pop(0),
        state=state,
    )

    assert client.step(0) is False
    assert state["media_connected"] is False
    assert client.step(100) is True
    assert state["media_connected"] is True


def test_tts_downlink_pauses_uploads_but_resumes_after_tts_end() -> None:
    transport = FakeTransport()
    camera_module = FakeCameraModule()
    output = AudioOutput(i2s_class=FakeI2S, pin_factory=pin_number)
    client = make_client(
        transport,
        camera=Camera(camera_module=camera_module),
        audio_out=output,
    )
    assert client.connect() is True

    client.handle_incoming(OP_TEXT, b'{"kind":"tts_start"}')
    client.collect(0, event_boost=False)
    assert client.state["audio_state"] == "speaking"
    assert len(client.queue) == 0

    tts_payload = b"t" * 2048
    tts = MediaFrame(KIND_TTS_PCM16, timestamp_ms=1, sequence=1, payload=tts_payload).encode()
    client.handle_incoming(OP_BINARY, tts)
    assert output.i2s.writes == []

    assert client.step(1) is True
    assert output.i2s.writes == [tts_payload[:1024]]
    assert client.step(2) is True
    assert output.i2s.writes == [tts_payload[:1024], tts_payload[1024:]]

    client.handle_incoming(OP_TEXT, b'{"kind":"tts_end"}')
    client.collect(100, event_boost=False)
    assert client.state["audio_state"] == "listening"
    assert len(client.queue) > 0


def test_media_failure_is_contained_from_safety_loop_state() -> None:
    transport = ExplodingTransport()
    state = {"safety_iterations": 0}
    client = make_client(transport, state=state)

    assert client.step(0) is False
    state["safety_iterations"] += 1

    assert state["safety_iterations"] == 1
    assert state["media_connected"] is False
    assert transport.closed is True
    assert "create_task(media_loop" in inspect.getsource(firmware_main.main)


def test_send_failure_keeps_unsent_frame_at_queue_head() -> None:
    transport = ExplodingTransport()
    client = make_client(transport)
    assert client.connect() is True
    speech = MediaFrame(KIND_AUDIO_PCM16, 1, 42, b"speech", FLAG_SPEECH)
    client.queue.put(speech)

    with pytest.raises(OSError, match="link down"):
        client.flush_one()

    assert client.queue.items()[0] is speech


def test_heartbeat_includes_media_health_fields() -> None:
    class Power:
        battery_pct = 88

    class Imu:
        def read_posture(self):
            return "upright"

    state = {
        "agent_online": True,
        "local_loop_ms": 2,
        "camera_fps": 10,
        "audio_state": "listening",
        "media_queue": 3,
        "media_dropped": 4,
        "psram_free": 7_654_321,
    }

    payload = firmware_main.FirmwareProtocol("kt2-test").heartbeat(Power(), Imu(), state=state)[
        "payload"
    ]

    assert payload["camera_fps"] == 10
    assert payload["audio_state"] == "listening"
    assert payload["media_queue"] == 3
    assert payload["media_dropped"] == 4
    assert payload["psram_free"] == 7_654_321


def test_custom_image_recipe_pins_exact_upstream_versions() -> None:
    build_root = FIRMWARE_ROOT / "build"
    script = (build_root / "build.ps1").read_text(encoding="utf-8")
    sdkconfig = (build_root / "sdkconfig.board").read_text(encoding="utf-8")
    board_cmake = (build_root / "N16R8_44PIN" / "mpconfigboard.cmake").read_text(
        encoding="utf-8"
    )
    cmake = (build_root / "camera_module" / "micropython.cmake").read_text(encoding="utf-8")

    assert "v1.28.0" in script
    assert "v2.1.6" in script
    assert "CONFIG_CAMERA_PSRAM_DMA=y" in sdkconfig
    assert "CONFIG_SPIRAM_MODE_OCT=y" in sdkconfig
    assert "CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y" in sdkconfig
    assert "${MICROPY_BOARD_DIR}/../sdkconfig.board" in board_cmake
    assert "BOARD_DIR=$BoardDir" in script
    assert "esp32-camera" in cmake
