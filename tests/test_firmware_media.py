from __future__ import annotations

import base64
import hashlib
import importlib
import json
import struct
import sys
import threading
import time
from pathlib import Path

import pytest

FIRMWARE_ROOT = Path(__file__).resolve().parents[1] / "firmware" / "esp32"
if str(FIRMWARE_ROOT) not in sys.path:
    sys.path.insert(0, str(FIRMWARE_ROOT))

board = importlib.import_module("boards.n16r8_44pin")
firmware_main = importlib.import_module("main")
firmware_config = importlib.import_module("config")
wifi_config = importlib.import_module("comm.wifi_config")
media_websocket = importlib.import_module("media.websocket")

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
from media.websocket import OP_BINARY, OP_CLOSE, OP_PING, OP_TEXT, MediaWebSocket  # noqa: E402

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
        self.frame_ready = True
        self.capture_calls = 0

    def init(self, config):
        self.config = dict(config)
        return True

    def set_psram_dma(self, enabled):
        self.psram_dma = enabled
        return True

    def capture(self):
        self.capture_calls += 1
        return self.frames.pop(0) if self.frames else None

    def available_frames(self):
        return self.frame_ready

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
        self.irq_handler = None
        self.__class__.instances.append(self)

    def irq(self, handler):
        self.irq_handler = handler

    def trigger_ready(self):
        if self.irq_handler:
            self.irq_handler(self)

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


class ControlledAudioOutput:
    available = True

    def __init__(self, write_results) -> None:
        self.write_results = list(write_results)
        self.writes = []

    def write(self, payload):
        self.writes.append(bytes(payload))
        if self.write_results:
            return self.write_results.pop(0)
        return len(payload)


class ScriptedSocket:
    def __init__(self, chunks=(), send_limit=None) -> None:
        self.chunks = list(chunks)
        self.send_limit = send_limit
        self.sent = []
        self.closed = False
        self.timeouts = []

    def recv(self, size):
        if not self.chunks:
            raise TimeoutError()
        chunk = self.chunks.pop(0)
        if len(chunk) > size:
            self.chunks.insert(0, chunk[size:])
            chunk = chunk[:size]
        return chunk

    def send(self, payload):
        count = len(payload) if self.send_limit is None else min(len(payload), self.send_limit)
        self.sent.append(bytes(payload[:count]))
        return count

    def settimeout(self, value):
        self.timeouts.append(value)

    def close(self):
        self.closed = True


def server_frame(opcode, payload=b"", *, fin=True, masked=False, rsv=0):
    payload = bytes(payload)
    first = (0x80 if fin else 0) | rsv | opcode
    mask_bit = 0x80 if masked else 0
    if len(payload) < 126:
        header = bytes((first, mask_bit | len(payload)))
    else:
        header = bytes((first, mask_bit | 126)) + len(payload).to_bytes(2, "big")
    if not masked:
        return header + payload
    mask = b"mask"
    encoded = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return header + mask + encoded


def decode_client_frame(raw):
    assert raw[1] & 0x80
    length = raw[1] & 0x7F
    offset = 2
    if length == 126:
        length = int.from_bytes(raw[offset : offset + 2], "big")
        offset += 2
    elif length == 127:
        length = int.from_bytes(raw[offset : offset + 8], "big")
        offset += 8
    mask = raw[offset : offset + 4]
    offset += 4
    payload = bytes(raw[offset + i] ^ mask[i % 4] for i in range(length))
    return raw[0] & 0x0F, payload


def decode_client_frames(raw):
    frames = []
    offset = 0
    while offset < len(raw):
        start = offset
        first, second = raw[offset], raw[offset + 1]
        offset += 2
        length = second & 0x7F
        if length == 126:
            length = int.from_bytes(raw[offset : offset + 2], "big")
            offset += 2
        elif length == 127:
            length = int.from_bytes(raw[offset : offset + 8], "big")
            offset += 8
        assert second & 0x80
        mask = raw[offset : offset + 4]
        offset += 4
        payload = bytes(raw[offset + i] ^ mask[i % 4] for i in range(length))
        offset += length
        frames.append((first & 0x0F, payload, offset - start))
    return frames


