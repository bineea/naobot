import sys
from pathlib import Path

import pytest

FIRMWARE_ROOT = Path(__file__).resolve().parents[1] / "firmware" / "esp32"
if str(FIRMWARE_ROOT) not in sys.path:
    sys.path.insert(0, str(FIRMWARE_ROOT))

from control.motion_controller import MotionController  # noqa: E402
from motion.action_player import ActionPlayer  # noqa: E402
from reflex.reflex_controller import ReflexController  # noqa: E402
from safety.guard import SafetyGuard  # noqa: E402


class FakePower:
    def __init__(self, low=False, critical=False):
        self.battery_pct = 5 if low else 80
        self.low = low
        self.critical = critical

    def is_low(self):
        return self.low

    def is_critical(self):
        return self.critical


class FakeImu:
    def __init__(self, fault=False):
        self.fault = fault

    def is_fault(self):
        return self.fault

    def read_posture(self):
        return "fallen" if self.fault else "upright"


class FakeServos:
    def __init__(self):
        self.calls = []
        self.enabled = False
        self.emergency_latched = False

    def pose(self, positions):
        if self.emergency_latched:
            return False
        self.enabled = True
        self.calls.append(("pose", dict(positions)))
        return True

    def sequence(self, frames, delay_ms=180):
        self.calls.append(("sequence", [dict(frame) for frame in frames], delay_ms))

    def stop(self):
        self.enabled = False
        self.calls.append(("stop",))

    def emergency_off(self):
        self.emergency_latched = True
        self.enabled = False
        self.calls.append(("emergency_off",))


class FakeDisplay:
    def __init__(self):
        self.faces = []
        self.expressions = []

    def set_face(self, face):
        self.faces.append(face)

    def set_expression(self, params):
        self.expressions.append(dict(params))


class FakeBuzzer:
    def __init__(self):
        self.tones = []

    def chirp(self, tone="soft"):
        self.tones.append(tone)


class FakeSafety:
    def can_execute(self, action):
        return action.get("name") != "unsafe"


def test_reflex_controller_runs_fall_reflex_locally() -> None:
    servos = FakeServos()
    display = FakeDisplay()
    buzzer = FakeBuzzer()
    actions = ActionPlayer(servos, display, buzzer)
    reflex = ReflexController(FakePower(), FakeImu(fault=True), actions, display, buzzer)

    assert reflex.check() is True
    assert reflex.run() is True

    assert ("emergency_off",) in servos.calls
    assert not any(call[0] == "pose" for call in servos.calls)
    assert "alert" in display.faces
    assert "alert" in buzzer.tones
    assert reflex.status()["control_authority"] == "reflex"
    assert reflex.status()["last_reflex"] == "fall_emergency_off"


def test_emergency_stop_latches_servo_output_before_display() -> None:
    events = []

    class OrderedServos(FakeServos):
        def emergency_off(self):
            super().emergency_off()
            events.append("emergency_off")

    class OrderedDisplay(FakeDisplay):
        def set_face(self, face):
            super().set_face(face)
            events.append("display")

    servos = OrderedServos()
    display = OrderedDisplay()
    actions = ActionPlayer(servos, display, FakeBuzzer())
    reflex = ReflexController(FakePower(), FakeImu(), actions, display)

    reflex.request_emergency_stop()
    reflex.run()

    assert events[0] == "emergency_off"
    assert events[1] == "display"
    assert servos.emergency_latched is True


def test_low_battery_stops_immediately_without_claiming_sit() -> None:
    servos = FakeServos()
    display = FakeDisplay()
    actions = ActionPlayer(servos, display, FakeBuzzer())
    reflex = ReflexController(FakePower(low=True), FakeImu(), actions, display)

    assert reflex.check() and reflex.run()

    assert [call[0] for call in servos.calls] == ["emergency_off"]
    assert servos.enabled is False
    assert reflex.last_reflex == "low_battery_stop"


def test_critical_battery_never_reenables_servo_for_sit() -> None:
    servos = FakeServos()
    display = FakeDisplay()
    actions = ActionPlayer(servos, display, FakeBuzzer())
    reflex = ReflexController(
        FakePower(low=True, critical=True),
        FakeImu(),
        actions,
        display,
    )

    assert reflex.check() and reflex.run()

    assert [call[0] for call in servos.calls] == ["emergency_off"]
    assert reflex.last_reflex == "low_battery_stop"


def test_reflex_controller_can_trigger_same_fall_again_after_recovery() -> None:
    servos = FakeServos()
    display = FakeDisplay()
    buzzer = FakeBuzzer()
    imu = FakeImu(fault=True)
    actions = ActionPlayer(servos, display, buzzer)
    reflex = ReflexController(FakePower(), imu, actions, display, buzzer)

    assert reflex.check() and reflex.run()
    imu.fault = False
    assert reflex.check() is False
    assert reflex.last_reflex == "fall_emergency_off"
    imu.fault = True
    assert reflex.check() and reflex.run()

    assert buzzer.tones.count("alert") == 2
    assert reflex.last_reflex == "fall_emergency_off"


