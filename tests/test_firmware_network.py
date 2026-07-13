import asyncio
import importlib
import sys
import threading
from pathlib import Path

import pytest

FIRMWARE_ROOT = Path(__file__).resolve().parents[1] / "firmware" / "esp32"
if str(FIRMWARE_ROOT) not in sys.path:
    sys.path.insert(0, str(FIRMWARE_ROOT))

firmware_main = importlib.import_module("main")
websocket_client = importlib.import_module("comm.websocket_client")

from comm.websocket_client import WebSocketClient, parse_ws_url  # noqa: E402
from media.websocket import OP_CLOSE, OP_CONTINUATION, OP_PING, OP_TEXT  # noqa: E402
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
        self.closed = False

    def send_json(self, payload):
        self.sent.append(payload)
        return True

    def close(self):
        self.closed = True


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
        self.cancel_reason = reason


class FakeReflex:
    def __init__(self, state="none"):
        self.state = state
        self.emergency_stop = state == "emergency_stop"

    def request_emergency_stop(self):
        self.state = "emergency_stop"
        self.emergency_stop = True

    def status(self, motion_state="idle"):
        return {
            "control_authority": "skill",
            "reflex_state": "none",
            "motion_state": motion_state,
            "last_reflex": None,
        }


class PartialSocket:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = []
        self.closed = False

    def recv(self, size):
        if not self.chunks:
            raise TimeoutError()
        chunk = self.chunks.pop(0)
        if len(chunk) > size:
            self.chunks.insert(0, chunk[size:])
            chunk = chunk[:size]
        return chunk

    def send(self, payload):
        self.sent.append(bytes(payload))
        return len(payload)

    def close(self):
        self.closed = True


def server_frame(opcode, payload=b"", *, fin=True):
    return bytes(((0x80 if fin else 0) | opcode, len(payload))) + payload


class FakeDisplay:
    def __init__(self):
        self.statuses = []

    def show_status(self, status):
        self.statuses.append(status)


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


@pytest.mark.asyncio
async def test_blocking_connection_worker_does_not_stall_safety_ticks() -> None:
    connection_worker = importlib.import_module("comm.connection_worker")
    connect_started = threading.Event()
    release_connect = threading.Event()

    class BlockingTransport:
        connected = False

        def connect(self):
            connect_started.set()
            release_connect.wait(timeout=1)
            self.connected = True
            return True

    transport = BlockingTransport()
    worker = connection_worker.ConnectionWorker(lambda: transport)
    connection_task = asyncio.create_task(
        firmware_main.wait_for_connection(worker, poll_interval_ms=1)
    )

    while not connect_started.is_set():
        await asyncio.sleep(0)
    safety_ticks = 0
    for _ in range(6):
        safety_ticks += 1
        await asyncio.sleep(0.005)

    assert safety_ticks == 6
    assert connection_task.done() is False
    release_connect.set()
    result = await asyncio.wait_for(connection_task, timeout=1)
    assert result is transport


def test_websocket_text_frame_is_masked() -> None:
    frame = WebSocketClient("ws://host/ws")._encode_frame(b"hello")

    assert frame[0] == 0x81
    assert frame[1] & 0x80
    assert decode_masked_payload(frame) == b"hello"


def test_control_websocket_preserves_partial_frames_and_reassembles_continuations() -> None:
    raw = (
        server_frame(OP_TEXT, b'{"type":', fin=False)
        + server_frame(OP_PING, b"alive")
        + server_frame(OP_CONTINUATION, b'"heartbeat"}', fin=True)
        + server_frame(OP_CLOSE, b"\x03\xe8")
    )
    sock = PartialSocket((raw[:1], raw[1:7], raw[7:]))
    websocket = WebSocketClient("ws://host/ws")
    websocket.sock = sock
    websocket.connected = True

    assert websocket.recv_json() is None
    assert websocket.recv_json() is None
    assert websocket.recv_json() is None
    assert websocket.recv_json() is None
    assert websocket.recv_json() == {"type": "heartbeat"}
    assert websocket.recv_json() is None
    assert websocket.connected is False
    assert sock.closed is True


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


def test_firmware_protocol_heartbeat_includes_link_health_payload() -> None:
    class Power:
        battery_pct = 77

    class Imu:
        def read_posture(self):
            return "upright"

    class Motion:
        motion_state = "wave"

    protocol = firmware_main.FirmwareProtocol("kt2-test")
    heartbeat = protocol.heartbeat(
        Power(),
        Imu(),
        FakeReflex(),
        Motion(),
        {"agent_online": True, "local_loop_ms": 4},
    )

    assert heartbeat["type"] == "heartbeat"
    assert heartbeat["payload"]["source"] == "firmware"
    assert heartbeat["payload"]["agent_online"] is True
    assert heartbeat["payload"]["local_loop_ms"] == 4
    assert heartbeat["payload"]["control_authority"] == "skill"
    assert heartbeat["payload"]["motion_state"] == "wave"


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


