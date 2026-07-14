import sys
from pathlib import Path

FIRMWARE_ROOT = Path(__file__).resolve().parents[1] / "firmware" / "esp32"
if str(FIRMWARE_ROOT) not in sys.path:
    sys.path.insert(0, str(FIRMWARE_ROOT))

from control.motion_controller import MotionController  # noqa: E402
from hardware.buzzer import TONE_PATTERNS  # noqa: E402
from hardware.display import FACE_ANIMATIONS  # noqa: E402
from motion.action_player import (  # noqa: E402
    ActionPlayer,
    BuzzerSkill,
    DisplaySkill,
    ImmediateSkill,
)
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


class TickDisplay:
    """记录 render_frame 调用的 display 替身，用于验证 DisplaySkill 逐帧推进。"""

    def __init__(self, face="idle"):
        self.face = face
        self.frames = []

    def render_frame(self, frame, status=None):
        self.frames.append(frame)


class TickBuzzer:
    """记录 play_step/off 调用的 buzzer 替身，用于验证 BuzzerSkill 逐段推进。"""

    def __init__(self):
        self.steps = []

    def play_step(self, freq, duration_ms):
        self.steps.append(("step", freq, duration_ms))

    def off(self):
        self.steps.append(("off",))


class FakeSafety:
    def can_execute(self, action):
        return action.get("name") != "unsafe"


def test_display_skill_advances_frames_on_tick() -> None:
    display = TickDisplay()
    frames, delay_ms = FACE_ANIMATIONS["idle"]
    skill = DisplaySkill(display, "set_face", frames, delay_ms)

    skill.start(0)
    assert skill.running is True
    assert display.frames == []  # start 不渲染，等 tick

    assert skill.tick(0) is False
    assert display.frames == ["idle"]
    assert skill.tick(109) is False
    assert display.frames == ["idle"]
    assert skill.tick(110) is False
    assert display.frames == ["idle", "idle_left"]
    assert skill.tick(220) is False
    assert display.frames == ["idle", "idle_left", "idle_right"]
    assert skill.tick(330) is True
    assert display.frames == ["idle", "idle_left", "idle_right", "idle"]
    assert skill.running is False


def test_display_skill_non_animated_face_completes_in_one_frame() -> None:
    display = TickDisplay()
    skill = DisplaySkill(display, "set_face", ("dizzy",), 0)

    skill.start(0)
    assert skill.tick(0) is True
    assert display.frames == ["dizzy"]
    assert skill.running is False


def test_display_skill_blink_renders_two_frames() -> None:
    display = TickDisplay(face="happy")
    skill = DisplaySkill(display, "blink", ("blink", "happy"), 80)

    skill.start(0)
    assert skill.tick(0) is False
    assert display.frames == ["blink"]
    assert skill.tick(80) is True
    assert display.frames == ["blink", "happy"]
    assert skill.running is False


def test_display_skill_cancel_stops_without_clearing() -> None:
    display = TickDisplay()
    frames, delay_ms = FACE_ANIMATIONS["idle"]
    skill = DisplaySkill(display, "set_face", frames, delay_ms)

    skill.start(0)
    skill.tick(0)
    assert display.frames == ["idle"]
    skill.cancel()
    assert skill.running is False
    # cancel 后 tick 不再渲染
    assert skill.tick(500) is True
    assert display.frames == ["idle"]


def test_buzzer_skill_steps_through_pattern() -> None:
    buzzer = TickBuzzer()
    pattern = TONE_PATTERNS["low_battery"]  # ((900,180),(0,90),(900,180))
    skill = BuzzerSkill(buzzer, "chirp", pattern)

    skill.start(0)
    assert buzzer.steps == [("step", 900, 180)]
    assert skill.tick(179) is False
    assert skill.tick(180) is False
    assert buzzer.steps[-1] == ("step", 0, 90)
    assert skill.tick(270) is False
    assert buzzer.steps[-1] == ("step", 900, 180)
    assert skill.tick(450) is True
    assert buzzer.steps[-1] == ("off",)
    assert skill.running is False


def test_buzzer_skill_cancel_silences_immediately() -> None:
    buzzer = TickBuzzer()
    skill = BuzzerSkill(buzzer, "chirp", TONE_PATTERNS["alert"])

    skill.start(0)
    assert buzzer.steps == [("step", 2600, 100)]
    skill.cancel()
    assert buzzer.steps[-1] == ("off",)
    assert skill.running is False
    assert skill.tick(500) is True


