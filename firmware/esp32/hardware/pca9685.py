try:
    import utime as time
except ImportError:
    import time

from config import PCA9685_ADDR

MODE1 = 0x00
PRESCALE = 0xFE
LED0_ON_L = 0x06
ALL_LED_ON_L = 0xFA


def sleep_ms(ms):
    if hasattr(time, "sleep_ms"):
        time.sleep_ms(ms)
    else:
        time.sleep(ms / 1000)


class PCA9685:
    def __init__(self, i2c, address=PCA9685_ADDR, frequency_hz=50):
        self.i2c = i2c
        self.address = address
        self.frequency_hz = frequency_hz
        self.available = False
        try:
            if i2c is None:
                raise RuntimeError("i2c unavailable")
            if hasattr(i2c, "scan") and address not in i2c.scan():
                raise RuntimeError("pca9685 not found")
            self._set_frequency(frequency_hz)
            self.available = True
        except Exception as exc:
            print("pca9685 fallback:", exc)

    def _write(self, register, data):
        if isinstance(data, int):
            data = bytes((data,))
        self.i2c.writeto_mem(self.address, register, data)

    def _set_frequency(self, frequency_hz):
        prescale = round(25_000_000 / (4096 * frequency_hz)) - 1
        prescale = max(3, min(255, prescale))
        self._write(MODE1, 0x10)
        self._write(PRESCALE, prescale)
        self._write(MODE1, 0x20)
        sleep_ms(1)
        self._write(MODE1, 0xA0)

    def set_channel(self, channel, pulse):
        if not self.available:
            return False
        channel = int(channel)
        if channel < 0 or channel > 15:
            raise ValueError("invalid pca9685 channel")
        pulse = max(0, min(4095, int(pulse)))
        register = LED0_ON_L + channel * 4
        self._write(register, bytes((0, 0, pulse & 0xFF, (pulse >> 8) & 0x0F)))
        return True

    def set_angle(self, channel, degrees):
        degrees = max(0, min(180, int(degrees)))
        pulse_us = 500 + (degrees * 2000 / 180)
        pulse = round(pulse_us * self.frequency_hz * 4096 / 1_000_000)
        return self.set_channel(channel, pulse)

    def all_off(self):
        if not self.available:
            return False
        self._write(ALL_LED_ON_L, b"\x00\x00\x00\x10")
        return True
