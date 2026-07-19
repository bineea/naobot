from __future__ import annotations

import importlib.util
import json
import sys
from hashlib import sha256
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIRMWARE_ROOT = ROOT / "firmware" / "esp32"
COORDINATOR_PATH = FIRMWARE_ROOT / "update" / "update_coordinator.py"
NATIVE_ROOT = FIRMWARE_ROOT / "build" / "ota_module"
if str(FIRMWARE_ROOT) not in sys.path:
    sys.path.insert(0, str(FIRMWARE_ROOT))


def load_coordinator_module():
    assert COORDINATOR_PATH.exists(), "OTA 协调器尚未实现"
    spec = importlib.util.spec_from_file_location("update_coordinator", COORDINATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def canonical_manifest(image: bytes, sequence: int = 2, **overrides) -> bytes:
    manifest = {
        "schema": 1,
        "board_id": "XIAO_ESP32S3_SENSE",
        "key_id": "test-dev",
        "sequence": sequence,
        "version": "2.0.0",
        "image_name": "firmware.bin",
        "image_size": len(image),
        "sha256": sha256(image).hexdigest(),
        "min_runtime_api": 1,
    }
    manifest.update(overrides)
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


class FakeStorage:
    def __init__(self, files, *, available=True, mounted=True):
        self.files = dict(files)
        self.available = available
        self.mounted = mounted
        self.runtime_state = "running"
        self.requests = []
        self.stalled_files = set()

    def snapshot(self):
        return {
            "available": self.available,
            "mounted": self.mounted,
            "runtime_state": self.runtime_state,
        }

    def submit_update_read(self, sequence, filename, offset=0, max_bytes=None):
        self.requests.append((str(sequence), filename, offset, max_bytes))
        if self.runtime_state != "running":
            return {"accepted": False, "reason": "storage worker not running"}
        if filename in self.stalled_files:
            return {"accepted": True, "result": None, "error": None}
        if not self.available or not self.mounted:
            return {"accepted": True, "result": None, "error": "SD unavailable"}
        data = self.files.get((str(sequence), filename))
        if data is None:
            return {"accepted": True, "result": None, "error": "missing update file"}
        limit = 4096 if max_bytes is None else max_bytes
        return {"accepted": True, "result": data[offset : offset + limit], "error": None}

    @staticmethod
    def poll(request):
        return dict(request)


class FakePower:
    def __init__(self):
        self.values = {
            "battery_pct": 80,
            "soc_precise": True,
            "charging": True,
            "fault": False,
            "available": True,
            "source": "bq34z100",
        }

    def snapshot(self):
        return dict(self.values)


class FakeMotion:
    def __init__(self):
        self.current = None
        self.queue = []
        self.motion_state = "idle"
        self.cancelled = []

    def cancel(self, reason):
        self.cancelled.append(reason)
        self.current = None
        self.queue = []


class FakeGate:
    def __init__(self):
        self.available = True
        self.disabled = True
        self.feedback = True
        self.disable_calls = 0

    def set_disabled(self, disabled):
        self.disable_calls += 1
        self.disabled = bool(disabled)
        return self.available

    def confirm_disabled(self):
        return self.feedback if self.available else None


class FakeReflex:
    state = "none"
    authority = "idle"
    emergency_stop = False
    shutdown_failed_latched = False


class FakeTouch:
    available = True
    touch_mask = 0x03
    both_touched = True


class FakeOta:
    def __init__(self):
        self.valid_signature = True
        self.writes = []
        self.begin_args = None
        self.abort_calls = 0
        self.finish_calls = 0
        self.reboot_calls = 0
        self.pending = False
        self.finish_error = None
        self.activate_error = None
        self.current = 1
        self.pending_sequence_value = None
        self.phase_value = None
        self.staged = False
        self.boot_selected = False
        self.activate_calls = 0

    def verify_manifest(self, manifest_bytes, signature_der):
        assert isinstance(manifest_bytes, bytes)
        assert isinstance(signature_der, bytes)
        return self.valid_signature

    def begin(self, image_size, expected_sha256_bytes, sequence):
        self.begin_args = (image_size, expected_sha256_bytes, sequence)
        return True

    def write(self, chunk):
        self.writes.append(bytes(chunk))
        return len(chunk)

    def finish(self):
        self.finish_calls += 1
        if self.finish_error:
            raise OSError(self.finish_error)
        self.staged = True
        return True

    def activate(self):
        self.activate_calls += 1
        if self.activate_error:
            raise OSError(self.activate_error)
        self.pending_sequence_value = self.begin_args[2]
        self.phase_value = "activated"
        self.boot_selected = True
        return True

    def abort(self):
        self.abort_calls += 1
        self.staged = False
        return True

    def pending_verify(self):
        return self.pending

    def current_sequence(self):
        return self.current

    def pending_sequence(self):
        return self.pending_sequence_value

    def phase(self):
        return self.phase_value

    def status(self):
        return {
            "state": "ready_to_reboot" if self.boot_selected else "idle",
        }


def make_system(image=b"abcdefgh", sequence=2, **coordinator_kwargs):
    module = load_coordinator_module()
    manifest = canonical_manifest(image, sequence)
    storage = FakeStorage(
        {
            (str(sequence), "manifest.json"): manifest,
            (str(sequence), "signature.der"): b"\x30\x01\x00",
            (str(sequence), "firmware.bin"): image,
        }
    )
    clock = [0]
    dependencies = {
        "storage": storage,
        "power": FakePower(),
        "motion": FakeMotion(),
        "servo_gate": FakeGate(),
        "reflex": FakeReflex(),
        "touch": FakeTouch(),
        "ota_module": FakeOta(),
        "clock_ms": lambda: clock[0],
        "reboot": lambda: dependencies["ota_module"].__dict__.__setitem__(
            "reboot_calls", dependencies["ota_module"].reboot_calls + 1
        ),
    }
    dependencies.update(coordinator_kwargs)
    coordinator = module.UpdateCoordinator(**dependencies)
    assert coordinator.request_install(sequence) is True
    return coordinator, dependencies, clock


def tick_until(coordinator, state, limit=30):
    for _ in range(limit):
        coordinator.tick()
        if coordinator.status()["ota_state"] == state:
            return
    pytest.fail(f"OTA state did not reach {state}: {coordinator.status()}")


def test_manifest_validation_is_exact_canonical_and_rejects_stale_sequence() -> None:
    module = load_coordinator_module()
    image = b"image"
    valid = canonical_manifest(image, 2)
    assert module.validate_manifest(valid, requested_sequence=2, current_sequence=1)["sequence"] == 2

    invalid = (
        canonical_manifest(image, 2, schema=2),
        canonical_manifest(image, 2, board_id="OTHER"),
        canonical_manifest(image, 2, image_name="other.bin"),
        canonical_manifest(image, 2, image_size=0),
        canonical_manifest(image, 2, image_size=0x280001),
        canonical_manifest(image, 2, sha256="A" * 64),
        canonical_manifest(image, 2, min_runtime_api=2),
        canonical_manifest(image, 1),
        valid + b" ",
    )
    for manifest in invalid:
        with pytest.raises(ValueError):
            module.validate_manifest(manifest, requested_sequence=2, current_sequence=1)

    with_extra = json.loads(valid)
    with_extra["extra"] = True
    with pytest.raises(ValueError):
        module.validate_manifest(
            json.dumps(with_extra, sort_keys=True, separators=(",", ":")).encode(),
            requested_sequence=2,
            current_sequence=1,
        )


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda d: d["power"].values.update(soc_precise=False), "precise SOC unavailable"),
        (lambda d: d["power"].values.update(battery_pct=49), "SOC below 50"),
        (lambda d: d["power"].values.update(charging=None), "charging not confirmed"),
        (lambda d: d["power"].values.update(charging=False), "charging not confirmed"),
        (lambda d: d["power"].values.update(fault=True), "power unhealthy"),
        (lambda d: d["power"].values.update(available=False), "power unhealthy"),
        (lambda d: setattr(d["motion"], "current", object()), "motion not idle"),
        (lambda d: d["motion"].queue.append(object()), "motion not idle"),
        (lambda d: setattr(d["motion"], "motion_state", "walking"), "motion not idle"),
        (lambda d: setattr(d["servo_gate"], "available", False), "OE unavailable"),
        (lambda d: setattr(d["servo_gate"], "feedback", None), "OE disable unconfirmed"),
        (lambda d: setattr(d["reflex"], "state", "fall_detected"), "reflex active"),
        (lambda d: setattr(d["reflex"], "authority", "reflex"), "reflex active"),
        (lambda d: setattr(d["touch"], "available", False), "MPR121 unavailable"),
        (lambda d: setattr(d["storage"], "available", False), "SD unavailable"),
    ],
)
def test_each_unknown_or_unsafe_gate_denies_install(mutate, error) -> None:
    coordinator, dependencies, clock = make_system()
    tick_until(coordinator, "waiting_for_gates")
    mutate(dependencies)
    clock[0] = 3000

    coordinator.tick()

    status = coordinator.status()
    assert status["ota_state"] != "installing"
    assert error in status["ota_error"]
    assert dependencies["ota_module"].begin_args is None


