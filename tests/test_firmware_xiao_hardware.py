import asyncio
import sys
from pathlib import Path

import pytest

FIRMWARE_ROOT = Path(__file__).resolve().parents[1] / "firmware" / "esp32"
if str(FIRMWARE_ROOT) not in sys.path:
    sys.path.insert(0, str(FIRMWARE_ROOT))

from hardware.buzzer import Buzzer  # noqa: E402
from hardware.display import Display  # noqa: E402
from hardware.i2c import SharedI2C  # noqa: E402
from hardware.imu import IMU  # noqa: E402
from hardware.power import PowerMonitor  # noqa: E402
from hardware.servo import PCA9685, ServoBank, ServoOutputGate  # noqa: E402
from hardware.touch import TouchInputs  # noqa: E402


def le16(value):
    if value < 0:
        value += 65536
    return bytes((value & 0xFF, (value >> 8) & 0xFF))


class FakePin:
    OUT = 1
    events = []

    def __init__(self, number, mode=None):
        self.number = number
        self.mode = mode

    def value(self, value=None):
        if value is not None:
            self.events.append(("oe", self.number, value))
        return value


class FakeI2C:
    def __init__(self, devices=(), values=None, events=None):
        self.devices = list(devices)
        self.values = dict(values or {})
        self.events = events if events is not None else []
        self.writes = []
        self.reads = []

    def scan(self):
        return self.devices

    def writeto_mem(self, address, register, data):
        record = (address, register, bytes(data))
        self.writes.append(record)
        self.events.append(("i2c_write",) + record)

    def readfrom_mem(self, address, register, length):
        self.reads.append((address, register, length))
        value = self.values.get((address, register), bytes(length))
        if isinstance(value, list):
            value = value.pop(0)
        return bytes(value[:length])


def test_shared_i2c_factory_creates_xiao_i2c0_once(monkeypatch) -> None:
    import hardware.i2c as shared_i2c

    calls = []

    class Pin:
        def __init__(self, number):
            self.number = number

    class I2C:
        def __init__(self, bus_id, **kwargs):
            calls.append((bus_id, kwargs))

    monkeypatch.setattr(shared_i2c, "Pin", Pin)
    monkeypatch.setattr(shared_i2c, "I2C", I2C)
    monkeypatch.setattr(SharedI2C, "_instance", None)
    monkeypatch.setattr(SharedI2C, "_attempted", False)

    first = SharedI2C.get()
    second = SharedI2C.get()

    assert first is second
    assert len(calls) == 1
    bus_id, kwargs = calls[0]
    assert bus_id == 0
    assert kwargs["sda"].number == 5
    assert kwargs["scl"].number == 6
    assert kwargs["freq"] == 400_000


def test_hardware_consumers_default_to_the_shared_raw_i2c(monkeypatch) -> None:
    bus = FakeI2C(
        devices=(0x3C, 0x40, 0x55, 0x5A, 0x68),
        values={
            (0x68, 0x3F): b"\x40\x00",
            (0x55, 0x02): b"\x32",
            (0x55, 0x08): le16(7600),
            (0x55, 0x0A): le16(-100),
            (0x55, 0x0E): le16(0),
        },
    )
    monkeypatch.setattr(SharedI2C, "get", classmethod(lambda cls: bus))
    monkeypatch.setattr(Display, "_create_oled", lambda self, i2c: object())
    monkeypatch.setattr(Display, "_safe_render_frame", lambda self, frame, status=None: None)

    display = Display()
    imu = IMU(calibrate=False)
    touch = TouchInputs()
    servos = ServoBank()
    power = PowerMonitor()

    assert display.i2c is bus
    assert imu.i2c is bus
    assert touch.i2c is bus
    assert servos.i2c is bus
    assert power.i2c is bus


def test_imu_is_fault_reuses_latest_posture_without_another_bus_read() -> None:
    bus = FakeI2C(
        devices=(0x68,),
        values={(0x68, 0x3F): b"\x40\x00"},
    )
    imu = IMU(i2c=bus, calibrate=False)
    reads_after_sample = len(bus.reads)

    assert imu.is_fault() is False
    assert len(bus.reads) == reads_after_sample


def test_pca9685_initializes_for_50_hz() -> None:
    bus = FakeI2C(devices=(0x40,))

    driver = PCA9685(bus)

    assert driver.available is True
    assert (0x40, 0xFE, b"y") in bus.writes


