from config import LOW_BATTERY_PCT


class PowerMonitor:
    def __init__(self):
        self.battery_pct = 80
        self.charging = False

    def is_low(self):
        return self.battery_pct <= LOW_BATTERY_PCT