def test_reflex_controller_can_trigger_low_battery_again_after_recovery() -> None:
    servos = FakeServos()
    display = FakeDisplay()
    buzzer = FakeBuzzer()
    power = FakePower(low=True)
    actions = ActionPlayer(servos, display, buzzer)
    reflex = ReflexController(power, FakeImu(), actions, display, buzzer)

    assert reflex.check() and reflex.run()
    power.low = False
    assert reflex.check() is False
    power.low = True
    assert reflex.check() and reflex.run()

    assert buzzer.tones.count("low_battery") == 2


@pytest.mark.parametrize(
    "action",
    [
        {"name": "wave", "args": {"level": 0}},
        {"name": "wave", "args": {"level": 3}},
        {"name": "small_step_forward", "args": {"steps": 0}},
        {"name": "turn_left", "args": {"steps": 4}},
        {"name": "set_face", "args": {"face": "unknown"}},
        {"name": "chirp", "args": {"tone": "loud"}},
        {"name": "set_expression", "args": {"emotion": "happy", "valence": 1.1}},
        {"name": "set_expression", "args": {"emotion": "happy", "duration_ms": 5001}},
        {"name": "blink", "args": {"unexpected": True}},
        {"name": "wave", "args": {"nested": {"servo_id": 1}}},
        {"name": "set_expression", "args": {"pixels": [[1, 0]]}},
    ],
)
def test_safety_guard_rejects_invalid_parameters_and_recursive_raw_fields(action) -> None:
    guard = SafetyGuard(FakePower(), FakeImu())
    assert guard.can_execute(action) is False


@pytest.mark.parametrize(
    "action",
    [
        {"name": "wave", "args": {"level": 2}},
        {"name": "turn_right", "args": {"steps": 3}},
        {"name": "set_face", "args": {"face": "happy"}},
        {
            "name": "set_expression",
            "args": {
                "emotion": "curious",
                "valence": -1.0,
                "arousal": 1.0,
                "eye_open": 0.0,
                "pupil_offset_x": 1.0,
                "blink_rate": 0.0,
                "duration_ms": 5000,
            },
        },
        {"name": "chirp", "args": {"tone": "low_battery"}},
    ],
)
def test_safety_guard_accepts_bounded_parameters(action) -> None:
    guard = SafetyGuard(FakePower(), FakeImu())
    assert guard.can_execute(action) is True


def test_motion_controller_cancels_running_skill_when_reflex_triggers() -> None:
    servos = FakeServos()
    display = FakeDisplay()
    buzzer = FakeBuzzer()
    actions = ActionPlayer(servos, display, buzzer)
    imu = FakeImu(fault=False)
    reflex = ReflexController(FakePower(), imu, actions, display, buzzer)
    current_time = {"value": 0}
    motion = MotionController(actions, FakeSafety(), reflex, lambda: current_time["value"])

    accepted, reason = motion.submit_action({"name": "small_step_forward", "args": {"steps": 3}})
    assert accepted, reason
    assert motion.is_running()

    imu.fault = True
    current_time["value"] += 50
    motion.tick()

    assert not motion.is_running()
    assert motion.motion_state == "cancelled"
    assert ("stop",) in servos.calls


def test_motion_controller_does_not_queue_compatibility_actions_twice() -> None:
    actions = ActionPlayer(FakeServos(), FakeDisplay(), FakeBuzzer())
    reflex = ReflexController(FakePower(), FakeImu(), actions, FakeDisplay(), FakeBuzzer())
    motion = MotionController(actions, FakeSafety(), reflex, lambda: 0)
    message = {
        "payload": {
            "expression": {"emotion": "happy"},
            "skills": [{"name": "wave", "args": {"level": 1}}],
            "actions": [
                {"name": "set_expression", "args": {"emotion": "happy"}},
                {"name": "wave", "args": {"level": 1}},
            ],
        }
    }

    accepted, reason = motion.submit_intent(message)

    assert accepted, reason
    assert motion.current.name == "set_expression"
    assert [skill.name for skill in motion.queue] == ["wave"]


def test_motion_controller_accepts_null_skills_with_compatibility_actions() -> None:
    actions = ActionPlayer(FakeServos(), FakeDisplay(), FakeBuzzer())
    reflex = ReflexController(FakePower(), FakeImu(), actions, FakeDisplay(), FakeBuzzer())
    motion = MotionController(actions, FakeSafety(), reflex, lambda: 0)

    accepted, reason = motion.submit_intent(
        {"payload": {"expression": None, "skills": None, "actions": [{"name": "blink", "args": {}}]}}
    )

    assert accepted, reason
    assert motion.current.name == "blink"
