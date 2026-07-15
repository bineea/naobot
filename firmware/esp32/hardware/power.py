from config import (
    BATTERY_CELL_CRITICAL_MV,
    BATTERY_CELL_LOW_MV,
    BATTERY_CELL_MAX_MV,
    BATTERY_CELL_MIN_MV,
    BATTERY_CELL_WARN_MV,
    BATTERY_CRITICAL_PCT,
    BATTERY_LOW_PCT,
    BATTERY_SERIES_COUNT,
    BATTERY_SERIES_MAX,
    BATTERY_SERIES_MIN,
    BATTERY_WARN_PCT,
    BQ34Z100_ADDR,
    INA226_ADDR,
    INA226_CALIBRATION,
    INA226_CONFIG,
    INA226_CONVERSION_READY_MS,
    INA226_CURRENT_LSB_UA,
    POWER_SAMPLE_INTERVAL_MS,
)
from hardware.i2c import SharedI2C

try:
    import utime as time
except ImportError:
    import time


BQ34Z100_SOC = 0x02
BQ34Z100_VOLTAGE = 0x08
BQ34Z100_AVERAGE_CURRENT = 0x0A
BQ34Z100_FLAGS = 0x0E
BQ34Z100_SAFETY_FLAGS = 0xFC00

INA226_CONFIGURATION = 0x00
INA226_BUS_VOLTAGE = 0x02
INA226_CURRENT = 0x04
INA226_CALIBRATION_REGISTER = 0x05
INA226_MASK_ENABLE = 0x06
INA226_BUS_VOLTAGE_LSB_UV = 1250
INA226_MASK_CVRF = 1 << 3
INA226_MASK_OVF = 1 << 2
CURRENT_POLARITY_CONFLICT_MIN_MA = 100


def now_ms():
    if hasattr(time, "ticks_ms"):
        return time.ticks_ms()
    return int(time.time() * 1000)


def ticks_diff(end, start):
    if hasattr(time, "ticks_diff"):
        return time.ticks_diff(end, start)
    return end - start


def ticks_add(start, delta):
    if hasattr(time, "ticks_add"):
        return time.ticks_add(start, delta)
    return start + delta