def test_servo_bank_raises_oe_before_any_i2c_and_maps_four_channels() -> None:
    events = []
    FakePin.events = events
    bus = FakeI2C(devices=(0x40,), events=events)
    servos = ServoBank(i2c=bus, pin_factory=FakePin)

    assert events[0] == ("oe", 1, 1)
    assert servos.enabled is False

    events.clear()
    assert servos.pose({"lf": 10, "rf": 170, "lr": 90, "rr": 90}) is True

    channel_registers = [record[1] for record in bus.writes if 0x06 <= record[1] <= 0x12]
    assert channel_registers[-4:] == [0x06, 0x0A, 0x0E, 0x12]
    assert servos.positions == {"lf": 30, "rf": 150, "lr": 90, "rr": 90}
    assert events[-1] == ("oe", 1, 0)
    assert servos.enabled is True


def test_servo_stop_disables_oe_before_clearing_outputs() -> None:
    events = []
    FakePin.events = events
    servos = ServoBank(i2c=FakeI2C(devices=(0x40,), events=events), pin_factory=FakePin)
    servos.pose({"lf": 90})
    events.clear()

    servos.stop()

    assert events[0] == ("oe", 1, 1)
    assert events[1][0:3] == ("i2c_write", 0x40, 0xFA)
    assert servos.enabled is False


def test_servo_emergency_off_latches_and_blocks_future_pose() -> None:
    events = []
    FakePin.events = events
    bus = FakeI2C(devices=(0x40,), events=events)
    servos = ServoBank(i2c=bus, pin_factory=FakePin)
    servos.pose({"lf": 90})
    servos.emergency_off()
    write_count = len(bus.writes)

    assert servos.pose({"lf": 120}) is False
    assert servos.set("rf", 120) is False
    assert len(bus.writes) == write_count
    assert servos.enabled is False
    assert servos.emergency_latched is True
    assert events[-1] != ("oe", 1, 0)


def test_servo_i2c_failure_still_leaves_oe_high() -> None:
    events = []
    FakePin.events = events

    class BrokenI2C(FakeI2C):
        def writeto_mem(self, address, register, data):
            raise OSError("i2c failed")

    servos = ServoBank(i2c=BrokenI2C(devices=(0x40,)), pin_factory=FakePin)

    assert events[0] == ("oe", 1, 1)
    assert servos.available is False
    assert servos.enabled is False


def test_servo_runtime_i2c_failure_disables_oe_before_write() -> None:
    events = []
    FakePin.events = events

    class FailingWriteI2C(FakeI2C):
        fail = False

        def writeto_mem(self, address, register, data):
            if self.fail:
                raise OSError("runtime i2c failed")
            super().writeto_mem(address, register, data)

    bus = FailingWriteI2C(devices=(0x40,), events=events)
    servos = ServoBank(i2c=bus, pin_factory=FakePin)
    servos.pose({"lf": 90})
    events.clear()
    bus.fail = True

    assert servos.pose({"rf": 100}) is False
    assert events[0] == ("oe", 1, 1)
    assert servos.enabled is False


def test_servo_oe_high_failure_preserves_enabled_and_blocks_pca_write() -> None:
    class FailingHighPin:
        OUT = 1
        fail_high = False

        def __init__(self, number, mode=None):
            self.number = number

        def value(self, value=None):
            if value == 1 and self.fail_high:
                raise OSError("oe stuck low")
            return value

    bus = FakeI2C(devices=(0x40,))
    servos = ServoBank(i2c=bus, pin_factory=FailingHighPin)
    assert servos.pose({"lf": 90}) is True
    writes_before_failure = len(bus.writes)
    FailingHighPin.fail_high = True

    assert servos.pose({"rf": 100}) is False
    assert servos.enabled is True
    assert len(bus.writes) == writes_before_failure


def test_servo_emergency_latch_blocks_every_future_pwm_write() -> None:
    events = []
    FakePin.events = events
    bus = FakeI2C(devices=(0x40,), events=events)
    servos = ServoBank(i2c=bus, pin_factory=FakePin)
    assert servos.pose({"lf": 90}) is True

    assert servos.emergency_off() is True
    writes_after_emergency = len(bus.writes)
    events_after_emergency = len(events)

    assert servos.pose({"lf": 100}) is False
    assert servos.set("rf", 100) is False
    assert servos.neutral() is False
    assert servos.sequence(({"lr": 100},), delay_ms=0) is False
    assert servos.stop() is False
    assert len(bus.writes) == writes_after_emergency
    assert ("oe", 1, 0) not in events[events_after_emergency:]


def test_servo_output_gate_raises_oe_during_construction() -> None:
    events = []
    FakePin.events = events

    gate = ServoOutputGate(pin_factory=FakePin)

    assert gate.available is True
    assert gate.disabled is True
    assert events == [("oe", 1, 1)]