def pin_number(value):
    return value


def wait_for_media_step(client, start_ms=0, timeout_sec=1.0):
    deadline = time.perf_counter() + timeout_sec
    current_ms = start_ms
    while time.perf_counter() < deadline:
        if client.step(current_ms):
            return True
        current_ms += 1
        time.sleep(0.001)
    return False


def wait_for_media_connection(client, timeout_sec=1.0):
    deadline = time.perf_counter() + timeout_sec
    while time.perf_counter() < deadline:
        if client.connect():
            return True
        time.sleep(0.001)
    return False


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
    assert board.NATIVE_USB_ENABLED is False
    assert board.CONSOLE_TRANSPORT == "CH343_UART"
    assert not set(board.used_pins()) & set(board.AVOID_PINS)
    assert {pin for pin in board.used_pins() if pin in (19, 20)} == {19, 20}
    assert not ({19, 20} & set(board.CAMERA_PINS.values()))
    assert not ({19, 20} & set(board.INMP441_PINS.values()))


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


def test_camera_skips_fb_get_until_driver_reports_frame_ready() -> None:
    module = FakeCameraModule()
    module.frame_ready = False
    camera = Camera(camera_module=module)

    assert camera.capture() is None
    assert module.capture_calls == 0

    module.frame_ready = True
    assert camera.capture() == b"jpeg-a"
    assert module.capture_calls == 1


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
    assert rx.irq_handler is not None
    assert audio_in.read_chunk() is None
    rx.trigger_ready()
    assert audio_in.read_chunk() == b"\x01\x02" * 320
    assert audio_in.read_chunk() is None
    assert tx.irq_handler is not None
    assert audio_out.write(b"pcm16") == 0
    tx.trigger_ready()
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


def test_media_websocket_chunks_large_masked_binary_send() -> None:
    payload = b"x" * (256 * 1024)
    sock = ScriptedSocket(send_limit=337)
    websocket = MediaWebSocket(
        "ws://host:8765/ws/media", send_chunk_bytes=1024
    )
    websocket.sock = sock
    websocket.connected = True

    assert websocket.send_binary(payload)
    while websocket.tx_pending:
        assert websocket.flush_tx_chunk()

    assert sock.sent
    assert max(map(len, sock.sent)) <= 1024
    opcode, decoded = decode_client_frame(b"".join(sock.sent))
    assert opcode == OP_BINARY
    assert decoded == payload


def test_media_websocket_validates_accept_and_preserves_upgrade_leftover(monkeypatch) -> None:
    nonce = b"0123456789abcdef"
    key = base64.b64encode(nonce).decode()
    accept = base64.b64encode(
        hashlib.sha1((key + media_websocket.GUID).encode()).digest()
    ).decode()
    leftover = server_frame(OP_TEXT, b"ready")
    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
    ).encode() + leftover
    sock = ScriptedSocket((response[:17], response[17:]))
    websocket = MediaWebSocket("ws://host:8765/ws/media")
    websocket.sock = sock
    monkeypatch.setattr(media_websocket, "_random_bytes", lambda _length: nonce)

    websocket._handshake()

    assert bytes(websocket._rx) == leftover


def test_media_websocket_handshake_includes_optional_request_headers(monkeypatch) -> None:
    nonce = b"0123456789abcdef"
    key = base64.b64encode(nonce).decode()
    accept = base64.b64encode(
        hashlib.sha1((key + media_websocket.GUID).encode()).digest()
    ).decode()
    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
    ).encode()
    sock = ScriptedSocket((response,))
    websocket = MediaWebSocket(
        "ws://host:8765/ws/kt2",
        headers={"X-Naobot-Token": "device-secret"},
    )
    websocket.sock = sock
    monkeypatch.setattr(media_websocket, "_random_bytes", lambda _length: nonce)

    websocket._handshake()

    request = b"".join(sock.sent)
    assert b"X-Naobot-Token: device-secret\r\n" in request