def test_build_skill_routes_set_face_to_display_skill() -> None:
    player = ActionPlayer(FakeServos(), TickDisplay(), TickBuzzer())
    skill = player.build_skill("set_face", {"face": "happy"})
    assert isinstance(skill, DisplaySkill)
    assert skill.frames == FACE_ANIMATIONS["happy"][0]
    assert skill.delay_ms == FACE_ANIMATIONS["happy"][1]


def test_build_skill_routes_blink_to_display_skill() -> None:
    display = TickDisplay(face="happy")
    player = ActionPlayer(FakeServos(), display, TickBuzzer())
    skill = player.build_skill("blink", {})
    assert isinstance(skill, DisplaySkill)
    assert skill.frames == ("blink", "happy")


def test_build_skill_routes_chirp_to_buzzer_skill() -> None:
    player = ActionPlayer(FakeServos(), TickDisplay(), TickBuzzer())
    skill = player.build_skill("chirp", {"tone": "alert"})
    assert isinstance(skill, BuzzerSkill)
    assert skill.pattern == TONE_PATTERNS["alert"]


def test_build_skill_chirp_without_buzzer_falls_back_to_immediate() -> None:
    player = ActionPlayer(FakeServos(), TickDisplay(), buzzer=None)
    skill = player.build_skill("chirp", {"tone": "soft"})
    assert isinstance(skill, ImmediateSkill)


def test_build_skill_set_expression_stays_immediate() -> None:
    player = ActionPlayer(FakeServos(), TickDisplay(), TickBuzzer())
    skill = player.build_skill("set_expression", {"emotion": "happy"})
    assert isinstance(skill, ImmediateSkill)


def test_motion_controller_tick_drives_display_skill_to_completion() -> None:
    display = TickDisplay()
    actions = ActionPlayer(FakeServos(), display, TickBuzzer())
    reflex = ReflexController(FakePower(), FakeImu(), actions, display, TickBuzzer())
    current_time = {"value": 0}
    motion = MotionController(actions, FakeSafety(), reflex, lambda: current_time["value"])

    accepted, _ = motion.submit_action({"name": "set_face", "args": {"face": "alert"}})
    assert accepted
    assert motion.is_running()

    for t in (0, 70, 140, 210):
        current_time["value"] = t
        motion.tick()

    assert not motion.is_running()
    assert display.frames == ["alert_left", "alert_right", "alert_left", "alert"]


def test_motion_controller_tick_drives_buzzer_skill_to_completion() -> None:
    buzzer = TickBuzzer()
    actions = ActionPlayer(FakeServos(), TickDisplay(), buzzer)
    reflex = ReflexController(FakePower(), FakeImu(), actions, TickDisplay(), buzzer)
    current_time = {"value": 0}
    motion = MotionController(actions, FakeSafety(), reflex, lambda: current_time["value"])

    accepted, _ = motion.submit_action({"name": "chirp", "args": {"tone": "soft"}})
    assert accepted
    assert motion.is_running()

    current_time["value"] = 80
    motion.tick()
    assert not motion.is_running()
    assert buzzer.steps[-1] == ("off",)


def test_reflex_cancel_interrupts_display_skill() -> None:
    display = TickDisplay()
    actions = ActionPlayer(FakeServos(), display, TickBuzzer())
    imu = FakeImu(fault=False)
    reflex = ReflexController(FakePower(), imu, actions, display, TickBuzzer())
    current_time = {"value": 0}
    motion = MotionController(actions, FakeSafety(), reflex, lambda: current_time["value"])

    motion.submit_action({"name": "set_face", "args": {"face": "idle"}})
    current_time["value"] = 0
    motion.tick()
    assert display.frames == ["idle"]

    imu.fault = True
    current_time["value"] = 50
    motion.tick()
    assert not motion.is_running()
    assert motion.motion_state == "cancelled"

    current_time["value"] = 300
    motion.tick()
    assert display.frames == ["idle"]  # cancel 后不再渲染


def test_reflex_cancel_interrupts_buzzer_skill() -> None:
    buzzer = TickBuzzer()
    actions = ActionPlayer(FakeServos(), TickDisplay(), buzzer)
    imu = FakeImu(fault=False)
    reflex = ReflexController(FakePower(), imu, actions, TickDisplay(), buzzer)
    current_time = {"value": 0}
    motion = MotionController(actions, FakeSafety(), reflex, lambda: current_time["value"])

    motion.submit_action({"name": "chirp", "args": {"tone": "low_battery"}})
    assert buzzer.steps == [("step", 900, 180)]

    imu.fault = True
    current_time["value"] = 50
    motion.tick()
    assert not motion.is_running()
    assert buzzer.steps[-1] == ("off",)