def test_signature_is_checked_before_manifest_is_parsed() -> None:
    coordinator, dependencies, _clock = make_system()
    dependencies["storage"].files[("2", "manifest.json")] = b"not json"
    dependencies["ota_module"].valid_signature = False

    tick_until(coordinator, "denied")

    assert coordinator.status()["ota_error"] == "invalid manifest signature"


@pytest.mark.parametrize("sequence", [-1, True, 1, 0x1_0000_0000])
def test_request_rejects_invalid_or_non_new_uint32_sequence(sequence) -> None:
    coordinator, _dependencies, _clock = make_system()
    coordinator._state = "idle"

    assert coordinator.request_install(sequence) is False


def test_request_rejects_when_native_pending_sequence_exists() -> None:
    coordinator, dependencies, _clock = make_system()
    coordinator._state = "idle"
    dependencies["ota_module"].pending_sequence_value = 2

    assert coordinator.request_install(3) is False


def test_dual_touch_must_be_continuous_for_three_seconds() -> None:
    coordinator, dependencies, clock = make_system()
    dependencies["touch"].both_touched = False
    dependencies["touch"].touch_mask = 0
    tick_until(coordinator, "waiting_for_gates")

    dependencies["touch"].both_touched = True
    dependencies["touch"].touch_mask = 3
    clock[0] = 100
    coordinator.tick()
    clock[0] = 3099
    coordinator.tick()
    assert dependencies["ota_module"].begin_args is None

    dependencies["touch"].both_touched = False
    dependencies["touch"].touch_mask = 0
    coordinator.tick()
    dependencies["touch"].both_touched = True
    dependencies["touch"].touch_mask = 3
    clock[0] = 4000
    coordinator.tick()
    clock[0] = 6999
    coordinator.tick()
    assert dependencies["ota_module"].begin_args is None
    clock[0] = 7000
    coordinator.tick()
    assert dependencies["ota_module"].begin_args is not None


