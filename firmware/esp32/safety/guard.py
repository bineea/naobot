MOVEMENT_ACTIONS = ("wave", "small_step_forward", "turn_left", "turn_right", "gentle_nudge")
ALLOWED_FACES = ("idle", "happy", "sad", "dizzy", "sleepy", "alert")
ALLOWED_EMOTIONS = (
    "idle",
    "happy",
    "sad",
    "dizzy",
    "sleepy",
    "alert",
    "curious",
    "confused",
    "proud",
    "shy",
)
ALLOWED_TONES = ("soft", "happy", "alert", "low_battery")
FORBIDDEN_FIELDS = (
    "raw",
    "servo",
    "angle",
    "pwm",
    "servo_id",
    "current",
    "torque",
    "grip_force",
    "pixels",
    "framebuffer",
)
ACTION_ARGS = {
    "set_face": ("face",),
    "set_expression": (
        "emotion",
        "valence",
        "arousal",
        "eye_open",
        "pupil_offset_x",
        "blink_rate",
        "duration_ms",
    ),
    "blink": (),
    "wave": ("level",),
    "small_step_forward": ("steps",),
    "turn_left": ("steps",),
    "turn_right": ("steps",),
    "gentle_nudge": ("level",),
    "sit": (),
    "chirp": ("tone",),
    "sleep": (),
    "stop": (),
}


def _contains_forbidden_field(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).lower() in FORBIDDEN_FIELDS or _contains_forbidden_field(nested):
                return True
    elif isinstance(value, (list, tuple)):
        for item in value:
            if _contains_forbidden_field(item):
                return True
    return False


def _number_in_range(value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return minimum <= value <= maximum


def _integer_in_range(value, minimum, maximum):
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum


class SafetyGuard:
    def __init__(self, power, imu):
        self.power = power
        self.imu = imu

    def can_emit_event(self, event):
        if self.power.is_low() and event not in ("battery_low", "touch_head", "touch_back"):
            return False
        return True

    def can_execute(self, action):
        if not isinstance(action, dict) or _contains_forbidden_field(action):
            return False
        name = action.get("name")
        args = action.get("args", {})
        if name not in ACTION_ARGS or not isinstance(args, dict):
            return False
        if any(key not in ACTION_ARGS[name] for key in args):
            return False
        if name == "stop":
            return True
        if self.power.is_low() and name in MOVEMENT_ACTIONS:
            return False
        if self.imu.is_fault() and name in MOVEMENT_ACTIONS:
            return False
        if name == "set_face":
            return args.get("face") in ALLOWED_FACES
        if name == "set_expression":
            return self._validate_expression(args)
        if name == "chirp":
            return args.get("tone", "soft") in ALLOWED_TONES
        if name == "wave":
            return _integer_in_range(args.get("level", 1), 1, 2)
        if name in ("small_step_forward", "turn_left", "turn_right"):
            return _integer_in_range(args.get("steps", 1), 1, 3)
        if name == "gentle_nudge":
            return _integer_in_range(args.get("level", 1), 1, 1)
        return True

    def can_accept_payload(self, payload):
        return isinstance(payload, dict) and not _contains_forbidden_field(payload)

    @staticmethod
    def _validate_expression(args):
        if args.get("emotion", "idle") not in ALLOWED_EMOTIONS:
            return False
        ranges = {
            "valence": (-1.0, 1.0),
            "arousal": (0.0, 1.0),
            "eye_open": (0.0, 1.0),
            "pupil_offset_x": (-1.0, 1.0),
            "blink_rate": (0.0, 1.0),
        }
        for key, bounds in ranges.items():
            if key in args and not _number_in_range(args[key], bounds[0], bounds[1]):
                return False
        if "duration_ms" in args and not _integer_in_range(args["duration_ms"], 0, 5000):
            return False
        return True
