class LocalFallback:
    def __init__(self, display, actions):
        self.display = display
        self.actions = actions

    def handle(self, event):
        if event == "touch_head":
            self.display.set_face("happy")
        elif event == "touch_back":
            self.display.blink()
        elif event == "battery_low":
            self.display.set_face("sleepy")
        elif event == "fall_detected":
            self.actions.stop()
            self.display.set_face("alert")