def test_streaming_stages_without_selecting_boot_or_rebooting() -> None:
    image = b"x" * 9000
    coordinator, dependencies, clock = make_system(image=image, chunk_size=4096)
    tick_until(coordinator, "waiting_for_gates")
    clock[0] = 3000
    tick_until(coordinator, "awaiting_activation_release")

    ota = dependencies["ota_module"]
    assert [len(chunk) for chunk in ota.writes] == [4096, 4096, 808]
    assert all(length is None or length <= 4096 for *_prefix, length in dependencies["storage"].requests)
    assert ota.finish_calls == 1
    assert ota.activate_calls == 0
    assert ota.boot_selected is False
    assert ota.pending_sequence() is None
    assert ota.reboot_calls == 0
    assert coordinator.status() == {
        "ota_state": "awaiting_activation_release",
        "ota_progress_pct": 100,
        "ota_error": None,
        "ota_pending_verify": False,
        "ota_sequence": 2,
    }
    coordinator.tick()
    assert ota.reboot_calls == 0
    assert coordinator.request_install(3) is False


def test_staged_image_requires_full_release_then_second_three_second_touch() -> None:
    coordinator, dependencies, clock = make_system()
    tick_until(coordinator, "waiting_for_gates")
    clock[0] = 3000
    tick_until(coordinator, "awaiting_activation_release")
    ota = dependencies["ota_module"]

    clock[0] = 4000
    coordinator.tick()
    assert coordinator.status()["ota_state"] == "awaiting_activation_release"

    dependencies["touch"].both_touched = False
    dependencies["touch"].touch_mask = 0
    coordinator.tick()
    clock[0] = 4499
    coordinator.tick()
    assert coordinator.status()["ota_state"] == "awaiting_activation_release"
    clock[0] = 4500
    coordinator.tick()
    assert coordinator.status()["ota_state"] == "waiting_for_activation"

    dependencies["touch"].both_touched = True
    dependencies["touch"].touch_mask = 3
    clock[0] = 5000
    coordinator.tick()
    clock[0] = 7999
    coordinator.tick()
    assert ota.activate_calls == 0
    assert ota.boot_selected is False
    assert ota.reboot_calls == 0

    clock[0] = 8000
    coordinator.tick()
    assert ota.activate_calls == 1
    assert ota.boot_selected is True
    assert ota.pending_sequence() == 2
    assert ota.reboot_calls == 1


