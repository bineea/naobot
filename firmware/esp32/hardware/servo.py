try:
    from machine import PWM, Pin
except ImportError:
    PWM = None
    Pin = None

from config import SERVO_LIMITS, SERVO_PINS


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

    def stop(self):
        for servo in self.servos.values():
            servo.off()
