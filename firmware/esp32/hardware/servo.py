try:
    from machine import Pin
except (ImportError, RuntimeError):
    Pin = None

from config import PCA9685_OE_PIN
from hardware.i2c import SharedI2C
from hardware.pca9685 import PCA9685

try:
    import utime as time
except ImportError:
    import time


def sleep_ms(ms):
    if hasattr(time, "sleep_ms"):
        time.sleep_ms(ms)
    else:
        time.sleep(ms / 1000)


class ServoOutputGate:
    def __init__(self, pin_factory=Pin):
        self._pin = None
        self.available = False
        self.disabled = None
        if pin_factory is None:
            return
        try:
            self._pin = pin_factory(PCA9685_OE_PIN, pin_factory.OUT)
            if not self.set_disabled(True):
                raise RuntimeError("servo oe did not go high")
        except Exception as exc:
            self.available = False
            print("servo oe fallback:", exc)

    def set_disabled(self, disabled):
        if self._pin is None:
            return False
        try:
            self._pin.value(1 if disabled else 0)
        except Exception as exc:
            print("servo oe write failed:", exc)
            return False
        self.disabled = bool(disabled)
        self.available = True
        return True

    def confirm_disabled(self):
        if self._pin is None or not self.available:
            return None
        try:
            value = self._pin.value()
        except Exception as exc:
            self.disabled = None
            print("servo oe feedback failed:", exc)
            return None
        if value not in (0, 1):
            self.disabled = None
            return None
        self.disabled = value == 1
        return self.disabled


class ServoBank:
    CHANNELS = {"lf": 0, "rf": 1, "lr": 2, "rr": 3}
    MIN_ANGLE = 30
    MAX_ANGLE = 150
    NEUTRAL_ANGLE = 90

    def __init__(self, i2c=None, pin_factory=Pin, output_gate=None):
        self.enabled = False
        self.available = False
        self.emergency_latched = False
        self._emergency_shutdown_result = None
        self.positions = {name: self.NEUTRAL_ANGLE for name in self.CHANNELS}
        self.output_gate = output_gate or ServoOutputGate(pin_factory=pin_factory)
        self._oe = self.output_gate._pin
        self.i2c = i2c if i2c is not None else SharedI2C.get()
        driver_i2c = self.i2c if self.output_gate.available else None
        self.driver = PCA9685(driver_i2c)
        self.available = self.output_gate.available and self.driver.available

    def _set_oe(self, disabled):
        return self.output_gate.set_disabled(disabled)

    @classmethod
    def _clamp(cls, degrees):
        return max(cls.MIN_ANGLE, min(cls.MAX_ANGLE, int(degrees)))

    def enable(self):
        if not self.available or self.emergency_latched:
            return False
        try:
            if not self._set_oe(False):
                return False
            self.enabled = True
            return True
        except Exception as exc:
            self.enabled = False
            print("servo enable failed:", exc)
            return False

    def disable(self):
        if not self._set_oe(True):
            return False
        self.enabled = False
        return True

    def neutral(self):
        return self.set_all(self.NEUTRAL_ANGLE)

    def _write_positions(self, positions):
        if self.emergency_latched or not self.available:
            return False
        if not self.disable():
            return False
        try:
            for name, degrees in positions.items():
                if not self.driver.set_angle(self.CHANNELS[name], degrees):
                    return False
        except Exception as exc:
            print("servo write failed:", exc)
            return False
        self.positions.update(positions)
        return self.enable()

    def set_all(self, degrees):
        if self.emergency_latched or not self.available:
            return False
        degrees = self._clamp(degrees)
        return self._write_positions({name: degrees for name in self.CHANNELS})

    def set(self, name, degrees):
        if name not in self.CHANNELS:
            raise ValueError(f"unknown servo: {name}")
        if self.emergency_latched or not self.available:
            return False
        degrees = self._clamp(degrees)
        return self._write_positions({name: degrees})

    def pose(self, positions):
        if self.emergency_latched or not self.available:
            return False
        clamped = {}
        for name, degrees in positions.items():
            if name not in self.CHANNELS:
                raise ValueError(f"unknown servo: {name}")
            clamped[name] = self._clamp(degrees)
        return self._write_positions(clamped)

    def sequence(self, frames, delay_ms=180):
        for frame in frames:
            if not self.pose(frame):
                return False
            sleep_ms(delay_ms)
        return True

    def stop(self):
        if self.emergency_latched or not self.available:
            return False
        if not self.disable():
            return False
        try:
            return self.driver.all_off()
        except Exception as exc:
            print("servo clear failed:", exc)
            return False

    def emergency_off(self):
        if self.emergency_latched:
            return bool(self._emergency_shutdown_result)
        disabled = self.disable()
        cleared = False
        if disabled:
            try:
                cleared = self.driver.all_off()
            except Exception as exc:
                print("servo emergency clear failed:", exc)
        self.emergency_latched = True
        self._emergency_shutdown_result = bool(disabled and cleared)
        return self._emergency_shutdown_result