def test_reboot_callback_failure_after_activate_preserves_boot_and_pending_metadata() -> None:
    def fail_reboot():
        raise OSError("reboot callback failed")

    coordinator, dependencies, clock = make_system(reboot=fail_reboot)
    tick_until(coordinator, "waiting_for_gates")
    clock[0] = 3000
    tick_until(coordinator, "awaiting_activation_release")
    dependencies["touch"].both_touched = False
    dependencies["touch"].touch_mask = 0
    clock[0] = 4000
    coordinator.tick()
    clock[0] = 4500
    coordinator.tick()
    dependencies["touch"].both_touched = True
    dependencies["touch"].touch_mask = 3
    clock[0] = 5000
    coordinator.tick()
    clock[0] = 8000
    coordinator.tick()

    ota = dependencies["ota_module"]
    status = coordinator.status()
    assert status["ota_state"] == "activated_reboot_failed"
    assert "activated but reboot failed" in status["ota_error"]
    assert ota.boot_selected is True
    assert ota.pending_sequence() == 2
    assert ota.phase() == "activated"
    assert ota.abort_calls == 0


def test_second_touch_must_also_be_continuous() -> None:
    coordinator, dependencies, clock = make_system()
    tick_until(coordinator, "waiting_for_gates")
    clock[0] = 3000
    tick_until(coordinator, "awaiting_activation_release")
    dependencies["touch"].both_touched = False
    dependencies["touch"].touch_mask = 0
    clock[0] = 4000
    coordinator.tick()
    clock[0] = 4500
    coordinator.tick()

    dependencies["touch"].both_touched = True
    dependencies["touch"].touch_mask = 3
    clock[0] = 5000
    coordinator.tick()
    clock[0] = 7000
    coordinator.tick()
    dependencies["touch"].both_touched = False
    dependencies["touch"].touch_mask = 0
    coordinator.tick()
    dependencies["touch"].both_touched = True
    dependencies["touch"].touch_mask = 3
    clock[0] = 7500
    coordinator.tick()
    clock[0] = 10499
    coordinator.tick()
    assert dependencies["ota_module"].activate_calls == 0
    clock[0] = 10500
    coordinator.tick()
    assert dependencies["ota_module"].activate_calls == 1


def test_gate_loss_during_install_aborts_and_keeps_oe_disabled() -> None:
    coordinator, dependencies, clock = make_system(image=b"x" * 8192)
    tick_until(coordinator, "waiting_for_gates")
    clock[0] = 3000
    tick_until(coordinator, "installing")
    coordinator.tick()
    dependencies["power"].values["charging"] = None

    coordinator.tick()

    assert coordinator.status()["ota_state"] == "aborted"
    assert "charging not confirmed" in coordinator.status()["ota_error"]
    assert dependencies["ota_module"].abort_calls == 1
    assert dependencies["servo_gate"].disabled is True


@pytest.mark.parametrize("failing_dependency", ["servo_gate", "ota_module"])
def test_abort_dependency_errors_do_not_escape_the_safety_tick(failing_dependency) -> None:
    coordinator, dependencies, clock = make_system(image=b"x" * 8192)
    tick_until(coordinator, "waiting_for_gates")
    clock[0] = 3000
    tick_until(coordinator, "installing")
    dependencies["power"].values["charging"] = None

    def fail(*_args):
        raise OSError("abort dependency failed")

    if failing_dependency == "servo_gate":
        dependencies["servo_gate"].set_disabled = fail
    else:
        dependencies["ota_module"].abort = fail

    coordinator.tick()

    assert coordinator.status()["ota_state"] == "failed"
    assert "charging not confirmed" in coordinator.status()["ota_error"]
    assert "abort dependency failed" in coordinator.status()["ota_error"]