def test_media_websocket_rejects_invalid_accept(monkeypatch) -> None:
    nonce = b"0123456789abcdef"
    response = (
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Sec-WebSocket-Accept: wrong\r\n\r\n"
    )
    websocket = MediaWebSocket("ws://host:8765/ws/media")
    websocket.sock = ScriptedSocket((response,))
    monkeypatch.setattr(media_websocket, "_random_bytes", lambda _length: nonce)

    with pytest.raises(OSError, match="accept"):
        websocket._handshake()


def test_media_websocket_handles_partial_recv_ping_and_close_handshake() -> None:
    text_frame = server_frame(OP_TEXT, b"partial")
    sock = ScriptedSocket((text_frame[:1], text_frame[1:3], text_frame[3:]))
    websocket = MediaWebSocket("ws://host:8765/ws/media")
    websocket.sock = sock
    websocket.connected = True

    assert websocket.recv_frame() is None
    assert websocket.recv_frame() is None
    assert websocket.recv_frame() == (OP_TEXT, b"partial")

    sock.chunks.extend((server_frame(OP_PING, b"hi"), server_frame(OP_CLOSE, b"\x03\xe8")))
    assert websocket.recv_frame() is None
    opcode, payload = decode_client_frame(b"".join(sock.sent))
    assert (opcode, payload) == (0xA, b"hi")
    sock.sent.clear()

    assert websocket.recv_frame() is None
    opcode, payload = decode_client_frame(b"".join(sock.sent))
    assert (opcode, payload) == (OP_CLOSE, b"\x03\xe8")
    assert websocket.connected is False
    assert sock.closed is True


def test_media_websocket_queues_pong_until_large_data_frame_boundary() -> None:
    sock = ScriptedSocket((server_frame(OP_PING, b"during-send"),), send_limit=64)
    websocket = MediaWebSocket(
        "ws://host:8765/ws/media",
        send_chunk_bytes=128,
    )
    websocket.sock = sock
    websocket.connected = True
    payload = b"x" * 4096

    assert websocket.send_binary(payload)
    bytes_before_ping = len(b"".join(sock.sent))
    assert websocket.recv_frame() is None
    assert len(b"".join(sock.sent)) == bytes_before_ping

    while websocket.tx_pending:
        assert websocket.flush_tx_chunk()

    frames = decode_client_frames(b"".join(sock.sent))
    assert [(opcode, body) for opcode, body, _size in frames] == [
        (OP_BINARY, payload),
        (0xA, b"during-send"),
    ]


def test_media_websocket_queues_close_until_large_data_frame_boundary() -> None:
    close_payload = b"\x03\xe8"
    sock = ScriptedSocket((server_frame(OP_CLOSE, close_payload),), send_limit=64)
    websocket = MediaWebSocket(
        "ws://host:8765/ws/media",
        send_chunk_bytes=128,
    )
    websocket.sock = sock
    websocket.connected = True
    payload = b"z" * 4096

    assert websocket.send_binary(payload)
    assert websocket.recv_frame() is None
    assert websocket.connected is True

    while websocket.tx_pending:
        assert websocket.flush_tx_chunk()

    frames = decode_client_frames(b"".join(sock.sent))
    assert [(opcode, body) for opcode, body, _size in frames] == [
        (OP_BINARY, payload),
        (OP_CLOSE, close_payload),
    ]
    assert websocket.connected is False
    assert sock.closed is True


