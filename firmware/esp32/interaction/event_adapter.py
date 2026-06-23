class EventAdapter:
    def __init__(self, touch, imu, power):
        self.touch = touch
        self.imu = imu
        self.power = power
        self.low_sent = False

    def poll(self):
        if self.power.is_low() and not self.low_sent:
            self.low_sent = True
            return "battery_low"
        touch_event = self.touch.poll()
        if touch_event:
            return touch_event
        if self.imu.is_fault():
            return "fall_detected"
        return None
