try:
    import utime as time
except ImportError:
    import time


HEALTHY_WINDOW_MS = 10000
VERIFY_DEADLINE_MS = 30000
SAFE_POSTURES = ("upright", "sitting")


def now_ms():
    if hasattr(time, "ticks_ms"):
        return time.ticks_ms()
    return int(time.time() * 1000)


def ticks_diff(end, start):
    if hasattr(time, "ticks_diff"):
        return time.ticks_diff(end, start)
    return end - start


class BootHealthMonitor:
    def __init__(
        self,
        ota_module,
        power,
        imu,
        motion,
        servo_gate,
        clock_ms=now_ms,
    ):
        self.ota = ota_module
        self.power = power
        self.imu = imu
        self.motion = motion
        self.servo_gate = servo_gate
        self.clock_ms = clock_ms
        self._state = "idle"
        self._error = None
        self._pending = False
        self._pending_sequence = None
        self._phase = None
        self._running_target = False
        self._pending_started_ms = None
        self._healthy_started_ms = None

    def tick(self):
        try:
            self._tick_pending()
        except Exception as exc:
            error = str(exc) or "boot health monitor failed"
            if self._is_protected_boot():
                current_ms = self.clock_ms()
                if (
                    self._pending_started_ms is not None
                    and ticks_diff(current_ms, self._pending_started_ms)
                    >= VERIFY_DEADLINE_MS
                ):
                    self._rollback("boot health deadline after exception: " + error)
                else:
                    self._state = "error"
                    self._error = error
                    self._healthy_started_ms = None
            else:
                self._state = "error"
                self._error = error
                self._healthy_started_ms = None
        return self.status()

    def status(self):
        return {
            "state": self._state,
            "error": self._error,
            "pending_verify": self._pending,
            "pending_sequence": self._pending_sequence,
            "phase": self._phase,
            "running_target": self._running_target,
        }

    def _tick_pending(self):
        pending_error = None
        try:
            pending = self.ota.pending_verify()
        except Exception as exc:
            pending = "unknown"
            pending_error = str(exc) or "pending verify state unavailable"
        if pending is None:
            pending = "unknown"
            pending_error = "pending verify state unavailable"
        self._pending = pending if pending in (True, False) else "unknown"

        metadata_error = None
        try:
            self._pending_sequence = self.ota.pending_sequence()
            self._phase = self.ota.phase()
        except Exception as exc:
            self._pending_sequence = "unknown"
            self._phase = "unknown"
            metadata_error = str(exc) or "OTA transaction metadata unavailable"
        try:
            running_target = self.ota.running_target()
            self._running_target = (
                running_target if running_target in (True, False) else "unknown"
            )
            if self._running_target == "unknown" and metadata_error is None:
                metadata_error = "OTA target partition state unavailable"
        except Exception as exc:
            self._running_target = "unknown"
            if metadata_error is None:
                metadata_error = str(exc) or "OTA target partition state unavailable"

        if not self._is_protected_boot():
            self._pending = False
            self._pending_started_ms = None
            self._healthy_started_ms = None
            if self._state != "healthy":
                self._state = "not_pending"
                self._error = None
            return

        current_ms = self.clock_ms()
        if self._pending_started_ms is None:
            self._pending_started_ms = current_ms
        self.motion.cancel("ota_pending_verify")

        if getattr(self.servo_gate, "available", False) is not True:
            self._rollback("OE unavailable during pending verify")
            return
        if self.servo_gate.set_disabled(True) is not True:
            self._rollback("OE disable failed during pending verify")
            return
        if self.servo_gate.confirm_disabled() is not True:
            self._rollback("OE disable readback failed during pending verify")
            return

        if self._phase == "rollback":
            self._rollback("persisted OTA rollback phase")
            return
        if (
            self._phase == "activated"
            and self._pending is False
            and self._running_target is False
        ):
            self._healthy_started_ms = None
            self._state = "waiting_reboot"
            self._error = "activated OTA target is not running"
            return

        power = self.power.snapshot()
        if power.get("available") is not True or power.get("fault") is not False:
            self._rollback("critical power fault during pending verify")
            return
        posture = getattr(self.imu, "posture", "unknown")
        if posture == "fallen":
            self._rollback("unsafe IMU posture during pending verify")
            return

        if ticks_diff(current_ms, self._pending_started_ms) >= VERIFY_DEADLINE_MS:
            self._rollback("pending verify health deadline exceeded")
            return
        if self._phase == "confirming" and self._pending is False:
            if self._running_target is not True:
                self._healthy_started_ms = None
                self._state = "error"
                self._error = "confirming OTA target is not running"
                return
            self._mark_healthy()
            return
        if (
            self._pending == "unknown"
            or metadata_error is not None
            or self._running_target is not True
        ):
            self._healthy_started_ms = None
            self._state = "error"
            self._error = pending_error or metadata_error or "OTA boot state unavailable"
            return
        if getattr(self.imu, "available", False) is not True or posture not in SAFE_POSTURES:
            self._healthy_started_ms = None
            self._state = "monitoring"
            self._error = "IMU posture not yet safe"
            return

        if self._healthy_started_ms is None:
            self._healthy_started_ms = current_ms
        self._state = "monitoring"
        self._error = None
        if ticks_diff(current_ms, self._healthy_started_ms) < HEALTHY_WINDOW_MS:
            return
        self._mark_healthy()

    def _mark_healthy(self):
        try:
            marked = self.ota.mark_healthy()
        except Exception as exc:
            self._state = "error"
            self._error = str(exc) or "mark healthy failed"
            return
        if marked is not True:
            self._state = "error"
            self._error = "mark healthy failed"
            return
        self._pending = False
        try:
            self._pending_sequence = self.ota.pending_sequence()
            self._phase = self.ota.phase()
            self._running_target = self.ota.running_target()
        except Exception:
            self._pending_sequence = "unknown"
            self._phase = "unknown"
            self._running_target = "unknown"
        self._state = "healthy"
        self._error = None

    def _is_protected_boot(self):
        return (
            self._pending in (True, "unknown")
            or self._pending_sequence not in (None, False)
            or self._phase in ("prepared", "activated", "confirming", "rollback", "unknown")
        )

    def _rollback(self, reason):
        self._state = "rollback"
        self._error = reason
        self._healthy_started_ms = None
        try:
            self.ota.rollback_and_reboot()
        except Exception as exc:
            self._state = "error"
            detail = str(exc) or "rollback failed"
            self._error = reason + "; rollback failed: " + detail