def test_media_websocket_queues_local_close_until_large_data_frame_boundary() -> None:
    sock = ScriptedSocket(send_limit=64)
    websocket = MediaWebSocket(
        "ws://host:8765/ws/media",
        send_chunk_bytes=128,
    )
    websocket.sock = sock
    websocket.connected = True
    payload = b"l" * 4096

    assert websocket.send_binary(payload)
    websocket.close()
    assert websocket.connected is True

    while websocket.tx_pending:
        assert websocket.flush_tx_chunk()

    frames = decode_client_frames(b"".join(sock.sent))
    assert [(opcode, body) for opcode, body, _size in frames] == [
        (OP_BINARY, payload),
        (OP_CLOSE, b"\x03\xe8"),
    ]
    assert websocket.connected is False


@pytest.mark.parametrize("opcode", [OP_TEXT, OP_BINARY])
def test_media_websocket_reassembles_server_fragments_with_interleaved_ping(opcode) -> None:
    fragments = (
        server_frame(opcode, b"first-", fin=False)
        + server_frame(OP_PING, b"keepalive")
        + server_frame(0x0, b"second-", fin=False)
        + server_frame(0x0, b"last", fin=True)
    )
    sock = ScriptedSocket((fragments,))
    websocket = MediaWebSocket("ws://host:8765/ws/media")
    websocket.sock = sock
    websocket.connected = True

    assert websocket.recv_frame() is None
    assert websocket.recv_frame() is None
    assert decode_client_frame(b"".join(sock.sent)) == (0xA, b"keepalive")
    assert websocket.recv_frame() is None
    assert websocket.recv_frame() == (opcode, b"first-second-last")


@pytest.mark.parametrize(
    "raw",
    [
        server_frame(0x0, b"orphan"),
        server_frame(OP_TEXT, b"first", fin=False) + server_frame(OP_BINARY, b"nested"),
    ],
)
def test_media_websocket_rejects_illegal_fragment_sequences(raw) -> None:
    sock = ScriptedSocket((raw,))
    websocket = MediaWebSocket("ws://host:8765/ws/media")
    websocket.sock = sock
    websocket.connected = True

    assert websocket.recv_frame() is None
    if websocket.connected:
        assert websocket.recv_frame() is None

    opcode, payload = decode_client_frame(b"".join(sock.sent))
    assert opcode == OP_CLOSE
    assert int.from_bytes(payload[:2], "big") == 1002


def test_media_websocket_limits_reassembled_message_size() -> None:
    raw = server_frame(OP_BINARY, b"123456", fin=False) + server_frame(0x0, b"789", fin=True)
    sock = ScriptedSocket((raw,))
    websocket = MediaWebSocket(
        "ws://host:8765/ws/media",
        max_message_bytes=8,
    )
    websocket.sock = sock
    websocket.connected = True

    assert websocket.recv_frame() is None
    assert websocket.recv_frame() is None

    opcode, payload = decode_client_frame(b"".join(sock.sent))
    assert opcode == OP_CLOSE
    assert int.from_bytes(payload[:2], "big") == 1009


@pytest.mark.parametrize(
    "raw",
    [
        server_frame(OP_TEXT, b"masked", masked=True),
        server_frame(OP_TEXT, b"rsv", rsv=0x40),
        bytes((0x80 | OP_PING, 126, 0, 126)) + b"x" * 126,
    ],
)
def test_media_websocket_protocol_errors_close_safely(raw) -> None:
    sock = ScriptedSocket((raw,))
    websocket = MediaWebSocket("ws://host:8765/ws/media")
    websocket.sock = sock
    websocket.connected = True

    assert websocket.recv_frame() is None

    opcode, payload = decode_client_frame(b"".join(sock.sent))
    assert opcode == OP_CLOSE
    assert int.from_bytes(payload[:2], "big") == 1002
    assert websocket.connected is False
    assert sock.closed is True


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

    assert client.step(0) is False
    assert wait_for_media_step(client, start_ms=1)

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
    assert wait_for_media_step(client, start_ms=100)
    assert state["media_connected"] is True


