MOVEMENT_ACTIONS = ("wave", "small_step_forward", "turn_left", "turn_right", "gentle_nudge")

NEUTRAL = {"lf": 90, "rf": 90, "lr": 90, "rr": 90}
SIT = {"lf": 78, "rf": 102, "lr": 78, "rr": 102}
FORWARD_A = {"lf": 118, "rf": 62, "lr": 70, "rr": 110}
FORWARD_B = {"lf": 70, "rf": 110, "lr": 118, "rr": 62}
TURN_LEFT_A = {"lf": 62, "rf": 62, "lr": 118, "rr": 118}
TURN_LEFT_B = {"lf": 96, "rf": 118, "lr": 80, "rr": 62}
TURN_RIGHT_A = {"lf": 118, "rf": 118, "lr": 62, "rr": 62}
TURN_RIGHT_B = {"lf": 80, "rf": 62, "lr": 96, "rr": 118}
NUDGE_A = {"lf": 112, "rf": 68, "lr": 112, "rr": 68}
NUDGE_B = {"lf": 78, "rf": 102, "lr": 78, "rr": 102}


class ActionResult:
    def __init__(self, accepted, reason=""):
        self.accepted = accepted
        self.reason = reason


class ActionPlayer:
    def __init__(self, servos, display, buzzer=None):
        self.servos = servos
        self.display = display
        self.buzzer = buzzer

    def execute(self, action):
        name = action.get("name")
        args = action.get("args", {})
        try:
            if name == "set_face":
                return self._set_face(args)
            if name == "blink":
                self.display.blink()
                return ActionResult(True)
            if name == "wave":
                return self._wave(args)
            if name == "small_step_forward":
                return self._small_step_forward(args)
            if name == "turn_left":
                return self._turn(args, left=True)
            if name == "turn_right":
                return self._turn(args, left=False)
            if name == "gentle_nudge":
                return self._gentle_nudge(args)
            if name == "sit":
                self.servos.pose(SIT)
                return ActionResult(True)
            if name == "chirp":
                return self._chirp(args)
            if name == "sleep":
                return self._sleep()
            if name == "stop":
                self.stop()
                return ActionResult(True)
        except Exception as exc:
            return ActionResult(False, str(exc))
        return ActionResult(False, f"未实现动作: {name}")

    def _set_face(self, args):
        self.display.set_face(args.get("face", "idle"))
        return ActionResult(True)

    def _wave(self, args):
        level = clamp_int(args.get("level", 1), 1, 2)
        high = 130 if level == 2 else 118
        low = 64 if level == 2 else 74
        cycles = 3 if level == 2 else 2
        frames = []
        for _ in range(cycles):
            frames.append({"lf": 90, "rf": high, "lr": 90, "rr": 90})
            frames.append({"lf": 90, "rf": low, "lr": 90, "rr": 90})
        frames.append(NEUTRAL)
        self.servos.sequence(frames, delay_ms=140)
        return ActionResult(True)

    def _small_step_forward(self, args):
        steps = clamp_int(args.get("steps", 1), 1, 3)
        frames = []
        for _ in range(steps):
            frames.extend((FORWARD_A, NEUTRAL, FORWARD_B, NEUTRAL))
        self.servos.sequence(frames, delay_ms=170)
        return ActionResult(True)

    def _turn(self, args, left):
        steps = clamp_int(args.get("steps", 1), 1, 3)
        frames = []
        first = TURN_LEFT_A if left else TURN_RIGHT_A
        second = TURN_LEFT_B if left else TURN_RIGHT_B
        for _ in range(steps):
            frames.extend((first, NEUTRAL, second, NEUTRAL))
        self.servos.sequence(frames, delay_ms=170)
        return ActionResult(True)

    def _gentle_nudge(self, args):
        clamp_int(args.get("level", 1), 1, 1)
        self.servos.sequence((NUDGE_A, NUDGE_B, NEUTRAL), delay_ms=130)
        return ActionResult(True)

    def _chirp(self, args):
        tone = args.get("tone", "soft")
        if not self.buzzer:
            print("chirp:", tone)
            return ActionResult(True)
        self.buzzer.chirp(tone)
        return ActionResult(True)

    def _sleep(self):
        self.display.set_face("sleepy")
        if self.buzzer:
            self.buzzer.chirp("soft")
        self.servos.pose(SIT)
        self.stop()
        return ActionResult(True)

    def stop(self):
        self.servos.stop()


def clamp_int(value, minimum, maximum):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = minimum
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value
