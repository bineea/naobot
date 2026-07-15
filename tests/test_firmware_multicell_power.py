import sys
from pathlib import Path

import pytest

FIRMWARE_ROOT = Path(__file__).resolve().parents[1] / "firmware" / "esp32"
if str(FIRMWARE_ROOT) not in sys.path:
    sys.path.insert(0, str(FIRMWARE_ROOT))

from config import (  # noqa: E402
    BATTERY_CELL_CRITICAL_MV,
    BATTERY_CELL_LOW_MV,
    BATTERY_CELL_WARN_MV,
    BATTERY_SERIES_MAX,
    BATTERY_SERIES_MIN,
    BQ34Z100_ADDR,
    INA226_ADDR,
    INA226_CALIBRATION,
    INA226_CONFIG,
)
from hardware.power import PowerMonitor  # noqa: E402


def le16(value):
    if value < 0:
        value += 65536
    return bytes((value & 0xFF, (value >> 8) & 0xFF))


def be16(value):
    if value < 0:
        value += 65536
    return bytes(((value >> 8) & 0xFF, value & 0xFF))


class FakeClock:
    def __init__(self, value=0):
        self.value = value

    def __call__(self):
        return self.value


class FakeI2C:
    def __init__(self, devices=(), values=None):
        self.devices = list(devices)
        self.values = dict(values or {})
        self.reads = []
        self.writes = []

    def scan(self):
        return list(self.devices)

    def readfrom_mem(self, address, register, length):
        self.reads.append((address, register, length))
        value = self.values[(address, register)]
        if isinstance(value, list):
            value = value.pop(0)
        return bytes(value[:length])

    def writeto_mem(self, address, register, data):
        self.writes.append((address, register, bytes(data)))


def bq_values(*, soc=67, voltage_mv=14800, current_ma=-320, flags=0):
    return {
        (BQ34Z100_ADDR, 0x02): bytes((soc,)),
        (BQ34Z100_ADDR, 0x08): le16(voltage_mv),
        (BQ34Z100_ADDR, 0x0A): le16(current_ma),
        (BQ34Z100_ADDR, 0x0E): le16(flags),
    }


def ina_values(*, voltage_mv=14800, current_ma=-320, power_mw=4750):
    return {
        (INA226_ADDR, 0x02): be16(round(voltage_mv / 1.25)),
        (INA226_ADDR, 0x03): be16(round(power_mw / 25)),
        (INA226_ADDR, 0x04): be16(current_ma),
    }


def test_config_declares_supported_series_and_conservative_cell_thresholds() -> None:
    assert (BATTERY_SERIES_MIN, BATTERY_SERIES_MAX) == (2, 6)
    assert BATTERY_CELL_WARN_MV == 3500
    assert BATTERY_CELL_LOW_MV == 3400
    assert BATTERY_CELL_CRITICAL_MV == 3200
    assert BQ34Z100_ADDR == 0x55
    assert INA226_ADDR == 0x41


@pytest.mark.parametrize(
    ("series_count", "pack_voltage_mv"),
    ((2, 5600), (2, 8600), (6, 16800), (6, 25800)),
)
def test_multicell_voltage_accepts_inclusive_2s_and_6s_boundaries(
    series_count, pack_voltage_mv
) -> None:
    bus = FakeI2C(
        devices=(BQ34Z100_ADDR,),
        values=bq_values(voltage_mv=pack_voltage_mv, current_ma=250),
    )

    power = PowerMonitor(i2c=bus, series_count=series_count)

    assert power.available is True
    assert power.fault is False
    assert power.pack_voltage_mv == pack_voltage_mv
    assert power.cell_voltage_mv == round(pack_voltage_mv / series_count)


@pytest.mark.parametrize("series_count", (None, 0, 1, 7, "4"))
def test_invalid_series_configuration_fails_closed_without_bus_access(series_count) -> None:
    bus = FakeI2C(devices=(BQ34Z100_ADDR,), values=bq_values())

    power = PowerMonitor(i2c=bus, series_count=series_count)

    assert power.available is False
    assert power.fault == "invalid_series_count"
    assert power.source == "none"
    assert power.is_low() is True
    assert power.is_critical() is True
    assert bus.reads == []


