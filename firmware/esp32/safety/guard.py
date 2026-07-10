MOVEMENT_ACTIONS = ("wave", "small_step_forward", "turn_left", "turn_right", "gentle_nudge")


class SafetyGuard:
    def __init__(self, power, imu):
        self.power = power
        self.imu = imu

    def can_emit_event(self, event):
        if self.power.is_low() and event not in ("battery_low", "touch_head", "touch_back"):
            return False
        return True

    def can_execute(self, action):
        name = action.get("name")
        if name == "stop":
            return True
        if self.power.is_low() and name in MOVEMENT_ACTIONS:
            return False
        if self.imu.is_fault() and name in MOVEMENT_ACTIONS:
            return False
        return name in (
            "set_face",
            "set_expression",
            "blink",
            "wave",
            "small_step_forward",
            "turn_left",
            "turn_right",
            "gentle_nudge",
            "sit",
            "chirp",
            "sleep",
            "stop",
        )
