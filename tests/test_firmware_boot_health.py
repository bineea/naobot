from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "firmware" / "esp32" / "update" / "boot_health.py"
FIRMWARE_ROOT = ROOT / "firmware" / "esp32"
if str(FIRMWARE_ROOT) not in sys.path:
    sys.path.insert(0, str(FIRMWARE_ROOT))

from control.motion_controller import MotionController  # noqa: E402
from hardware.servo import ServoBank, ServoOutputGate  # noqa: E402
from motion.action_player import ActionPlayer  # noqa: E402


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
        self.running_target_value = True
        self.running_target_error = None
        self.running_target_calls = 0

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

    def running_target(self):
        self.running_target_calls += 1
        if self.running_target_error:
            raise OSError(self.running_target_error)
        return self.running_target_value

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
        self.motion_inhibited = False
        self.motion_inhibit_reason = None
        self._owners = []

    def cancel(self, reason):
        self.cancelled.append(reason)

    def set_motion_inhibited(self, inhibited, reason):
        if inhibited:
            if reason not in self._owners:
                self._owners.append(reason)
        elif reason in self._owners:
            self._owners.remove(reason)
        self.motion_inhibited = bool(self._owners)
        self.motion_inhibit_reason = self._owners[0] if self._owners else None
        return True

    def has_motion_inhibit(self, owner):
        return owner in self._owners


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


class StatefulPin:
    OUT = 1

    def __init__(self, _number, _mode=None):
        self.level = 0
        self.history = []

    def value(self, value=None):
        if value is not None:
            self.level = int(value)
            self.history.append(self.level)
        return self.level


class ServoI2C:
    def __init__(self):
        self.writes = []

    def scan(self):
        return [0x40]

    def writeto_mem(self, address, register, data):
        self.writes.append((address, register, bytes(data)))


class QuietDisplay:
    def set_face(self, _face):
        return None

    def set_expression(self, _params):
        return None


class AllowAllSafety:
    def can_execute(self, _action):
        return True


class NoReflex:
    def check(self):
        return False


def make_integrated_monitor():
    module = load_module()
    clock = [0]
    gate = ServoOutputGate(pin_factory=StatefulPin)
    servos = ServoBank(i2c=ServoI2C(), output_gate=gate)
    actions = ActionPlayer(servos, QuietDisplay())
    motion = MotionController(actions, AllowAllSafety(), NoReflex(), lambda: clock[0])
    dependencies = {
        "ota_module": FakeOta(),
        "power": FakePower(),
        "imu": FakeImu(),
        "motion": motion,
        "servo_gate": gate,
        "clock_ms": lambda: clock[0],
    }
    monitor = module.BootHealthMonitor(**dependencies)
    return monitor, dependencies, clock, servos


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


def test_running_target_pending_verify_is_marked_healthy_after_ten_seconds() -> None:
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


def test_activated_boot_target_does_not_confirm_while_old_partition_is_running() -> None:
    monitor, dependencies, clock = make_monitor()
    dependencies["ota_module"].pending = False
    dependencies["ota_module"].running_target_value = False

    first = monitor.tick()
    clock[0] = 10000
    second = monitor.tick()

    assert first["state"] == "waiting_reboot"
    assert second["state"] == "waiting_reboot"
    assert dependencies["ota_module"].mark_calls == 0
    assert dependencies["ota_module"].sequence == 2
    assert dependencies["motion"].cancelled == [
        "ota_pending_verify",
        "ota_pending_verify",
    ]


def test_running_target_already_valid_recovers_after_full_health_window() -> None:
    monitor, dependencies, clock = make_monitor()
    dependencies["ota_module"].pending = False

    first = monitor.tick()
    clock[0] = 9999
    monitor.tick()
    clock[0] = 10000
    result = monitor.tick()

    assert first["state"] == "monitoring"
    assert dependencies["ota_module"].running_target_calls >= 1
    assert dependencies["ota_module"].mark_calls == 1
    assert result["state"] == "healthy"


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


def test_pending_verify_inhibits_real_motion_and_sit_cannot_enable_oe() -> None:
    monitor, dependencies, _clock, servos = make_integrated_monitor()

    monitor.tick()
    accepted, reason = dependencies["motion"].submit_action(
        {"name": "sit", "args": {}}
    )

    assert accepted is False
    assert "ota_boot_health" in reason
    assert dependencies["motion"].motion_inhibited is True
    assert dependencies["servo_gate"].confirm_disabled() is True
    assert servos.enabled is False
    assert 0 not in dependencies["servo_gate"]._pin.history


def test_mark_healthy_success_releases_boot_health_motion_owner() -> None:
    monitor, dependencies, clock, servos = make_integrated_monitor()
    monitor.tick()
    assert dependencies["motion"].motion_inhibited is True

    clock[0] = 10000
    result = monitor.tick()
    accepted, reason = dependencies["motion"].submit_action(
        {"name": "sit", "args": {}}
    )

    assert result["state"] == "healthy"
    assert dependencies["motion"].motion_inhibited is False
    assert accepted is True, reason
    assert dependencies["servo_gate"].confirm_disabled() is False
    assert servos.enabled is True


@pytest.mark.parametrize("failure", ["mark_healthy", "rollback"])
def test_health_or_rollback_failure_keeps_boot_health_motion_owner(failure) -> None:
    monitor, dependencies, clock, servos = make_integrated_monitor()
    if failure == "mark_healthy":
        dependencies["ota_module"].mark_error = "NVS commit failed"
        monitor.tick()
        clock[0] = 10000
    else:
        dependencies["power"].values["fault"] = True

        def fail_rollback():
            dependencies["ota_module"].rollback_calls += 1
            raise OSError("rollback failed")

        dependencies["ota_module"].rollback_and_reboot = fail_rollback

    result = monitor.tick()
    accepted, _reason = dependencies["motion"].submit_action(
        {"name": "sit", "args": {}}
    )

    assert result["state"] == "error"
    assert dependencies["motion"].motion_inhibited is True
    assert accepted is False
    assert dependencies["servo_gate"].confirm_disabled() is True
    assert servos.enabled is False


def test_boot_health_and_coordinator_motion_owners_do_not_unlock_each_other() -> None:
    monitor, dependencies, clock, servos = make_integrated_monitor()
    motion = dependencies["motion"]
    assert motion.set_motion_inhibited(True, "ota") is True

    monitor.tick()
    clock[0] = 10000
    result = monitor.tick()

    assert result["state"] == "healthy"
    assert motion.has_motion_inhibit("ota") is True
    assert motion.has_motion_inhibit("ota_boot_health") is False
    assert motion.motion_inhibited is True
    assert motion.set_motion_inhibited(False, "ota") is True
    assert motion.motion_inhibited is False
    assert dependencies["servo_gate"].confirm_disabled() is True
    assert servos.enabled is False


def test_unknown_pending_state_locks_until_no_transaction_is_confirmed() -> None:
    monitor, dependencies, _clock, _servos = make_integrated_monitor()
    ota = dependencies["ota_module"]
    ota.pending_error = "native state unavailable"

    first = monitor.tick()
    assert first["pending_verify"] == "unknown"
    assert dependencies["motion"].motion_inhibited is True

    ota.pending_error = None
    ota.pending = False
    ota.sequence = None
    ota.phase_value = None
    ota.running_target_value = False
    second = monitor.tick()

    assert second["state"] == "not_pending"
    assert dependencies["motion"].motion_inhibited is False