def test_execute_intent_rejects_forbidden_field_anywhere_in_payload() -> None:
    actions = FakeActions()
    ws = FakeWs()
    safety = firmware_main.SafetyGuard(
        type("Power", (), {"is_low": lambda self: False})(),
        type("Imu", (), {"is_fault": lambda self: False})(),
    )
    message = {
        "id": "intent-raw",
        "payload": {
            "metadata": {"framebuffer": [1, 0]},
            "actions": [{"name": "set_face", "args": {"face": "happy"}}],
        },
    }

    firmware_main.execute_intent(
        message,
        actions,
        safety,
        firmware_main.FirmwareProtocol("test"),
        ws,
        state={},
    )

    assert actions.executed == []
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

    state = {"last_host_ts_ms": 1000, "last_host_clock_seen_ms": 1000}
    firmware_main.execute_intent(
        message,
        actions,
        FakeSafety(),
        protocol,
        ws,
        motion=motion,
        state=state,
        clock=lambda: 1000,
    )

    assert motion.submitted == [message]
    assert ws.sent[-1]["type"] == "ack"


def test_execute_intent_rejects_expired_deadline_using_host_heartbeat_clock() -> None:
    actions = FakeActions()
    motion = FakeMotion()
    ws = FakeWs()
    message = {
        "id": "expired",
        "type": "intent",
        "ts_ms": 10_000,
        "deadline_ms": 1_000,
        "payload": {"skills": [{"name": "wave", "args": {"level": 1}}]},
    }
    state = {"last_host_ts_ms": 10_000, "last_host_clock_seen_ms": 500}

    firmware_main.execute_intent(
        message,
        actions,
        FakeSafety(),
        firmware_main.FirmwareProtocol("test"),
        ws,
        motion=motion,
        state=state,
        clock=lambda: 1_601,
    )

    assert motion.submitted == []
    assert ws.sent[-1]["type"] == "error"
    assert ws.sent[-1]["payload"]["code"] == "INTENT_EXPIRED"


def test_execute_intent_without_host_clock_rejects_motion_but_allows_expression() -> None:
    protocol = firmware_main.FirmwareProtocol("test")
    motion = FakeMotion()
    denied_ws = FakeWs()
    firmware_main.execute_intent(
        {"id": "move", "payload": {"skills": [{"name": "wave", "args": {}}]}},
        FakeActions(),
        FakeSafety(),
        protocol,
        denied_ws,
        motion=motion,
        state={},
    )
    assert motion.submitted == []
    assert denied_ws.sent[-1]["payload"]["code"] == "HOST_CLOCK_UNAVAILABLE"

    actions = FakeActions()
    allowed_ws = FakeWs()
    firmware_main.execute_intent(
        {"id": "face", "payload": {"actions": [{"name": "set_face", "args": {"face": "happy"}}]}},
        actions,
        FakeSafety(),
        protocol,
        allowed_ws,
        state={},
    )
    assert actions.executed == [{"name": "set_face", "args": {"face": "happy"}}]
    assert allowed_ws.sent[-1]["type"] == "ack"


@pytest.mark.parametrize("reflex_state", ["emergency_stop", "fall_detected", "recovering", "fault"])
def test_execute_intent_rejects_non_stop_before_ack_during_critical_reflex(reflex_state) -> None:
    actions = FakeActions()
    ws = FakeWs()
    firmware_main.execute_intent(
        {"id": "unsafe-during-reflex", "payload": {"actions": [{"name": "chirp", "args": {}}]}},
        actions,
        FakeSafety(),
        firmware_main.FirmwareProtocol("test"),
        ws,
        reflex=FakeReflex(reflex_state),
        state={},
    )

    assert actions.executed == []
    assert ws.sent[-1]["type"] == "error"
    assert ws.sent[-1]["payload"]["code"] == "REFLEX_ACTIVE"


