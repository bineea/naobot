import sys
from pathlib import Path

FIRMWARE_ROOT = Path(__file__).resolve().parents[1] / "firmware" / "esp32"
if str(FIRMWARE_ROOT) not in sys.path:
    sys.path.insert(0, str(FIRMWARE_ROOT))

from control.motion_controller import MotionController  # noqa: E402
from motion.action_player import ActionPlayer  # noqa: E402
from reflex.reflex_controller import ReflexController  # noqa: E402


class FakePower:
    def __init__(self, low=False):
        self.battery_pct = 5 if low else 80
        self.low = low

    def is_low(self):
        return self.low


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

    def pose(self, positions):
        self.calls.append(("pose", dict(positions)))

    def sequence(self, frames, delay_ms=180):
        self.calls.append(("sequence", [dict(frame) for frame in frames], delay_ms))

    def stop(self):
        self.calls.append(("stop",))


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

    assert ("stop",) in servos.calls
    assert "alert" in display.faces
    assert "alert" in buzzer.tones
    assert reflex.status()["control_authority"] == "reflex"
    assert reflex.status()["last_reflex"] == "brace_and_sit"


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
