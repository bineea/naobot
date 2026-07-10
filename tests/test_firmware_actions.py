import sys
from pathlib import Path

FIRMWARE_ROOT = Path(__file__).resolve().parents[1] / "firmware" / "esp32"
if str(FIRMWARE_ROOT) not in sys.path:
    sys.path.insert(0, str(FIRMWARE_ROOT))

from motion.action_player import ActionPlayer  # noqa: E402


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
        self.blinks = 0

    def set_face(self, face):
        self.faces.append(face)

    def set_expression(self, params):
        self.expressions.append(dict(params))

    def blink(self):
        self.blinks += 1


class FakeBuzzer:
    def __init__(self):
        self.tones = []

    def chirp(self, tone="soft"):
        self.tones.append(tone)
        return True


def make_player():
    servos = FakeServos()
    display = FakeDisplay()
    buzzer = FakeBuzzer()
    return ActionPlayer(servos, display, buzzer), servos, display, buzzer


def test_all_host_actions_are_implemented() -> None:
    player, servos, display, buzzer = make_player()
    actions = [
        {"name": "set_face", "args": {"face": "happy"}},
        {"name": "set_expression", "args": {"emotion": "curious", "eye_open": 0.8}},
        {"name": "blink", "args": {}},
        {"name": "wave", "args": {"level": 1}},
        {"name": "small_step_forward", "args": {"steps": 1}},
        {"name": "turn_left", "args": {"steps": 1}},
        {"name": "turn_right", "args": {"steps": 1}},
        {"name": "gentle_nudge", "args": {"level": 1}},
        {"name": "sit", "args": {}},
        {"name": "chirp", "args": {"tone": "happy"}},
        {"name": "sleep", "args": {}},
        {"name": "stop", "args": {}},
    ]

    results = [player.execute(action) for action in actions]

    assert all(result.accepted for result in results)
    assert "happy" in display.faces
    assert display.expressions == [{"emotion": "curious", "eye_open": 0.8}]
    assert display.blinks == 1
    assert ("pose", {"lf": 78, "rf": 102, "lr": 78, "rr": 102}) in servos.calls
    assert "happy" in buzzer.tones
    assert ("stop",) in servos.calls


def test_wave_level_two_has_more_frames_than_level_one() -> None:
    player, servos, _, _ = make_player()

    assert player.execute({"name": "wave", "args": {"level": 1}}).accepted
    level_one = servos.calls[-1][1]
    assert player.execute({"name": "wave", "args": {"level": 2}}).accepted
    level_two = servos.calls[-1][1]

    assert len(level_two) > len(level_one)
    assert level_one[-1] == {"lf": 90, "rf": 90, "lr": 90, "rr": 90}
    assert level_two[-1] == {"lf": 90, "rf": 90, "lr": 90, "rr": 90}


def test_steps_are_clamped_to_one_through_three() -> None:
    player, servos, _, _ = make_player()

    assert player.execute({"name": "small_step_forward", "args": {"steps": 99}}).accepted
    frames = servos.calls[-1][1]

    assert len(frames) == 12
    assert frames[-1] == {"lf": 90, "rf": 90, "lr": 90, "rr": 90}


def test_chirp_supports_all_host_tones() -> None:
    player, _, _, buzzer = make_player()

    for tone in ("soft", "happy", "alert", "low_battery"):
        assert player.execute({"name": "chirp", "args": {"tone": tone}}).accepted

    assert buzzer.tones == ["soft", "happy", "alert", "low_battery"]


def test_unknown_action_returns_failure() -> None:
    player, _, _, _ = make_player()

    result = player.execute({"name": "not_real", "args": {}})

    assert not result.accepted
    assert "未实现" in result.reason