@pytest.mark.parametrize(
    ("series_count", "pack_voltage_mv"),
    ((2, 5599), (2, 8601), (6, 16799), (6, 25801)),
)
def test_configured_series_voltage_mismatch_fails_closed(series_count, pack_voltage_mv) -> None:
    bus = FakeI2C(
        devices=(BQ34Z100_ADDR,),
        values=bq_values(voltage_mv=pack_voltage_mv),
    )

    power = PowerMonitor(i2c=bus, series_count=series_count)

    assert power.available is False
    assert power.fault == "voltage_series_mismatch"
    assert power.battery_pct is None
    assert power.is_low() is True


def test_bq34z100_provides_precise_soc_with_little_endian_signed_current_and_flags() -> None:
    bus = FakeI2C(
        devices=(BQ34Z100_ADDR,),
        values=bq_values(soc=73, voltage_mv=15120, current_ma=-456, flags=0x0001),
    )

    power = PowerMonitor(i2c=bus, series_count=4)

    assert power.snapshot() == {
        "battery_pct": 73,
        "soc_precise": True,
        "pack_voltage_mv": 15120,
        "cell_voltage_mv": 3780,
        "current_ma": -456,
        "power_mw": -6895,
        "charging": True,
        "series_count": 4,
        "available": True,
        "fault": False,
        "source": "bq34z100",
        "flags": 0x0001,
        "level": "normal",
    }
    assert bus.reads == [
        (BQ34Z100_ADDR, 0x02, 1),
        (BQ34Z100_ADDR, 0x08, 2),
        (BQ34Z100_ADDR, 0x0A, 2),
        (BQ34Z100_ADDR, 0x0E, 2),
    ]


def test_ina226_fallback_has_no_soc_claim_and_uses_big_endian_calibration() -> None:
    bus = FakeI2C(
        devices=(INA226_ADDR,),
        values=ina_values(voltage_mv=14100, current_ma=-275, power_mw=3875),
    )

    power = PowerMonitor(i2c=bus, series_count=4)

    assert bus.writes[:2] == [
        (INA226_ADDR, 0x00, be16(INA226_CONFIG)),
        (INA226_ADDR, 0x05, be16(INA226_CALIBRATION)),
    ]
    assert power.battery_pct is None
    assert power.soc_precise is False
    assert power.pack_voltage_mv == 14100
    assert power.cell_voltage_mv == 3525
    assert power.current_ma == -275
    assert power.power_mw == 3875
    assert power.charging is True
    assert power.source == "ina226_voltage_fallback"
    assert power.level == "normal"


@pytest.mark.parametrize(
    ("cell_voltage_mv", "expected_level"),
    (
        (BATTERY_CELL_WARN_MV, "warning"),
        (BATTERY_CELL_LOW_MV, "low"),
        (BATTERY_CELL_CRITICAL_MV, "critical"),
    ),
)
def test_ina_fallback_uses_conservative_per_cell_levels(cell_voltage_mv, expected_level) -> None:
    bus = FakeI2C(
        devices=(INA226_ADDR,),
        values=ina_values(voltage_mv=cell_voltage_mv * 4, current_ma=100),
    )

    power = PowerMonitor(i2c=bus, series_count=4)

    assert power.level == expected_level
    assert power.is_low() is (expected_level in ("low", "critical"))
    assert power.is_critical() is (expected_level == "critical")


def test_missing_bq_and_ina_fail_closed() -> None:
    power = PowerMonitor(i2c=FakeI2C(), series_count=4)

    assert power.available is False
    assert power.fault == "power_devices_unavailable"
    assert power.source == "none"
    assert power.battery_pct is None
    assert power.is_low() is True
    assert power.is_critical() is True


def test_sampling_is_cached_for_500_ms_and_refreshes_at_2hz() -> None:
    clock = FakeClock()
    values = bq_values(soc=60)
    values[(BQ34Z100_ADDR, 0x02)] = [bytes((60,)), bytes((59,))]
    bus = FakeI2C(devices=(BQ34Z100_ADDR,), values=values)
    power = PowerMonitor(i2c=bus, series_count=4, clock_ms=clock)
    initial_reads = len(bus.reads)

    clock.value = 499
    assert power.sample()["battery_pct"] == 60
    assert len(bus.reads) == initial_reads

    clock.value = 500
    assert power.sample()["battery_pct"] == 59
    assert len(bus.reads) == initial_reads + 4