def test_media_step_starts_blocking_connect_off_uasyncio_thread() -> None:
    release_connect = threading.Event()

    class BlockingTransport(FakeTransport):
        def connect(self):
            release_connect.wait(timeout=0.1)
            self.connected = True
            return True

    transport = BlockingTransport()
    client = make_client(transport)

    started = time.perf_counter()
    assert client.step(0) is False
    elapsed = time.perf_counter() - started
    release_connect.set()

    assert elapsed < 0.05


def test_media_connect_api_never_runs_blocking_transport_inline() -> None:
    class SlowTransport(FakeTransport):
        def connect(self):
            time.sleep(0.1)
            self.connected = True
            return True

    client = make_client(SlowTransport())

    started = time.perf_counter()
    assert client.connect() is False

    assert time.perf_counter() - started < 0.05


def test_tts_downlink_keeps_video_but_pauses_audio_until_tts_end() -> None:
    transport = FakeTransport()
    camera_module = FakeCameraModule()
    output = AudioOutput(i2s_class=FakeI2S, pin_factory=pin_number)
    client = make_client(
        transport,
        camera=Camera(camera_module=camera_module),
        audio_out=output,
    )
    assert wait_for_media_connection(client)

    client.handle_incoming(OP_TEXT, b'{"kind":"tts_start"}')
    client.collect(0, event_boost=False)
    assert client.state["audio_state"] == "speaking"
    queued_during_tts = list(client.queue._items)
    assert [frame.kind for frame in queued_during_tts] == [KIND_JPEG]
    assert client.step(100) is True
    assert camera_module.capture_calls == 2
    assert all(frame.kind != KIND_AUDIO_PCM16 for frame in client.queue._items)

    tts_payload = b"t" * 2048
    tts = MediaFrame(KIND_TTS_PCM16, timestamp_ms=1, sequence=1, payload=tts_payload).encode()
    client.handle_incoming(OP_BINARY, tts)
    assert output.i2s.writes == []

    output.i2s.trigger_ready()
    assert client.step(1) is True
    assert output.i2s.writes == [tts_payload[:1024]]
    output.i2s.trigger_ready()
    assert client.step(2) is True
    assert output.i2s.writes == [tts_payload[:1024], tts_payload[1024:]]

    client.handle_incoming(OP_TEXT, b'{"kind":"tts_end"}')
    client.audio_input.i2s.trigger_ready()
    client.collect(200, event_boost=False)
    assert client.state["audio_state"] == "listening"
    assert [frame.kind for frame in client.queue._items] == [KIND_AUDIO_PCM16]


def test_tts_new_start_and_disconnect_reset_playback_atomically() -> None:
    transport = FakeTransport()
    client = make_client(transport)
    assert wait_for_media_connection(client)
    old_frame = MediaFrame(KIND_TTS_PCM16, 1, 1, b"old").encode()

    client.handle_incoming(OP_TEXT, b'{"kind":"tts_start"}', current_ms=10)
    client.handle_incoming(OP_BINARY, old_frame, current_ms=11)
    assert client._tts_chunks == [b"old"]

    client.handle_incoming(OP_TEXT, b'{"kind":"tts_start"}', current_ms=12)
    assert client._tts_chunks == []
    assert client._tts_buffered_bytes == 0
    assert client.state["audio_state"] == "speaking"

    client._disconnect()
    assert client._speaking is False
    assert client._tts_chunks == []
    assert client.state["audio_state"] == "listening"


