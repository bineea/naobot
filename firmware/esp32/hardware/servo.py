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


class ServoBank:
    CHANNELS = {"lf": 0, "rf": 1, "lr": 2, "rr": 3}
    MIN_ANGLE = 30
    MAX_ANGLE = 150
    NEUTRAL_ANGLE = 90

    def __init__(self, i2c=None, pin_factory=Pin):
        self.enabled = False
        self.available = False
        self.emergency_latched = False
        self.positions = {name: self.NEUTRAL_ANGLE for name in self.CHANNELS}
        self._oe = None
        self._init_oe(pin_factory)
        self.i2c = i2c if i2c is not None else SharedI2C.get()
        self.driver = PCA9685(self.i2c)
        self.available = self._oe is not None and self.driver.available

    def _init_oe(self, pin_factory):
        if pin_factory is None:
            return
        try:
            self._oe = pin_factory(PCA9685_OE_PIN, pin_factory.OUT)
            self._set_oe(True)
        except Exception as exc:
            self._oe = None
            print("servo oe fallback:", exc)

    def _set_oe(self, disabled):
        if self._oe is None:
            return False
        self._oe.value(1 if disabled else 0)
        return True

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
        self.enabled = False
        try:
            return self._set_oe(True)
        except Exception as exc:
            print("servo disable failed:", exc)
            return False

    def neutral(self):
        return self.set_all(self.NEUTRAL_ANGLE)

    def _write_positions(self, positions):
        if self.emergency_latched or not self.available:
            return False
        self.disable()
        try:
            for name, degrees in positions.items():
                self.driver.set_angle(self.CHANNELS[name], degrees)
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
        self.disable()
        try:
            self.driver.all_off()
        except Exception as exc:
            print("servo clear failed:", exc)
        return True

    def emergency_off(self):
        self.emergency_latched = True
        return self.stop()