def test_native_digest_failure_is_reported_and_does_not_reboot() -> None:
    coordinator, dependencies, clock = make_system()
    dependencies["ota_module"].finish_error = "firmware digest mismatch"
    tick_until(coordinator, "waiting_for_gates")
    clock[0] = 3000
    tick_until(coordinator, "failed")

    assert "digest mismatch" in coordinator.status()["ota_error"]
    assert dependencies["ota_module"].reboot_calls == 0


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda d: setattr(d["storage"], "available", False), "SD unavailable"),
        (lambda d: setattr(d["storage"], "mounted", False), "SD unavailable"),
        (lambda d: d["power"].values.update(available=False), "power unhealthy"),
        (lambda d: d["power"].values.update(fault=True), "power unhealthy"),
        (lambda d: d["power"].values.update(soc_precise=False), "precise SOC unavailable"),
        (lambda d: d["power"].values.update(source="ina226"), "precise SOC unavailable"),
        (lambda d: d["power"].values.update(battery_pct=49), "SOC below 50"),
        (lambda d: d["power"].values.update(charging=False), "charging not confirmed"),
        (lambda d: d["power"].values.update(charging=None), "charging not confirmed"),
        (lambda d: setattr(d["motion"], "current", object()), "motion not idle"),
        (lambda d: d["motion"].queue.append(object()), "motion not idle"),
        (lambda d: setattr(d["motion"], "motion_state", "walking"), "motion not idle"),
        (lambda d: setattr(d["servo_gate"], "available", False), "OE unavailable"),
        (lambda d: setattr(d["servo_gate"], "feedback", False), "OE disable unconfirmed"),
        (lambda d: setattr(d["reflex"], "state", "fault"), "reflex active"),
        (lambda d: setattr(d["reflex"], "authority", "reflex"), "reflex active"),
        (lambda d: setattr(d["reflex"], "emergency_stop", True), "reflex active"),
        (
            lambda d: setattr(d["reflex"], "shutdown_failed_latched", True),
            "reflex active",
        ),
        (lambda d: setattr(d["touch"], "available", False), "MPR121 unavailable"),
        (lambda d: setattr(d["touch"], "both_touched", False), "dual touch hold incomplete"),
    ],
)
def test_each_install_gate_loss_aborts(mutate, expected) -> None:
    coordinator, dependencies, clock = make_system(image=b"x" * 8192)
    tick_until(coordinator, "waiting_for_gates")
    clock[0] = 3000
    tick_until(coordinator, "installing")
    mutate(dependencies)

    coordinator.tick()

    expected_state = (
        "failed"
        if expected in ("OE unavailable", "OE disable unconfirmed")
        else "aborted"
    )
    assert coordinator.status()["ota_state"] == expected_state
    assert expected in coordinator.status()["ota_error"]
    assert dependencies["ota_module"].abort_calls == 1


def test_async_storage_read_times_out_after_five_seconds() -> None:
    coordinator, dependencies, clock = make_system()
    dependencies["storage"].stalled_files.add("manifest.json")
    coordinator.tick()
    clock[0] = 4999
    coordinator.tick()
    assert coordinator.status()["ota_state"] == "loading_manifest"

    clock[0] = 5000
    coordinator.tick()

    assert coordinator.status()["ota_state"] == "denied"
    assert "timeout" in coordinator.status()["ota_error"]


def test_firmware_chunk_read_also_times_out_after_five_seconds() -> None:
    coordinator, dependencies, clock = make_system(image=b"x" * 8192)
    tick_until(coordinator, "waiting_for_gates")
    dependencies["storage"].stalled_files.add("firmware.bin")
    clock[0] = 3000
    tick_until(coordinator, "installing")
    coordinator.tick()
    clock[0] = 7999
    coordinator.tick()
    assert coordinator.status()["ota_state"] == "installing"

    clock[0] = 8000
    coordinator.tick()

    assert coordinator.status()["ota_state"] == "aborted"
    assert "timeout" in coordinator.status()["ota_error"]


def test_storage_worker_stopped_rejects_update_request() -> None:
    coordinator, dependencies, _clock = make_system()
    dependencies["storage"].runtime_state = "stopped"

    coordinator.tick()

    assert coordinator.status()["ota_state"] == "denied"
    assert "not running" in coordinator.status()["ota_error"]


def test_storage_worker_starting_has_total_five_second_deadline() -> None:
    coordinator, dependencies, clock = make_system()
    dependencies["storage"].runtime_state = "starting"
    coordinator.tick()
    clock[0] = 4999
    coordinator.tick()
    assert coordinator.status()["ota_state"] == "loading_manifest"

    clock[0] = 5000
    coordinator.tick()

    assert coordinator.status()["ota_state"] == "denied"
    assert "startup timeout" in coordinator.status()["ota_error"]


def test_tick_is_top_level_fail_closed() -> None:
    coordinator, dependencies, _clock = make_system()

    def explode():
        raise OSError("unexpected native error")

    coordinator._update_touch_hold = explode
    result = coordinator.tick()

    assert result["ota_state"] == "failed"
    assert "unexpected native error" in result["ota_error"]
    assert dependencies["servo_gate"].disabled is True