def test_execute_intent_low_battery_rejects_motion_but_allows_chirp() -> None:
    protocol = firmware_main.FirmwareProtocol("test")
    motion = FakeMotion()
    denied_ws = FakeWs()
    clock_state = {"last_host_ts_ms": 1_000, "last_host_clock_seen_ms": 1_000}
    firmware_main.execute_intent(
        {"id": "move-low", "payload": {"skills": [{"name": "wave", "args": {}}]}},
        FakeActions(),
        FakeSafety(),
        protocol,
        denied_ws,
        motion=motion,
        reflex=FakeReflex("low_battery"),
        state=clock_state,
        clock=lambda: 1_000,
    )
    assert motion.submitted == []
    assert denied_ws.sent[-1]["payload"]["code"] == "REFLEX_ACTIVE"

    actions = FakeActions()
    allowed_ws = FakeWs()
    firmware_main.execute_intent(
        {"id": "chirp-low", "payload": {"actions": [{"name": "chirp", "args": {}}]}},
        actions,
        FakeSafety(),
        protocol,
        allowed_ws,
        reflex=FakeReflex("low_battery"),
        state={},
    )
    assert actions.executed == [{"name": "chirp", "args": {}}]
    assert allowed_ws.sent[-1]["type"] == "ack"


def test_execute_intent_stop_bypasses_expired_deadline_and_active_reflex() -> None:
    actions = FakeActions()
    motion = FakeMotion()
    ws = FakeWs()
    reflex = FakeReflex("fall_detected")
    firmware_main.execute_intent(
        {
            "id": "stop-now",
            "ts_ms": 1,
            "deadline_ms": 1,
            "payload": {"actions": [{"name": "stop", "args": {}}]},
        },
        actions,
        FakeSafety(allowed=False),
        firmware_main.FirmwareProtocol("test"),
        ws,
        motion=motion,
        reflex=reflex,
        state={"last_host_ts_ms": 100_000, "last_host_clock_seen_ms": 0},
        clock=lambda: 10,
    )

    assert motion.cancelled is True
    assert reflex.emergency_stop is True
    assert ws.sent[-1]["type"] == "ack"


def test_semantic_payload_takes_precedence_over_compatibility_actions() -> None:
    payload = {
        "expression": {"emotion": "happy"},
        "skills": [{"name": "wave", "args": {"level": 1}}],
        "actions": [
            {"name": "set_expression", "args": {"emotion": "happy"}},
            {"name": "wave", "args": {"level": 1}},
        ],
    }

    actions = firmware_main._semantic_actions(payload)

    assert actions == [
        {"name": "set_expression", "args": {"emotion": "happy"}},
        {"name": "wave", "args": {"level": 1}},
    ]


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


def test_host_heartbeat_updates_brain_seen_without_action(monkeypatch) -> None:
    actions = FakeActions()
    ws = FakeWs()
    protocol = firmware_main.FirmwareProtocol("kt2-test")
    state = {"agent_online": False, "last_brain_seen_ms": None}
    display = FakeDisplay()
    monkeypatch.setattr(firmware_main, "now_ms", lambda: 1234)

    firmware_main.handle_agent_message(
        {"type": "heartbeat", "payload": {"source": "host", "host_ts_ms": 987654}},
        actions,
        FakeSafety(),
        protocol,
        ws,
        state=state,
        display=display,
    )

    assert state["last_brain_seen_ms"] == 1234
    assert state["last_host_ts_ms"] == 987654
    assert state["last_host_clock_seen_ms"] == 1234
    assert state["agent_online"] is True
    assert actions.executed == []
    assert ws.sent == []
    assert display.statuses == ["agent online"]


def test_non_host_heartbeat_cannot_replace_host_clock_anchor(monkeypatch) -> None:
    state = {
        "agent_online": True,
        "last_brain_seen_ms": 100,
        "last_host_ts_ms": 50_000,
        "last_host_clock_seen_ms": 100,
    }
    monkeypatch.setattr(firmware_main, "now_ms", lambda: 200)

    firmware_main.handle_agent_message(
        {"type": "heartbeat", "payload": {"source": "firmware", "host_ts_ms": 999_999}},
        FakeActions(),
        FakeSafety(),
        firmware_main.FirmwareProtocol("test"),
        FakeWs(),
        state=state,
    )

    assert state["last_brain_seen_ms"] == 200
    assert state["last_host_ts_ms"] == 50_000
    assert state["last_host_clock_seen_ms"] == 100


def test_brain_timeout_cancels_motion_and_marks_offline(monkeypatch) -> None:
    state = {"agent_online": True, "last_brain_seen_ms": 1000}
    display = FakeDisplay()
    motion = FakeMotion()
    monkeypatch.setattr(firmware_main, "now_ms", lambda: 9001)

    firmware_main.check_brain_timeout(state, display, motion)

    assert state["agent_online"] is False
    assert display.statuses == ["agent offline"]
    assert motion.cancelled is True
    assert motion.cancel_reason == "brain_timeout"
