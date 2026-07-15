from hardware.buzzer import TONE_PATTERNS
from hardware.display import BLINK_DELAY_MS, FACE_ANIMATIONS

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


class PoseSkill:
    def __init__(self, servos, name, frames, delay_ms=160):
        self.servos = servos
        self.name = name
        self.frames = list(frames)
        self.delay_ms = delay_ms
        self.index = 0
        self.last_tick_ms = 0
        self.running = False

    def start(self, now_ms):
        self.index = 0
        self.last_tick_ms = now_ms - self.delay_ms
        self.running = True

    def tick(self, now_ms):
        if not self.running:
            return True
        if now_ms - self.last_tick_ms < self.delay_ms:
            return False
        if self.index >= len(self.frames):
            self.running = False
            return True
        self.servos.pose(self.frames[self.index])
        self.index += 1
        self.last_tick_ms = now_ms
        if self.index >= len(self.frames):
            self.running = False
            return True
        return False

    def cancel(self):
        self.running = False
        self.servos.stop()


class ImmediateSkill:
    def __init__(self, actions, action):
        self.actions = actions
        self.action = action
        self.name = action.get("name")
        self.running = False
        self.result = None

    def start(self, now_ms):
        self.running = True
        self.result = self.actions.execute(self.action)
        self.running = False

    def tick(self, now_ms):
        return True

    def cancel(self):
        self.running = False


class DisplaySkill:
    """OLED 多帧动画的 tick/cancel 化执行。start 只设状态不渲染（仿 PoseSkill），
    由 MotionController.tick 逐帧推进，避免动作队列里的 set_face/blink 阻塞安全循环。"""

    def __init__(self, display, name, frames, delay_ms):
        self.display = display
        self.name = name
        self.frames = tuple(frames)
        self.delay_ms = delay_ms
        self.index = 0
        self.last_tick_ms = 0
        self.running = False

    def start(self, now_ms):
        self.index = 0
        self.last_tick_ms = now_ms - self.delay_ms
        self.running = True

    def tick(self, now_ms):
        if not self.running:
            return True
        if self.index >= len(self.frames):
            self.running = False
            return True
        if now_ms - self.last_tick_ms < self.delay_ms:
            return False
        self.display.render_frame(self.frames[self.index])
        self.index += 1
        self.last_tick_ms = now_ms
        if self.index >= len(self.frames):
            self.running = False
            return True
        return False

    def cancel(self):
        self.running = False


class BuzzerSkill:
    """蜂鸣器多段音型的 tick/cancel 化执行。start 立即播放第一段，tick 按段时长推进，
    cancel 立即静音。避免动作队列里的 chirp 阻塞安全循环。"""

    def __init__(self, buzzer, name, pattern):
        self.buzzer = buzzer
        self.name = name
        self.pattern = tuple(pattern)
        self.index = 0
        self.last_tick_ms = 0
        self.running = False

    def start(self, now_ms):
        self.index = 0
        self.last_tick_ms = now_ms
        self.running = True
        if self.pattern:
            self.buzzer.play_step(*self.pattern[0])

    def tick(self, now_ms):
        if not self.running:
            return True
        if self.index >= len(self.pattern):
            self.running = False
            return True
        _freq, duration_ms = self.pattern[self.index]
        if now_ms - self.last_tick_ms < duration_ms:
            return False
        self.index += 1
        self.last_tick_ms = now_ms
        if self.index >= len(self.pattern):
            self.buzzer.off()
            self.running = False
            return True
        self.buzzer.play_step(*self.pattern[self.index])
        return False

    def cancel(self):
        self.running = False
        self.buzzer.off()


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
            if name == "set_expression":
                return self._set_expression(args)
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
                if not self.servos.pose(SIT):
                    return ActionResult(False, "servo pose failed")
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

    def build_skill(self, name, args=None):
        args = args or {}
        action = {"name": name, "args": args}
        if name == "wave":
            level = clamp_int(args.get("level", 1), 1, 2)
            high = 130 if level == 2 else 118
            low = 64 if level == 2 else 74
            cycles = 3 if level == 2 else 2
            frames = []
            for _ in range(cycles):
                frames.append({"lf": 90, "rf": high, "lr": 90, "rr": 90})
                frames.append({"lf": 90, "rf": low, "lr": 90, "rr": 90})
            frames.append(NEUTRAL)
            return PoseSkill(self.servos, name, frames, 140)
        if name == "small_step_forward":
            steps = clamp_int(args.get("steps", 1), 1, 3)
            frames = []
            for _ in range(steps):
                frames.extend((FORWARD_A, NEUTRAL, FORWARD_B, NEUTRAL))
            return PoseSkill(self.servos, name, frames, 170)
        if name == "turn_left" or name == "turn_right":
            steps = clamp_int(args.get("steps", 1), 1, 3)
            first = TURN_LEFT_A if name == "turn_left" else TURN_RIGHT_A
            second = TURN_LEFT_B if name == "turn_left" else TURN_RIGHT_B
            frames = []
            for _ in range(steps):
                frames.extend((first, NEUTRAL, second, NEUTRAL))
            return PoseSkill(self.servos, name, frames, 170)
        if name == "gentle_nudge":
            return PoseSkill(self.servos, name, (NUDGE_A, NUDGE_B, NEUTRAL), 130)
        if name == "set_face":
            face = args.get("face", "idle")
            if face not in FACE_ANIMATIONS:
                frames, delay_ms = ((face,), 0)
            else:
                frames, delay_ms = FACE_ANIMATIONS[face]
            return DisplaySkill(self.display, name, frames, delay_ms)
        if name == "blink":
            current_face = getattr(self.display, "face", "idle")
            return DisplaySkill(self.display, name, ("blink", current_face), BLINK_DELAY_MS)
        if name == "chirp":
            tone = args.get("tone", "soft")
            pattern = TONE_PATTERNS.get(tone, TONE_PATTERNS["soft"])
            if self.buzzer:
                return BuzzerSkill(self.buzzer, name, pattern)
        return ImmediateSkill(self, action)

    def _set_face(self, args):
        self.display.set_face(args.get("face", "idle"))
        return ActionResult(True)

    def _set_expression(self, args):
        if hasattr(self.display, "set_expression"):
            self.display.set_expression(args)
        else:
            self.display.set_face(args.get("emotion", "idle"))
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

    def emergency_stop(self):
        if hasattr(self.servos, "emergency_off"):
            self.servos.emergency_off()
        else:
            self.servos.stop()

    @property
    def emergency_latched(self):
        return bool(getattr(self.servos, "emergency_latched", False))

    @property
    def servo_output_enabled(self):
        return bool(getattr(self.servos, "enabled", False))


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
