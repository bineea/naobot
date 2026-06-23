MOVEMENT_ACTIONS = ("wave", "small_step_forward", "turn_left", "turn_right", "gentle_nudge")


class ActionPlayer:
    def __init__(self, servos, display):
        self.servos = servos
        self.display = display

    def execute(self, action):
        name = action.get("name")
        args = action.get("args", {})
        if name == "set_face":
            self.display.set_face(args.get("face", "idle"))
        elif name == "blink":
            self.display.blink()
        elif name == "wave":
            self.servos.set_all(100)
            self.servos.neutral()
        elif name == "sit":
            self.servos.set_all(80)
        elif name == "stop":
            self.stop()
        else:
            print("ignored action:", name)

    def stop(self):
        self.servos.stop()