def test_abort_cleanup_failure_reports_combined_failed_error() -> None:
    coordinator, dependencies, clock = make_system(image=b"x" * 8192)
    tick_until(coordinator, "waiting_for_gates")
    clock[0] = 3000
    tick_until(coordinator, "installing")
    dependencies["power"].values["charging"] = False

    def fail_oe(*_args):
        raise OSError("OE cleanup failed")

    def fail_abort():
        raise OSError("native abort failed")

    dependencies["servo_gate"].set_disabled = fail_oe
    dependencies["ota_module"].abort = fail_abort
    coordinator.tick()

    status = coordinator.status()
    assert status["ota_state"] == "failed"
    assert "charging not confirmed" in status["ota_error"]
    assert "OE cleanup failed" in status["ota_error"]
    assert "native abort failed" in status["ota_error"]


def test_pending_verify_exception_is_unknown_and_reported() -> None:
    coordinator, dependencies, _clock = make_system()

    def fail_pending():
        raise OSError("NVS read failed")

    dependencies["ota_module"].pending_verify = fail_pending
    status = coordinator.status()

    assert status["ota_pending_verify"] == "unknown"
    assert "NVS read failed" in status["ota_error"]


def test_trailing_firmware_byte_is_rejected_before_finish() -> None:
    coordinator, dependencies, clock = make_system(image=b"abcdefgh")
    dependencies["storage"].files[("2", "firmware.bin")] += b"x"
    tick_until(coordinator, "waiting_for_gates")
    clock[0] = 3000
    tick_until(coordinator, "aborted")

    assert "exceeds manifest size" in coordinator.status()["ota_error"]
    assert dependencies["ota_module"].finish_calls == 0
    assert dependencies["ota_module"].boot_selected is False


@pytest.mark.parametrize("operation", ["begin", "write", "finish", "activate"])
def test_native_operation_errors_are_fail_closed(operation) -> None:
    coordinator, dependencies, clock = make_system()
    ota = dependencies["ota_module"]

    def explode(*_args):
        raise OSError(f"native {operation} failed")

    setattr(ota, operation, explode)
    tick_until(coordinator, "waiting_for_gates")
    clock[0] = 3000
    if operation == "activate":
        tick_until(coordinator, "awaiting_activation_release")
        dependencies["touch"].both_touched = False
        dependencies["touch"].touch_mask = 0
        clock[0] = 4000
        coordinator.tick()
        clock[0] = 4500
        coordinator.tick()
        dependencies["touch"].both_touched = True
        dependencies["touch"].touch_mask = 3
        clock[0] = 5000
        coordinator.tick()
        clock[0] = 8000
        coordinator.tick()
    else:
        tick_until(coordinator, "failed")

    assert coordinator.status()["ota_state"] == "failed"
    assert f"native {operation} failed" in coordinator.status()["ota_error"]
    assert ota.reboot_calls == 0


def test_touch_exposes_stable_mask_without_changing_edge_events() -> None:
    from hardware.touch import TouchInputs

    class Bus:
        values = [b"\x03\x00", b"\x03\x00", b"\x03\x00"]

        @staticmethod
        def scan():
            return [0x5A]

        @staticmethod
        def writeto_mem(*_args):
            return None

        def readfrom_mem(self, *_args):
            return self.values.pop(0)

    touch = TouchInputs(i2c=Bus())
    assert touch.touch_mask == 0
    assert touch.both_touched is False
    assert touch.poll() is None
    assert touch.poll() == "touch_head"
    assert touch.touch_mask == 3
    assert touch.both_touched is True
    assert touch.poll() == "touch_back"


