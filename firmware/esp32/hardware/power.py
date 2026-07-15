from config import (
    BATTERY_CRITICAL_PCT,
    BATTERY_LOW_PCT,
    BATTERY_WARN_PCT,
    BQ25895_ADDR,
    BQ27441_ADDR,
)
from hardware.i2c import SharedI2C


class PowerMonitor:
    def __init__(self, i2c=None):
        self.i2c = i2c if i2c is not None else SharedI2C.get()
        self._set_unknown()
        self.sample()

    def _set_unknown(self):
        self.battery_pct = None
        self.voltage_mv = None
        self.current_ma = None
        self.charging = None
        self.external_power = None
        self.fault = "unknown"
        self.available = False
        self.level = "unknown"

    def _read_u16(self, address, register):
        data = self.i2c.readfrom_mem(address, register, 2)
        return data[0] | (data[1] << 8)

    def _read_s16(self, address, register):
        value = self._read_u16(address, register)
        return value - 65536 if value & 0x8000 else value

    def sample(self):
        try:
            if self.i2c is None:
                raise RuntimeError("i2c unavailable")
            if hasattr(self.i2c, "scan"):
                devices = self.i2c.scan()
                if BQ27441_ADDR not in devices or BQ25895_ADDR not in devices:
                    raise RuntimeError("power devices not found")
            battery_pct = self._read_u16(BQ27441_ADDR, 0x1C)
            voltage_mv = self._read_u16(BQ27441_ADDR, 0x04)
            current_ma = self._read_s16(BQ27441_ADDR, 0x10)
            status = self.i2c.readfrom_mem(BQ25895_ADDR, 0x0B, 1)[0]
            fault_register = self.i2c.readfrom_mem(BQ25895_ADDR, 0x0C, 1)[0]
            charge_state = (status >> 3) & 0x03
            vbus_state = (status >> 5) & 0x07

            self.battery_pct = max(0, min(100, battery_pct))
            self.voltage_mv = voltage_mv
            self.current_ma = current_ma
            self.charging = charge_state in (1, 2)
            self.external_power = vbus_state not in (0, 7)
            self.fault = bool(fault_register)
            self.available = True
            self.level = self._level_for(self.battery_pct)
        except Exception as exc:
            self._set_unknown()
            print("power fallback:", exc)
        return self.snapshot()

    @staticmethod
    def _level_for(battery_pct):
        if battery_pct <= BATTERY_CRITICAL_PCT:
            return "critical"
        if battery_pct <= BATTERY_LOW_PCT:
            return "low"
        if battery_pct <= BATTERY_WARN_PCT:
            return "warning"
        return "normal"

    def snapshot(self):
        return {
            "battery_pct": self.battery_pct,
            "voltage_mv": self.voltage_mv,
            "current_ma": self.current_ma,
            "charging": self.charging,
            "external_power": self.external_power,
            "fault": self.fault,
            "available": self.available,
            "level": self.level,
        }

    def is_low(self):
        return not self.available or self.battery_pct <= BATTERY_LOW_PCT

    def is_critical(self):
        return not self.available or self.battery_pct <= BATTERY_CRITICAL_PCT
