class EventAdapter:
    def __init__(self, touch, imu, power):
        self.touch = touch
        self.imu = imu
        self.power = power
        self.low_sent = False
        self.fault_active = False

    def poll(self):
        if self.power.is_low() and not self.low_sent:
            self.low_sent = True
            return "battery_low"
        touch_event = self.touch.poll()
        if touch_event:
            return touch_event
        is_fault = self.imu.is_fault()
        if is_fault and not self.fault_active:
            self.fault_active = True
            return "fall_detected"
        if not is_fault:
            self.fault_active = False
        return None
