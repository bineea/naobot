from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "firmware" / "esp32" / "update" / "boot_health.py"


def load_module():
    assert MODULE_PATH.exists(), "BootHealthMonitor 尚未实现"
    spec = importlib.util.spec_from_file_location("boot_health", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeOta:
    def __init__(self):
        self.pending = True
        self.mark_calls = 0
        self.rollback_calls = 0
        self.pending_error = None
        self.pending_none = False
        self.mark_error = None
        self.sequence = 2
        self.phase_value = "activated"

    def pending_verify(self):
        if self.pending_error:
            raise OSError(self.pending_error)
        if self.pending_none:
            return None
        return self.pending

    def pending_sequence(self):
        return self.sequence

    def phase(self):
        return self.phase_value

    def mark_healthy(self):
        self.mark_calls += 1
        if self.mark_error:
            raise OSError(self.mark_error)
        self.pending = False
        self.sequence = None
        self.phase_value = None
        return True

    def rollback_and_reboot(self):
        self.rollback_calls += 1
        return True


class FakePower:
    def __init__(self):
        self.values = {"available": True, "fault": False}

    def snapshot(self):
        return dict(self.values)


class FakeImu:
    available = True
    posture = "upright"


class FakeMotion:
    def __init__(self):
        self.cancelled = []

    def cancel(self, reason):
        self.cancelled.append(reason)


class FakeGate:
    available = True

    def __init__(self):
        self.disabled = False
        self.feedback = True

    def set_disabled(self, value):
        self.disabled = bool(value)
        return True

    def confirm_disabled(self):
        return self.feedback


def make_monitor():
    module = load_module()
    clock = [0]
    dependencies = {
        "ota_module": FakeOta(),
        "power": FakePower(),
        "imu": FakeImu(),
        "motion": FakeMotion(),
        "servo_gate": FakeGate(),
        "clock_ms": lambda: clock[0],
    }
    return module.BootHealthMonitor(**dependencies), dependencies, clock


def test_pending_boot_is_quiesced_and_marked_healthy_after_ten_continuous_seconds() -> None:
    monitor, dependencies, clock = make_monitor()

    monitor.tick()
    assert dependencies["motion"].cancelled == ["ota_pending_verify"]
    assert dependencies["servo_gate"].disabled is True
    assert dependencies["ota_module"].mark_calls == 0

    clock[0] = 9999
    monitor.tick()
    assert dependencies["ota_module"].mark_calls == 0
    clock[0] = 10000
    monitor.tick()

    assert dependencies["ota_module"].mark_calls == 1
    assert dependencies["ota_module"].rollback_calls == 0
    assert monitor.status()["state"] == "healthy"


def test_health_window_resets_when_a_condition_is_temporarily_unknown() -> None:
    monitor, dependencies, clock = make_monitor()
    monitor.tick()
    clock[0] = 9000
    monitor.tick()
    dependencies["imu"].posture = "unknown"
    clock[0] = 9500
    monitor.tick()
    dependencies["imu"].posture = "upright"
    clock[0] = 10000
    monitor.tick()
    clock[0] = 19999
    monitor.tick()
    assert dependencies["ota_module"].mark_calls == 0
    clock[0] = 20000
    monitor.tick()
    assert dependencies["ota_module"].mark_calls == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["power"].values.update(available=False),
        lambda d: d["power"].values.update(fault=True),
        lambda d: setattr(d["imu"], "posture", "fallen"),
        lambda d: setattr(d["servo_gate"], "available", False),
        lambda d: setattr(d["servo_gate"], "feedback", False),
    ],
)
def test_critical_pending_boot_fault_rolls_back_immediately(mutate) -> None:
    monitor, dependencies, _clock = make_monitor()
    mutate(dependencies)

    result = monitor.tick()

    assert dependencies["ota_module"].rollback_calls == 1
    assert result["state"] == "rollback"


def test_pending_boot_rolls_back_at_thirty_second_deadline() -> None:
    monitor, dependencies, clock = make_monitor()
    dependencies["imu"].posture = "unknown"
    monitor.tick()
    clock[0] = 29999
    monitor.tick()
    assert dependencies["ota_module"].rollback_calls == 0

    clock[0] = 30000
    monitor.tick()

    assert dependencies["ota_module"].rollback_calls == 1


def test_mark_healthy_failure_keeps_retrying_before_deadline() -> None:
    monitor, dependencies, clock = make_monitor()
    dependencies["ota_module"].mark_error = "NVS commit failed"
    monitor.tick()
    clock[0] = 10000
    first = monitor.tick()
    clock[0] = 10050
    second = monitor.tick()

    assert dependencies["ota_module"].mark_calls == 2
    assert dependencies["ota_module"].rollback_calls == 0
    assert "NVS commit failed" in first["error"]
    assert "NVS commit failed" in second["error"]


@pytest.mark.parametrize("unknown_mode", ["exception", "none"])
def test_unknown_pending_state_quiesces_and_rolls_back_at_deadline(unknown_mode) -> None:
    monitor, dependencies, _clock = make_monitor()
    if unknown_mode == "exception":
        dependencies["ota_module"].pending_error = "native state failed"
    else:
        dependencies["ota_module"].pending_none = True

    result = monitor.tick()

    assert result["pending_verify"] == "unknown"
    assert result["state"] == "error"
    assert dependencies["motion"].cancelled == ["ota_pending_verify"]
    assert dependencies["servo_gate"].disabled is True
    assert dependencies["servo_gate"].confirm_disabled() is True
    assert dependencies["ota_module"].rollback_calls == 0

    _clock[0] = 30000
    result = monitor.tick()

    assert dependencies["ota_module"].rollback_calls == 1
    assert result["state"] == "rollback"


def test_valid_confirming_metadata_is_completed_idempotently() -> None:
    monitor, dependencies, _clock = make_monitor()
    dependencies["ota_module"].pending = False
    dependencies["ota_module"].phase_value = "confirming"

    result = monitor.tick()

    assert dependencies["motion"].cancelled == ["ota_pending_verify"]
    assert dependencies["servo_gate"].disabled is True
    assert dependencies["ota_module"].mark_calls == 1
    assert result["state"] == "healthy"
    assert result["phase"] is None
    assert result["pending_sequence"] is None


@pytest.mark.parametrize("dependency", ["motion", "servo_gate", "power", "imu"])
def test_dependency_exceptions_never_escape_the_health_monitor(dependency) -> None:
    monitor, dependencies, _clock = make_monitor()

    def explode(*_args):
        raise OSError(f"{dependency} exploded")

    if dependency == "motion":
        dependencies["motion"].cancel = explode
    elif dependency == "servo_gate":
        dependencies["servo_gate"].set_disabled = explode
    elif dependency == "power":
        dependencies["power"].snapshot = explode
    else:
        class ExplodingImu:
            available = True

            @property
            def posture(self):
                return explode()

        monitor.imu = ExplodingImu()

    result = monitor.tick()

    assert result["state"] in ("error", "rollback")
    assert dependencies["ota_module"].mark_calls == 0