def test_tts_zero_write_retries_and_playback_timeout_cannot_stick_speaking() -> None:
    transport = FakeTransport()
    output = ControlledAudioOutput([0, 4])
    client = MediaClient(
        "ws://host:8765/ws/media",
        device_id="robot-1",
        token="secret",
        boot_id="boot-1",
        camera=Camera(camera_module=None),
        audio_input=AudioInput(i2s_class=None, pin_factory=None),
        audio_output=output,
        transport_factory=lambda _url: transport,
        tts_playback_timeout_ms=100,
    )
    assert wait_for_media_connection(client)
    frame = MediaFrame(KIND_TTS_PCM16, 1, 1, b"data").encode()
    client.handle_incoming(OP_TEXT, b'{"kind":"tts_start"}', current_ms=0)
    client.handle_incoming(OP_BINARY, frame, current_ms=1)
    client.handle_incoming(OP_TEXT, b'{"kind":"tts_end"}', current_ms=2)

    assert client.step(3)
    assert client._tts_chunks == [b"data"]
    assert client.state["audio_state"] == "speaking"
    assert client.step(4)
    assert client._tts_chunks == []
    assert client.state["audio_state"] == "unavailable"

    client.handle_incoming(OP_TEXT, b'{"kind":"tts_start"}', current_ms=10)
    client.handle_incoming(OP_BINARY, frame, current_ms=11)
    assert client.step(111)
    assert client._speaking is False
    assert client._tts_chunks == []
    assert client.state["audio_state"] == "unavailable"


def test_tts_receive_progress_refreshes_stall_timeout() -> None:
    transport = FakeTransport()
    output = ControlledAudioOutput([0, 0, 0])
    client = MediaClient(
        "ws://host:8765/ws/media",
        device_id="robot-1",
        token="secret",
        boot_id="boot-1",
        camera=Camera(camera_module=None),
        audio_input=AudioInput(i2s_class=None, pin_factory=None),
        audio_output=output,
        transport_factory=lambda _url: transport,
        tts_playback_timeout_ms=100,
    )
    assert wait_for_media_connection(client)
    client.handle_incoming(OP_TEXT, b'{"kind":"tts_start"}', current_ms=0)

    for sequence, current_ms in enumerate((90, 180, 270), start=1):
        frame = MediaFrame(KIND_TTS_PCM16, current_ms, sequence, b"data").encode()
        client.handle_incoming(OP_BINARY, frame, current_ms=current_ms)
        assert client.step(current_ms)
        assert client.state["audio_state"] == "speaking"


def test_tts_write_progress_refreshes_stall_timeout_during_long_playback() -> None:
    transport = FakeTransport()
    output = ControlledAudioOutput([1, 1, 1])
    client = MediaClient(
        "ws://host:8765/ws/media",
        device_id="robot-1",
        token="secret",
        boot_id="boot-1",
        camera=Camera(camera_module=None),
        audio_input=AudioInput(i2s_class=None, pin_factory=None),
        audio_output=output,
        transport_factory=lambda _url: transport,
        tts_playback_timeout_ms=100,
    )
    assert wait_for_media_connection(client)
    client.handle_incoming(OP_TEXT, b'{"kind":"tts_start"}', current_ms=0)
    frame = MediaFrame(KIND_TTS_PCM16, 1, 1, b"long").encode()
    client.handle_incoming(OP_BINARY, frame, current_ms=1)

    for current_ms in (90, 180, 270):
        assert client.step(current_ms)
        assert client.state["audio_state"] == "speaking"


def test_tts_jitter_buffer_is_bounded() -> None:
    client = make_client(FakeTransport())
    client.tts_buffer_limit_bytes = 6
    client.handle_incoming(OP_TEXT, b'{"kind":"tts_start"}', current_ms=0)
    client.handle_incoming(
        OP_BINARY,
        MediaFrame(KIND_TTS_PCM16, 1, 1, b"1234").encode(),
        current_ms=1,
    )
    client.handle_incoming(
        OP_BINARY,
        MediaFrame(KIND_TTS_PCM16, 2, 2, b"5678").encode(),
        current_ms=2,
    )

    assert client._speaking is False
    assert client._tts_chunks == []
    assert client._tts_buffered_bytes == 0
    assert client.state["tts_dropped"] == 1


