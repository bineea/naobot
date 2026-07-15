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
        self._active_reflex = None
        self.emergency_stop = False
        self.shutdown_succeeded = None
        self.shutdown_failed_latched = False

    def request_emergency_stop(self):
        if hasattr(self.actions, "emergency_stop"):
            shutdown_succeeded = self.actions.emergency_stop()
        else:
            shutdown_succeeded = self.actions.stop()
        self.shutdown_succeeded = bool(shutdown_succeeded)
        self.shutdown_failed_latched = not self.shutdown_succeeded
        self.emergency_stop = self.shutdown_succeeded
        self.state = "emergency_stop" if self.shutdown_succeeded else "fault"
        self.authority = "emergency"
        return self.shutdown_succeeded

    def check(self):
        if self.shutdown_failed_latched:
            self.state = "fault"
            self.authority = "emergency"
            return True
        if self.emergency_stop:
            self.state = "emergency_stop"
            self.authority = "emergency"
            return True
        if self.imu.is_fault():
            self.state = "fall_detected"
            self.authority = "reflex"
            return True
        if self.power.is_low():
            self.state = "low_battery"
            self.authority = "reflex"
            return True
        if self.state in ("fall_detected", "recovering", "low_battery"):
            self.state = "recovered"
            self.authority = "idle"
            self._active_reflex = None
        elif self.state != "recovered":
            self.state = "none"
            self.authority = "idle"
        return False

    def run(self):
        if self.state == "emergency_stop":
            if self._active_reflex != "emergency_stop":
                self.display.set_face("alert")
                self.last_reflex = "emergency_stop"
                self._active_reflex = "emergency_stop"
            return True
        if self.state == "low_battery":
            if self._active_reflex != "low_battery":
                self.last_reflex = run_low_battery_reflex(
                    self.power,
                    self.imu,
                    self.actions,
                    self.display,
                    self.buzzer,
                )
                self._active_reflex = "low_battery"
                self._record_shutdown_result(self.last_reflex)
            return True
        if self.state == "fall_detected":
            if self._active_reflex != "fall_detected":
                self.last_reflex = run_fall_reflex(self.actions, self.display, self.buzzer)
                self._active_reflex = "fall_detected"
                self._record_shutdown_result(self.last_reflex)
            return True
        if self.state == "fault":
            return True
        return False

    def _record_shutdown_result(self, reflex_name):
        self.shutdown_succeeded = not reflex_name.endswith("shutdown_failed")
        if not self.shutdown_succeeded:
            self.shutdown_failed_latched = True
            self.state = "fault"
            self.authority = "emergency"

    def status(self, motion_state="idle"):
        return {
            "control_authority": self.authority,
            "reflex_state": self.state,
            "motion_state": motion_state,
            "last_reflex": self.last_reflex,
            "shutdown_succeeded": self.shutdown_succeeded,
        }
