import importlib
import sys
from pathlib import Path

FIRMWARE_ROOT = Path(__file__).resolve().parents[1] / "firmware" / "esp32"
if str(FIRMWARE_ROOT) not in sys.path:
    sys.path.insert(0, str(FIRMWARE_ROOT))

firmware_main = importlib.import_module("main")
websocket_client = importlib.import_module("comm.websocket_client")

from comm.websocket_client import WebSocketClient, parse_ws_url  # noqa: E402
from motion.action_player import ActionResult  # noqa: E402


def decode_masked_payload(frame: bytes) -> bytes:
    length = frame[1] & 0x7F
    offset = 2
    if length == 126:
        length = (frame[2] << 8) | frame[3]
        offset = 4
    mask = frame[offset : offset + 4]
    payload = frame[offset + 4 : offset + 4 + length]
    return bytes(payload[i] ^ mask[i % 4] for i in range(length))


class FakeActions:
    def __init__(self):
        self.executed = []
        self.stopped = False

    def execute(self, action):
        self.executed.append(action)
        return ActionResult(True)

    def stop(self):
        self.stopped = True


class FakeSafety:
    def __init__(self, allowed=True):
        self.allowed = allowed

    def can_execute(self, action):
        return self.allowed and action.get("name") != "unsafe"


class FakeWs:
    def __init__(self):
        self.sent = []

    def send_json(self, payload):
        self.sent.append(payload)
        return True


class FailingActions(FakeActions):
    def execute(self, action):
        self.executed.append(action)
        return ActionResult(False, "测试执行失败")


class FakeMotion:
    def __init__(self, accepted=True):
        self.accepted = accepted
        self.submitted = []
        self.cancelled = False

    def submit_intent(self, message):
        self.submitted.append(message)
        return self.accepted, "" if self.accepted else "调度失败"

    def cancel(self, reason="cancelled"):
        self.cancelled = True


def test_parse_ws_url_with_port_and_path() -> None:
    assert parse_ws_url("ws://192.168.1.2:8765/ws/kt2") == ("192.168.1.2", 8765, "/ws/kt2")
    assert parse_ws_url("ws://host.local") == ("host.local", 80, "/")


def test_parse_ws_url_rejects_wss() -> None:
    try:
        parse_ws_url("wss://example.com/ws")
    except ValueError as exc:
        assert "ws://" in str(exc)
    else:
        raise AssertionError("wss url should be rejected")


def test_websocket_text_frame_is_masked() -> None:
    frame = WebSocketClient("ws://host/ws")._encode_frame(b"hello")

    assert frame[0] == 0x81
    assert frame[1] & 0x80
    assert decode_masked_payload(frame) == b"hello"


def test_random_bytes_uses_byte_sized_getrandbits(monkeypatch) -> None:
    class MicroPythonRandom:
        calls = []

        @staticmethod
        def getrandbits(bits):
            MicroPythonRandom.calls.append(bits)
            if bits > 32:
                raise ValueError("bits must be 32 or less")
            return 7

    monkeypatch.setattr(websocket_client, "random", MicroPythonRandom)

    assert websocket_client._random_bytes(16) == bytes([7] * 16)
    assert MicroPythonRandom.calls == [8] * 16


def test_firmware_protocol_event_shape() -> None:
    class Power:
        battery_pct = 77

    class Imu:
        def read_posture(self):
            return "upright"

    protocol = firmware_main.FirmwareProtocol("kt2-test")
    event = protocol.event("touch_head", Power(), Imu())

    assert event["type"] == "event"
    assert event["session_id"] == "kt2-test"
    assert event["payload"]["name"] == "touch_head"
    assert event["payload"]["battery_pct"] == 77
    assert event["payload"]["posture"] == "upright"


def test_execute_intent_runs_safe_actions_and_acks() -> None:
    actions = FakeActions()
    ws = FakeWs()
    protocol = firmware_main.FirmwareProtocol("kt2-test")
    message = {
        "id": "intent-1",
        "type": "intent",
        "payload": {"actions": [{"name": "set_face", "args": {"face": "happy"}}]},
    }

    firmware_main.execute_intent(message, actions, FakeSafety(), protocol, ws)

    assert actions.executed == [{"name": "set_face", "args": {"face": "happy"}}]
    assert ws.sent[-1]["type"] == "ack"
    assert ws.sent[-1]["payload"]["intent_id"] == "intent-1"


def test_execute_intent_rejects_unsafe_action_without_execution() -> None:
    actions = FakeActions()
    ws = FakeWs()
    protocol = firmware_main.FirmwareProtocol("kt2-test")
    message = {
        "id": "intent-2",
        "type": "intent",
        "payload": {"actions": [{"name": "unsafe", "args": {}}]},
    }

    firmware_main.execute_intent(message, actions, FakeSafety(), protocol, ws)

    assert actions.executed == []
    assert ws.sent[-1]["type"] == "error"
    assert ws.sent[-1]["payload"]["code"] == "POLICY_DENIED"


def test_execute_intent_reports_execution_failure() -> None:
    actions = FailingActions()
    ws = FakeWs()
    protocol = firmware_main.FirmwareProtocol("kt2-test")
    message = {
        "id": "intent-4",
        "type": "intent",
        "payload": {"actions": [{"name": "set_face", "args": {"face": "happy"}}]},
    }

    firmware_main.execute_intent(message, actions, FakeSafety(), protocol, ws)

    assert actions.executed == [{"name": "set_face", "args": {"face": "happy"}}]
    assert ws.sent[-1]["type"] == "error"
    assert ws.sent[-1]["payload"]["code"] == "EXECUTION_FAILED"


def test_execute_intent_stop_bypasses_policy() -> None:
    actions = FakeActions()
    ws = FakeWs()
    protocol = firmware_main.FirmwareProtocol("kt2-test")
    message = {
        "id": "intent-3",
        "type": "intent",
        "payload": {"actions": [{"name": "stop", "args": {}}]},
    }

    firmware_main.execute_intent(message, actions, FakeSafety(allowed=False), protocol, ws)

    assert actions.stopped is True
    assert ws.sent[-1]["type"] == "ack"


def test_execute_intent_accepts_semantic_payload_with_motion_controller() -> None:
    actions = FakeActions()
    motion = FakeMotion()
    ws = FakeWs()
    protocol = firmware_main.FirmwareProtocol("kt2-test")
    message = {
        "id": "intent-5",
        "type": "intent",
        "payload": {
            "expression": {"emotion": "happy", "eye_open": 0.8},
            "skills": [{"name": "wave", "args": {"level": 1}}],
            "actions": [{"name": "blink", "args": {}}],
        },
    }

    firmware_main.execute_intent(message, actions, FakeSafety(), protocol, ws, motion=motion)

    assert motion.submitted == [message]
    assert ws.sent[-1]["type"] == "ack"


def test_execute_intent_stop_cancels_motion_controller() -> None:
    actions = FakeActions()
    motion = FakeMotion()
    ws = FakeWs()
    protocol = firmware_main.FirmwareProtocol("kt2-test")
    message = {
        "id": "intent-6",
        "type": "intent",
        "payload": {"skills": [{"name": "stop", "args": {}}]},
    }

    firmware_main.execute_intent(message, actions, FakeSafety(allowed=False), protocol, ws, motion=motion)

    assert motion.cancelled is True
    assert ws.sent[-1]["type"] == "ack"