def test_pcm_energy_vad_marks_speech_and_end_of_utterance() -> None:
    vad_module = importlib.import_module("media.vad")
    vad = vad_module.EnergyVAD(speech_threshold=500, end_silence_chunks=3)
    voice = struct.pack("<160h", *([2_000] * 160))
    silence = b"\x00\x00" * 160

    assert vad.process(voice) & FLAG_SPEECH
    assert vad.process(silence) == 0
    assert vad.process(silence) == 0
    assert vad.process(silence) & FLAG_END_OF_UTTERANCE
    assert vad.process(silence) == 0


def test_media_client_uses_local_vad_flags_for_pcm_uploads() -> None:
    class SequenceAudioInput:
        available = True

        def __init__(self):
            voice = struct.pack("<160h", *([2_000] * 160))
            silence = b"\x00\x00" * 160
            self.chunks = [voice, silence, silence, silence]

        def read_chunk(self):
            return self.chunks.pop(0) if self.chunks else None

    client = make_client(
        FakeTransport(),
        camera=Camera(camera_module=None),
        audio_in=SequenceAudioInput(),
    )
    for current_ms in range(4):
        client.collect(current_ms)

    audio_frames = [frame for frame in client.queue.items() if frame.kind == KIND_AUDIO_PCM16]
    assert audio_frames[0].flags & FLAG_SPEECH
    assert not audio_frames[1].flags & FLAG_END_OF_UTTERANCE
    assert audio_frames[-1].flags & FLAG_END_OF_UTTERANCE


def test_media_step_failure_stall_probe_has_no_internal_long_sleep() -> None:
    transport = ExplodingTransport()
    state = {}
    client = make_client(transport, state=state)

    started = time.perf_counter()
    assert client.step(0) is False
    elapsed = time.perf_counter() - started

    assert elapsed < 0.05
    assert state["media_connected"] is False
    deadline = time.perf_counter() + 1
    while not transport.closed and time.perf_counter() < deadline:
        step_started = time.perf_counter()
        client.step(1)
        assert time.perf_counter() - step_started < 0.05
        time.sleep(0.001)
    assert transport.closed is True


@pytest.mark.asyncio
async def test_safety_loop_sleeps_only_to_50ms_deadline_and_records_overrun() -> None:
    state = {}
    sleeps = []

    async def fake_sleep(delay_ms):
        sleeps.append(delay_ms)

    delay = await firmware_main.sleep_to_safety_deadline(
        100,
        state,
        clock=lambda: 130,
        sleeper=fake_sleep,
    )
    assert delay == 20
    assert sleeps == [20]
    assert state["local_loop_ms"] == 30
    assert state["local_loop_overrun_ms"] == 0

    sleeps.clear()
    delay = await firmware_main.sleep_to_safety_deadline(
        200,
        state,
        clock=lambda: 270,
        sleeper=fake_sleep,
    )
    assert delay == 0
    assert sleeps == []
    assert state["local_loop_ms"] == 70
    assert state["local_loop_overrun_ms"] == 20


@pytest.mark.asyncio
async def test_wifi_connect_wait_is_async_and_never_calls_blocking_sleep(monkeypatch) -> None:
    class FakeWlan:
        def __init__(self):
            self.checks = 0
            self.connected_to = None

        def active(self, _enabled):
            return None

        def isconnected(self):
            self.checks += 1
            return self.checks >= 3

        def connect(self, ssid, password):
            self.connected_to = (ssid, password)

        def ifconfig(self):
            return ("192.168.1.2", "", "", "")

    wlan = FakeWlan()
    sleeps = []

    async def fake_async_sleep(delay_ms):
        sleeps.append(delay_ms)

    monkeypatch.setattr(wifi_config, "get_wlan", lambda: wlan)
    monkeypatch.setattr(
        wifi_config,
        "_sleep_ms",
        lambda _delay: (_ for _ in ()).throw(AssertionError("blocking sleep called")),
    )

    assert await wifi_config.connect_wifi_async(
        "ssid",
        "password",
        timeout_ms=1_000,
        sleeper=fake_async_sleep,
    )
    assert wlan.connected_to == ("ssid", "password")
    assert sleeps == [50]