class PowerMonitor:
    def __init__(self, i2c=None, series_count=BATTERY_SERIES_COUNT, clock_ms=now_ms):
        self.i2c = i2c if i2c is not None else SharedI2C.get()
        self.series_count = series_count
        self._clock_ms = clock_ms
        self._last_sample_ms = None
        self._ina_configured = False
        self._ina_ready_at_ms = None
        self._set_unknown("unknown")
        self.sample(force=True)

    def _set_unknown(self, fault, source="none"):
        self.battery_pct = None
        self.soc_precise = False
        self.pack_voltage_mv = None
        self.cell_voltage_mv = None
        self.current_ma = None
        self.power_mw = None
        self.charging = None
        self.flags = None
        self.fault = fault
        self.available = False
        self.source = source
        self.level = "unknown"

    def _read_le_u16(self, address, register):
        data = self.i2c.readfrom_mem(address, register, 2)
        return data[0] | (data[1] << 8)

    def _read_le_s16(self, address, register):
        value = self._read_le_u16(address, register)
        return value - 65536 if value & 0x8000 else value

    def _read_be_u16(self, address, register):
        data = self.i2c.readfrom_mem(address, register, 2)
        return (data[0] << 8) | data[1]

    def _read_be_s16(self, address, register):
        value = self._read_be_u16(address, register)
        return value - 65536 if value & 0x8000 else value

    def _write_be_u16(self, address, register, value):
        self.i2c.writeto_mem(address, register, bytes(((value >> 8) & 0xFF, value & 0xFF)))

    def _read_bq34z100(self):
        battery_pct = self.i2c.readfrom_mem(BQ34Z100_ADDR, BQ34Z100_SOC, 1)[0]
        if battery_pct > 100:
            raise ValueError("invalid bq34z100 soc")
        return {
            "battery_pct": battery_pct,
            "pack_voltage_mv": self._read_le_u16(BQ34Z100_ADDR, BQ34Z100_VOLTAGE),
            "current_ma": self._read_le_s16(BQ34Z100_ADDR, BQ34Z100_AVERAGE_CURRENT),
            "flags": self._read_le_u16(BQ34Z100_ADDR, BQ34Z100_FLAGS),
        }

    def _read_ina226(self, sample_ms):
        if not self._ina_configured:
            self._write_be_u16(INA226_ADDR, INA226_CONFIGURATION, INA226_CONFIG)
            self._write_be_u16(
                INA226_ADDR,
                INA226_CALIBRATION_REGISTER,
                INA226_CALIBRATION,
            )
            self._ina_configured = True
            self._ina_ready_at_ms = ticks_add(sample_ms, INA226_CONVERSION_READY_MS)
            return {"status": "pending"}
        if ticks_diff(sample_ms, self._ina_ready_at_ms) < 0:
            return {"status": "pending"}

        mask = self._read_be_u16(INA226_ADDR, INA226_MASK_ENABLE)
        if mask & INA226_MASK_OVF:
            return {"status": "overflow"}
        if not mask & INA226_MASK_CVRF:
            return {"status": "pending"}

        bus_raw = self._read_be_u16(INA226_ADDR, INA226_BUS_VOLTAGE)
        current_raw = self._read_be_s16(INA226_ADDR, INA226_CURRENT)
        pack_voltage_mv = (bus_raw * INA226_BUS_VOLTAGE_LSB_UV + 500) // 1000
        current_ma = (current_raw * INA226_CURRENT_LSB_UA) // 1000
        return {
            "status": "ready",
            "pack_voltage_mv": pack_voltage_mv,
            "current_ma": current_ma,
            "power_mw": (pack_voltage_mv * current_ma) // 1000,
        }

    def _try_read(self, address, reader, devices):
        if devices is not None and address not in devices:
            return None
        try:
            return reader()
        except Exception as exc:
            print("power device fallback:", hex(address), exc)
            return None

    def sample(self, force=False):
        sample_ms = self._clock_ms()
        if (
            not force
            and self._last_sample_ms is not None
            and ticks_diff(sample_ms, self._last_sample_ms) < POWER_SAMPLE_INTERVAL_MS
        ):
            return self.snapshot()
        if not isinstance(self.series_count, int) or isinstance(self.series_count, bool):
            self._set_unknown("invalid_series_count")
            return self.snapshot()
        if not BATTERY_SERIES_MIN <= self.series_count <= BATTERY_SERIES_MAX:
            self._set_unknown("invalid_series_count")
            return self.snapshot()
        if self.i2c is None:
            self._set_unknown("power_devices_unavailable")
            return self.snapshot()

        devices = None
        if hasattr(self.i2c, "scan"):
            try:
                devices = self.i2c.scan()
            except Exception as exc:
                print("power scan fallback:", exc)

        bq = self._try_read(BQ34Z100_ADDR, self._read_bq34z100, devices)
        ina_result = self._try_read(
            INA226_ADDR,
            lambda: self._read_ina226(sample_ms),
            devices,
        )
        if ina_result is not None and ina_result["status"] == "overflow":
            source = "bq34z100+ina226" if bq is not None else "ina226_voltage_fallback"
            self._set_unknown("ina226_overflow", source)
            self._last_sample_ms = sample_ms
            return self.snapshot()
        ina_pending = ina_result is not None and ina_result["status"] == "pending"
        ina = ina_result if ina_result is not None and ina_result["status"] == "ready" else None
        if bq is None and ina is None:
            if ina_pending:
                self._set_unknown(False, "ina226_voltage_fallback")
                return self.snapshot()
            self._set_unknown("power_devices_unavailable")
            self._last_sample_ms = sample_ms
            return self.snapshot()

        if bq is not None and ina is not None:
            source = "bq34z100+ina226"
        elif bq is not None:
            source = "bq34z100"
        else:
            source = "ina226_voltage_fallback"

        measurement = ina if ina is not None else bq
        self.pack_voltage_mv = measurement["pack_voltage_mv"]
        self.cell_voltage_mv = (
            self.pack_voltage_mv + self.series_count // 2
        ) // self.series_count
        self.current_ma = measurement["current_ma"]
        self.power_mw = measurement.get("power_mw")
        if self.power_mw is None:
            self.power_mw = (self.pack_voltage_mv * self.current_ma) // 1000
        self.charging = bq["current_ma"] > 0 if bq is not None else self.current_ma < 0
        self.source = source
        self.flags = bq["flags"] if bq is not None else None
        self.battery_pct = bq["battery_pct"] if bq is not None else None
        self.soc_precise = bq is not None

        if (
            bq is not None
            and ina is not None
            and abs(bq["current_ma"]) >= CURRENT_POLARITY_CONFLICT_MIN_MA
            and abs(ina["current_ma"]) >= CURRENT_POLARITY_CONFLICT_MIN_MA
            and bq["current_ma"] * ina["current_ma"] > 0
        ):
            self._set_unknown("current_polarity_conflict", source)
            self._last_sample_ms = sample_ms
            return self.snapshot()

        pack_min_mv = BATTERY_CELL_MIN_MV * self.series_count
        pack_max_mv = BATTERY_CELL_MAX_MV * self.series_count
        if not pack_min_mv <= self.pack_voltage_mv <= pack_max_mv:
            self._set_unknown("voltage_series_mismatch", source)
            self._last_sample_ms = sample_ms
            return self.snapshot()
        if self.flags is not None and self.flags & BQ34Z100_SAFETY_FLAGS:
            self._set_unknown("bq34z100_safety_flags", source)
            self._last_sample_ms = sample_ms
            return self.snapshot()

        self.fault = False
        self.available = True
        if self.soc_precise:
            self.level = self._level_for_soc(self.battery_pct)
        else:
            self.level = self._level_for_cell_voltage(self.cell_voltage_mv)
        self._last_sample_ms = sample_ms
        return self.snapshot()

    @staticmethod
    def _level_for_soc(battery_pct):
        if battery_pct <= BATTERY_CRITICAL_PCT:
            return "critical"
        if battery_pct <= BATTERY_LOW_PCT:
            return "low"
        if battery_pct <= BATTERY_WARN_PCT:
            return "warning"
        return "normal"

    @staticmethod
    def _level_for_cell_voltage(cell_voltage_mv):
        if cell_voltage_mv <= BATTERY_CELL_CRITICAL_MV:
            return "critical"
        if cell_voltage_mv <= BATTERY_CELL_LOW_MV:
            return "low"
        if cell_voltage_mv <= BATTERY_CELL_WARN_MV:
            return "warning"
        return "normal"

    def snapshot(self):
        return {
            "battery_pct": self.battery_pct,
            "soc_precise": self.soc_precise,
            "pack_voltage_mv": self.pack_voltage_mv,
            "cell_voltage_mv": self.cell_voltage_mv,
            "current_ma": self.current_ma,
            "power_mw": self.power_mw,
            "charging": self.charging,
            "series_count": self.series_count,
            "fault": self.fault,
            "available": self.available,
            "source": self.source,
            "flags": self.flags,
            "level": self.level,
        }

    def is_low(self):
        return not self.available or self.level in ("low", "critical")

    def is_critical(self):
        return not self.available or self.level == "critical"