def test_native_module_surface_build_chain_and_rollback_config() -> None:
    source_path = NATIVE_ROOT / "modnao_ota.c"
    cmake_path = NATIVE_ROOT / "micropython.cmake"
    assert source_path.exists(), "nao_ota 原生模块尚未实现"
    assert cmake_path.exists(), "nao_ota CMake 链尚未实现"
    source = source_path.read_text(encoding="utf-8")
    cmake = cmake_path.read_text(encoding="utf-8")
    camera_cmake = (
        FIRMWARE_ROOT / "build" / "camera_module" / "micropython.cmake"
    ).read_text(encoding="utf-8")
    sdkconfig = (FIRMWARE_ROOT / "build" / "sdkconfig.board").read_text(encoding="utf-8")
    build_script = (FIRMWARE_ROOT / "build" / "build.ps1").read_text(encoding="utf-8")

    for api in (
        "verify_manifest",
        "begin",
        "write",
        "finish",
        "activate",
        "abort",
        "status",
        "pending_verify",
        "current_sequence",
        "pending_sequence",
        "phase",
        "mark_healthy",
        "rollback_and_reboot",
    ):
        assert f"MP_QSTR_{api}" in source
    for symbol in (
        "esp_ota_get_next_update_partition",
        "OTA_WITH_SEQUENTIAL_WRITES",
        "esp_ota_write",
        "esp_ota_end",
        "esp_ota_set_boot_partition",
        "mbedtls_pk_verify",
        "mbedtls_sha256_update",
        "nvs_open",
        "nvs_get_u32",
        "nvs_set_u32",
        "nvs_erase_key",
        "nvs_commit",
        "MBEDTLS_ECP_DP_SECP256R1",
        "esp_ota_get_boot_partition",
        "MBEDTLS_PRIVATE(grp).id",
    ):
        assert symbol in source
    verify_body = source[
        source.index("static mp_obj_t nao_ota_verify_manifest")
        : source.index("static MP_DEFINE_CONST_FUN_OBJ_2(nao_ota_verify_manifest_obj")
    ]
    assert "mbedtls_pk_get_type" in verify_body
    assert "result = -1" in verify_body
    assert "MBEDTLS_PK_ECKEY" in verify_body
    assert "ec_key->MBEDTLS_PRIVATE(grp).id == MBEDTLS_ECP_DP_SECP256R1" in verify_body
    finish_body = source[
        source.index("static mp_obj_t nao_ota_finish")
        : source.index("static MP_DEFINE_CONST_FUN_OBJ_0(nao_ota_finish_obj")
    ]
    activate_body = source[
        source.index("static mp_obj_t nao_ota_activate")
        : source.index("static MP_DEFINE_CONST_FUN_OBJ_0(nao_ota_activate_obj")
    ]
    assert "esp_ota_set_boot_partition" not in finish_body
    assert "esp_ota_set_boot_partition" in activate_body
    assert "naobot_nvs_write_transaction" in activate_body
    assert "NAOBOT_OTA_PENDING_SEQUENCE_KEY" in source
    assert "static MP_DEFINE_CONST_FUN_OBJ_3(nao_ota_begin_obj" in source
    assert "static MP_DEFINE_CONST_FUN_OBJ_2(nao_ota_begin_obj" not in source
    assert "MP_REGISTER_MODULE(MP_QSTR_nao_ota" in source
    assert "target_link_libraries(usermod INTERFACE usermod_ota)" in cmake
    assert "__idf_nvs_flash" in cmake
    assert "ota_module/micropython.cmake" in camera_cmake
    assert "CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y" in sdkconfig
    assert "OtaPublicKeyHeader" in build_script
    assert "validate_ota_public_key.py" in build_script
    assert "0x280000" in build_script


def test_native_sequence_promotion_and_rollback_are_fail_closed() -> None:
    source = (NATIVE_ROOT / "modnao_ota.c").read_text(encoding="utf-8")
    begin_body = source[
        source.index("static mp_obj_t nao_ota_begin")
        : source.index("static MP_DEFINE_CONST_FUN_OBJ_3(nao_ota_begin_obj")
    ]
    mark_body = source[
        source.index("static mp_obj_t nao_ota_mark_healthy")
        : source.index("static MP_DEFINE_CONST_FUN_OBJ_0(nao_ota_mark_healthy_obj")
    ]
    rollback_body = source[
        source.index("static mp_obj_t nao_ota_rollback_and_reboot")
        : source.index("static MP_DEFINE_CONST_FUN_OBJ_0(nao_ota_rollback_and_reboot_obj")
    ]

    assert "mp_obj_is_int" in source
    assert "uint32_t sequence" in begin_body
    assert "naobot_get_uint32" in begin_body
    assert "sequence <= current_sequence" in begin_body
    assert "OTA sequence must be uint32" in begin_body
    assert "NAOBOT_OTA_CURRENT_SEQUENCE_KEY" in source
    assert "NAOBOT_OTA_PENDING_SEQUENCE_KEY" in source
    assert "NAOBOT_OTA_PHASE_CONFIRMING" in mark_body
    confirming_commit = mark_body.index("naobot_nvs_begin_confirming")
    mark_valid = mark_body.index("esp_ota_mark_app_valid_cancel_rollback")
    clear_transaction = mark_body.index("naobot_nvs_clear_transaction")
    assert confirming_commit < mark_valid < clear_transaction
    assert "naobot_nvs_clear_transaction" in rollback_body
    assert rollback_body.index("naobot_nvs_clear_transaction") < rollback_body.index(
        "esp_ota_mark_app_invalid_rollback_and_reboot"
    )


