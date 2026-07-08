try:
    from machine import PWM, Pin
except ImportError:
    PWM = None
    Pin = None

from config import SERVO_LIMITS, SERVO_PINS

try:
    import utime as time
except ImportError:
    import time


def sleep_ms(ms):
    if hasattr(time, "sleep_ms"):
        time.sleep_ms(ms)
    else:
        time.sleep(ms / 1000)


class Servo:
    def __init__(self, pin):
        self.pin = pin
        self.angle_value = SERVO_LIMITS["neutral"]
        self.pwm = PWM(Pin(pin), freq=50) if PWM and Pin else None

    def angle(self, degrees):
        degrees = max(SERVO_LIMITS["min"], min(SERVO_LIMITS["max"], int(degrees)))
        self.angle_value = degrees
        if self.pwm:
            us = 500 + (degrees / 180) * 2000
            duty = int(us / 20000 * 65535)
            self.pwm.duty_u16(duty)

    def off(self):
        if self.pwm:
            self.pwm.duty_u16(0)


class ServoBank:
    def __init__(self):
        self.servos = {name: Servo(pin) for name, pin in SERVO_PINS.items()}

    def neutral(self):
        for servo in self.servos.values():
            servo.angle(SERVO_LIMITS["neutral"])

    def set_all(self, degrees):
        for servo in self.servos.values():
            servo.angle(degrees)

    def set(self, name, degrees):
        servo = self.servos.get(name)
        if not servo:
            raise ValueError(f"unknown servo: {name}")
        servo.angle(degrees)

    def pose(self, positions):
        for name, degrees in positions.items():
            self.set(name, degrees)

    def sequence(self, frames, delay_ms=180):
        for frame in frames:
            self.pose(frame)
            sleep_ms(delay_ms)

    def stop(self):
        for servo in self.servos.values():
            servo.off()