def test_mpr121_debounces_two_samples_and_emits_rising_edges_only() -> None:
    bus = FakeI2C(
        devices=(0x5A,),
        values={(0x5A, 0x00): [b"\x01\x00", b"\x01\x00", b"\x01\x00", b"\x00\x00", b"\x00\x00", b"\x01\x00", b"\x01\x00"]},
    )
    touch = TouchInputs(i2c=bus)

    assert touch.poll() is None
    assert touch.poll() == "touch_head"
    assert touch.poll() is None
    assert touch.poll() is None
    assert touch.poll() is None
    assert touch.poll() is None
    assert touch.poll() == "touch_head"
    assert (0x5A, 0x41, b"\x0c") in bus.writes
    assert (0x5A, 0x42, b"\x06") in bus.writes
    assert bus.writes[-1] == (0x5A, 0x5E, b"\x82")


def test_mpr121_maps_electrode_one_to_touch_back_and_missing_device_is_safe() -> None:
    bus = FakeI2C(
        devices=(0x5A,),
        values={(0x5A, 0x00): [b"\x02\x00", b"\x02\x00"]},
    )
    touch = TouchInputs(i2c=bus)

    assert touch.poll() is None
    assert touch.poll() == "touch_back"

    missing = TouchInputs(i2c=FakeI2C(devices=()))
    assert missing.available is False
    assert missing.poll() is None


def make_power_bus(soc=50, voltage=7600, current=-120, flags=0):
    return FakeI2C(
        devices=(0x55,),
        values={
            (0x55, 0x02): bytes((soc,)),
            (0x55, 0x08): le16(voltage),
            (0x55, 0x0A): le16(current),
            (0x55, 0x0E): le16(flags),
        },
    )


def test_power_monitor_reads_multicell_gauge_snapshot() -> None:
    power = PowerMonitor(i2c=make_power_bus())

    assert power.snapshot() == {
        "battery_pct": 50,
        "soc_precise": True,
        "pack_voltage_mv": 7600,
        "cell_voltage_mv": 3800,
        "current_ma": -120,
        "power_mw": -912,
        "charging": True,
        "series_count": 2,
        "fault": False,
        "available": True,
        "source": "bq34z100",
        "flags": 0,
        "level": "normal",
    }
    assert power.is_low() is False


def test_power_monitor_applies_warning_low_and_critical_thresholds() -> None:
    assert PowerMonitor(i2c=make_power_bus(soc=20)).level == "warning"
    assert PowerMonitor(i2c=make_power_bus(soc=15)).level == "low"
    critical = PowerMonitor(i2c=make_power_bus(soc=8))
    assert critical.level == "critical"
    assert critical.is_low() is True
    assert critical.is_critical() is True


def test_power_monitor_missing_or_failed_devices_fail_closed() -> None:
    missing = PowerMonitor(i2c=FakeI2C(devices=()))

    assert missing.available is False
    assert missing.battery_pct is None
    assert missing.fault == "power_devices_unavailable"
    assert missing.level == "unknown"
    assert missing.is_low() is True
    assert missing.is_critical() is True


def test_buzzer_only_forwards_non_blocking_tone_requests() -> None:
    requests = []
    buzzer = Buzzer(request_tone=requests.append)

    assert buzzer.chirp("happy") is True
    buzzer.play_step(900, 180)
    buzzer.off()

    assert requests == ["happy", {"frequency_hz": 900, "duration_ms": 180}, {"stop": True}]
    assert not hasattr(buzzer, "pwm")


def test_main_explicitly_injects_one_shared_i2c_into_all_consumers() -> None:
    source = (FIRMWARE_ROOT / "main.py").read_text(encoding="utf-8")

    assert "shared_i2c = SharedI2C.get()" in source
    assert "Display(i2c=shared_i2c)" in source
    assert "IMU(i2c=shared_i2c)" in source
    assert "PowerMonitor(i2c=shared_i2c)" in source
    assert "TouchInputs(i2c=shared_i2c)" in source
    assert "ServoBank(i2c=shared_i2c, output_gate=servo_gate)" in source


def test_main_raises_servo_oe_before_attempting_i2c_initialization(monkeypatch) -> None:
    import main as firmware_main

    events = []
    gates = []

    class Gate:
        def __init__(self):
            self.disabled = True
            gates.append(self)
            events.append("oe_high")

    def fail_i2c(cls):
        events.append("i2c")
        raise OSError("i2c init failed")

    monkeypatch.setattr(firmware_main, "ServoOutputGate", Gate)
    monkeypatch.setattr(firmware_main.SharedI2C, "get", classmethod(fail_i2c))

    with pytest.raises(OSError, match="i2c init failed"):
        asyncio.run(firmware_main.main())

    assert events == ["oe_high", "i2c"]
    assert gates[0].disabled is True
