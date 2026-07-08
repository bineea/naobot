try:
    from machine import PWM, Pin
except ImportError:
    PWM = None
    Pin = None

try:
    import utime as time
except ImportError:
    import time

from config import BUZZER_PIN

TONE_PATTERNS = {
    "soft": ((1600, 80),),
    "happy": ((1800, 70), (2200, 90)),
    "alert": ((2600, 100), (0, 60), (2600, 100)),
    "low_battery": ((900, 180), (0, 90), (900, 180)),
}


def sleep_ms(ms):
    if hasattr(time, "sleep_ms"):
        time.sleep_ms(ms)
    else:
        time.sleep(ms / 1000)


class Buzzer:
    def __init__(self, pin=BUZZER_PIN):
        self.pin = pin
        self.pwm = PWM(Pin(pin), freq=1000) if PWM and Pin else None
        if self.pwm:
            self.pwm.duty_u16(0)

    def chirp(self, tone="soft"):
        pattern = TONE_PATTERNS.get(tone, TONE_PATTERNS["soft"])
        if not self.pwm:
            print("chirp:", tone)
            return True
        for freq, duration_ms in pattern:
            if freq:
                self.pwm.freq(freq)
                self.pwm.duty_u16(12000)
            else:
                self.pwm.duty_u16(0)
            sleep_ms(duration_ms)
        self.pwm.duty_u16(0)
        return True