def test_control_and_media_socket_timeouts_are_strictly_bounded() -> None:
    assert firmware_config.WS_SOCKET_TIMEOUT_SEC <= 0.01
    assert firmware_config.MEDIA_SOCKET_CONNECT_TIMEOUT_SEC <= 0.01
    assert firmware_config.MEDIA_SOCKET_IO_TIMEOUT_SEC <= 0.01
    assert firmware_config.MEDIA_SOCKET_SEND_CHUNK_BYTES <= 1024


def test_send_failure_keeps_unsent_frame_at_queue_head() -> None:
    transport = ExplodingTransport()
    client = make_client(transport)
    assert wait_for_media_connection(client)
    speech = MediaFrame(KIND_AUDIO_PCM16, 1, 42, b"speech", FLAG_SPEECH)
    client.queue.put(speech)

    with pytest.raises(OSError, match="link down"):
        client.flush_one()

    assert client.queue.items()[0] is speech


def test_pending_media_send_does_not_dequeue_a_second_frame() -> None:
    class PendingTransport(FakeTransport):
        tx_pending = True

        def flush_tx_chunk(self):
            return True

    transport = PendingTransport()
    client = make_client(transport)
    assert wait_for_media_connection(client)
    first = MediaFrame(KIND_JPEG, 1, 1, b"first")
    second = MediaFrame(KIND_AUDIO_PCM16, 2, 2, b"second", FLAG_SPEECH)
    client.queue.put(first)
    client.queue.put(second)

    assert client.step(3)

    assert client.queue.items()[:2] == [first, second]
    assert transport.sent_binary == []


def test_heartbeat_includes_media_health_fields() -> None:
    class Power:
        battery_pct = 88

    class Imu:
        def read_posture(self):
            return "upright"

    state = {
        "agent_online": True,
        "local_loop_ms": 2,
        "local_loop_interval_ms": 50,
        "local_loop_overrun_ms": 0,
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
    assert payload["local_loop_interval_ms"] == 50
    assert payload["local_loop_overrun_ms"] == 0


def test_camera_binding_recipe_exposes_nonblocking_frame_probe_and_gil_release() -> None:
    source = (
        FIRMWARE_ROOT / "build" / "camera_module" / "modcamera.c"
    ).read_text(encoding="utf-8")

    assert "esp_camera_available_frames()" in source
    assert "MP_QSTR_available_frames" in source
    assert "MP_THREAD_GIL_EXIT()" in source
    assert "MP_THREAD_GIL_ENTER()" in source


def test_custom_image_recipe_static_structure_pins_exact_upstream_versions() -> None:
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
    assert "CONFIG_ESP_CONSOLE_USB_CDC=n" in sdkconfig
    assert "CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=n" in sdkconfig
    assert "CONFIG_TINYUSB_ENABLED=n" in sdkconfig
    assert "MICROPY_HW_ENABLE_USBDEV=0" in board_cmake
    assert "MICROPY_HW_ESP_USB_SERIAL_JTAG=0" in board_cmake
    assert "MICROPY_HW_ENABLE_UART_REPL=1" in board_cmake
    assert "${MICROPY_BOARD_DIR}/../sdkconfig.board" in board_cmake
    assert "BOARD_DIR=$BoardDir" in script
    assert "esp32-camera" in cmake


def test_readme_reports_recipe_only_and_no_freertos_isolation_claim() -> None:
    readme = (FIRMWARE_ROOT / "README.md").read_text(encoding="utf-8")

    assert "静态编译" not in readme
    assert "未实际执行 C 编译" in readme
    assert "不提供 FreeRTOS 高优先级隔离保证" in readme
    assert "静态结构检查" in readme
    assert "DNS、TCP 和 WebSocket 握手" in readme
    assert "硬件、动作和反射对象不会跨线程" in readme