def test_native_activation_transaction_and_recovery_are_persistent_and_idempotent() -> None:
    source = (NATIVE_ROOT / "modnao_ota.c").read_text(encoding="utf-8")
    activate_body = source[
        source.index("static mp_obj_t nao_ota_activate")
        : source.index("static MP_DEFINE_CONST_FUN_OBJ_0(nao_ota_activate_obj")
    ]
    recovery_body = source[
        source.index("static esp_err_t naobot_ota_recover_transaction")
        : source.index("static mp_obj_t nao_ota_verify_manifest")
    ]

    for phase in ("PREPARED", "ACTIVATED", "CONFIRMING", "ROLLBACK"):
        assert f"NAOBOT_OTA_PHASE_{phase}" in source
    prepared = activate_body.index("NAOBOT_OTA_PHASE_PREPARED")
    select_boot = activate_body.index("esp_ota_set_boot_partition")
    activated = activate_body.index("NAOBOT_OTA_PHASE_ACTIVATED")
    assert prepared < select_boot < activated
    assert "naobot_partition_state" in recovery_body
    assert "transaction.target_address" in recovery_body
    assert "esp_ota_get_state_partition" in source
    assert "NAOBOT_OTA_TARGET_ADDRESS_KEY" in source
    for symbol in (
        "esp_ota_get_running_partition",
        "esp_ota_get_boot_partition",
        "NAOBOT_OTA_PHASE_PREPARED",
        "NAOBOT_OTA_PHASE_ACTIVATED",
        "NAOBOT_OTA_PHASE_CONFIRMING",
        "NAOBOT_OTA_PHASE_ROLLBACK",
        "esp_ota_mark_app_valid_cancel_rollback",
    ):
        assert symbol in recovery_body


def test_native_abort_is_limited_to_writing_or_staged_and_checks_native_result() -> None:
    source = (NATIVE_ROOT / "modnao_ota.c").read_text(encoding="utf-8")
    abort_body = source[
        source.index("static mp_obj_t nao_ota_abort")
        : source.index("static MP_DEFINE_CONST_FUN_OBJ_0(nao_ota_abort_obj")
    ]
    assert "ota_state != NAOBOT_OTA_WRITING && ota_state != NAOBOT_OTA_STAGED" in abort_body
    assert "esp_ota_abort" in abort_body
    assert "abort_result != ESP_OK" in abort_body
    assert "esp_ota_set_boot_partition" not in abort_body
    assert "naobot_nvs_clear_transaction" not in abort_body


def test_repository_contains_only_replaceable_non_production_public_key_material() -> None:
    public_header = NATIVE_ROOT / "ota_public_key_dev.h"
    assert public_header.exists(), "仓库开发公钥尚未提供"
    text = public_header.read_text(encoding="ascii")
    assert "NON_PRODUCTION" in text
    assert "BEGIN PUBLIC KEY" in text
    assert "PRIVATE KEY" not in text
    cmake = (NATIVE_ROOT / "micropython.cmake").read_text(encoding="utf-8")
    assert "NAOBOT_OTA_PUBLIC_KEY_HEADER" in cmake

    private_marker = b"BEGIN " + b"PRIVATE KEY"
    for root in (FIRMWARE_ROOT, ROOT / "tools"):
        for path in root.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                assert private_marker not in path.read_bytes(), f"private key material in {path}"


def test_main_status_exposes_ota_health_fields() -> None:
    import main as firmware_main

    class Power:
        battery_pct = 80

    class Imu:
        posture = "upright"

    ota = {
        "ota_state": "waiting_for_gates",
        "ota_progress_pct": 0,
        "ota_error": None,
        "ota_pending_verify": True,
        "ota_sequence": 2,
    }
    payload = firmware_main.FirmwareProtocol("ota-test").heartbeat(
        Power(), Imu(), state={"ota": ota}
    )["payload"]
    assert {key: payload[key] for key in ota} == ota


def test_main_integrates_boot_health_before_motion_tick() -> None:
    source = (FIRMWARE_ROOT / "main.py").read_text(encoding="utf-8")
    assert "from update.boot_health import BootHealthMonitor" in source
    assert "boot_health = BootHealthMonitor(" in source
    assert "OTA_CURRENT_SEQUENCE" not in source
    assert source.index("boot_health.tick()") < source.index("motion.tick()", source.index("while True:"))
