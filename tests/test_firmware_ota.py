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
        self.requests = []

    def snapshot(self):
        return {"available": self.available, "mounted": self.mounted}

    def submit_update_read(self, sequence, filename, offset=0, max_bytes=None):
        self.requests.append((str(sequence), filename, offset, max_bytes))
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

    def verify_manifest(self, manifest_bytes, signature_der):
        assert isinstance(manifest_bytes, bytes)
        assert isinstance(signature_der, bytes)
        return self.valid_signature

    def begin(self, image_size, expected_sha256_bytes):
        self.begin_args = (image_size, expected_sha256_bytes)
        return True

    def write(self, chunk):
        self.writes.append(bytes(chunk))
        return len(chunk)

    def finish(self):
        self.finish_calls += 1
        if self.finish_error:
            raise OSError(self.finish_error)
        return True

    def abort(self):
        self.abort_calls += 1
        return True

    def pending_verify(self):
        return self.pending

    def status(self):
        return {"state": "idle"}


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
        "current_sequence": 1,
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


def test_streaming_is_chunk_bounded_finishes_without_automatic_reboot() -> None:
    image = b"x" * 9000
    coordinator, dependencies, clock = make_system(image=image, chunk_size=4096)
    tick_until(coordinator, "waiting_for_gates")
    clock[0] = 3000
    tick_until(coordinator, "ready_to_reboot")

    ota = dependencies["ota_module"]
    assert [len(chunk) for chunk in ota.writes] == [4096, 4096, 808]
    assert all(length is None or length <= 4096 for *_prefix, length in dependencies["storage"].requests)
    assert ota.finish_calls == 1
    assert ota.reboot_calls == 0
    assert coordinator.status() == {
        "ota_state": "ready_to_reboot",
        "ota_progress_pct": 100,
        "ota_error": None,
        "ota_pending_verify": False,
        "ota_sequence": 2,
    }
    coordinator.tick()
    assert ota.reboot_calls == 0


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

    assert coordinator.status()["ota_state"] == "aborted"
    assert "charging not confirmed" in coordinator.status()["ota_error"]


def test_native_digest_failure_is_reported_and_does_not_reboot() -> None:
    coordinator, dependencies, clock = make_system()
    dependencies["ota_module"].finish_error = "firmware digest mismatch"
    tick_until(coordinator, "waiting_for_gates")
    clock[0] = 3000
    tick_until(coordinator, "failed")

    assert "digest mismatch" in coordinator.status()["ota_error"]
    assert dependencies["ota_module"].reboot_calls == 0


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
        "abort",
        "status",
        "pending_verify",
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
    ):
        assert symbol in source
    assert "esp_restart(" not in source
    assert "MP_REGISTER_MODULE(MP_QSTR_nao_ota" in source
    assert "target_link_libraries(usermod INTERFACE usermod_ota)" in cmake
    assert "ota_module/micropython.cmake" in camera_cmake
    assert "CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y" in sdkconfig
    assert "OtaPublicKeyHeader" in build_script
    assert "0x280000" in build_script


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
