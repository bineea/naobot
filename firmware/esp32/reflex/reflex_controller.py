from reflex.fall_reflex import run_fall_reflex
from reflex.low_battery_reflex import run_low_battery_reflex


class ReflexController:
    def __init__(self, power, imu, actions, display, buzzer=None):
        self.power = power
        self.imu = imu
        self.actions = actions
        self.display = display
        self.buzzer = buzzer
        self.state = "none"
        self.authority = "idle"
        self.last_reflex = None
        self.emergency_stop = False

    def request_emergency_stop(self):
        self.emergency_stop = True
        self.state = "emergency_stop"
        self.authority = "emergency"

    def check(self):
        if self.emergency_stop:
            self.state = "emergency_stop"
            self.authority = "emergency"
            return True
        if self.power.is_low():
            self.state = "low_battery"
            self.authority = "reflex"
            return True
        if self.imu.is_fault():
            self.state = "fall_detected"
            self.authority = "reflex"
            return True
        if self.state in ("fall_detected", "recovering", "low_battery"):
            self.state = "recovered"
            self.authority = "idle"
        elif self.state != "recovered":
            self.state = "none"
            self.authority = "idle"
        return False

    def run(self):
        if self.state == "emergency_stop":
            self.actions.stop()
            self.display.set_face("alert")
            self.last_reflex = "emergency_stop"
            return True
        if self.state == "low_battery":
            if self.last_reflex != "low_battery_sit":
                self.last_reflex = run_low_battery_reflex(self.actions, self.display, self.buzzer)
            return True
        if self.state == "fall_detected":
            if self.last_reflex != "brace_and_sit":
                self.last_reflex = run_fall_reflex(self.actions, self.display, self.buzzer)
            return True
        return False

    def status(self, motion_state="idle"):
        return {
            "control_authority": self.authority,
            "reflex_state": self.state,
            "motion_state": motion_state,
            "last_reflex": self.last_reflex,
        }
