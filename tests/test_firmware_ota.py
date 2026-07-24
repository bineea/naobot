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
OTA_WORKER_PATH = FIRMWARE_ROOT / "update" / "ota_worker.py"
NATIVE_ROOT = FIRMWARE_ROOT / "build" / "ota_module"
if str(FIRMWARE_ROOT) not in sys.path:
    sys.path.insert(0, str(FIRMWARE_ROOT))

firmware_main = __import__("main")
from control.motion_controller import MotionController  # noqa: E402
from motion.action_player import ActionPlayer  # noqa: E402


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
        self.motion_inhibited = False
        self.motion_inhibit_reason = None

    def cancel(self, reason):
        self.cancelled.append(reason)
        self.current = None
        self.queue = []

    def set_motion_inhibited(self, inhibited, reason):
        self.motion_inhibited = bool(inhibited)
        self.motion_inhibit_reason = reason if inhibited else None
        if inhibited:
            self.cancel(reason)
        return True


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

    @staticmethod
    def check():
        return False


class FakeTouch:
    available = True
    touch_mask = 0x03
    both_touched = True


class GateBackedServos:
    def __init__(self, gate):
        self.gate = gate
        self.enabled = False
        self.pose_calls = 0

    def pose(self, _positions):
        self.pose_calls += 1
        if not self.gate.set_disabled(False):
            return False
        self.enabled = True
        return True

    def stop(self):
        disabled = self.gate.set_disabled(True)
        self.enabled = not disabled
        return disabled


class PassiveDisplay:
    def set_face(self, _face):
        return None


class AllowAllSafety:
    @staticmethod
    def can_execute(_action):
        return True


class RecordingWs:
    def __init__(self):
        self.sent = []

    def send_json(self, payload):
        self.sent.append(payload)
        return True


def make_real_motion(gate, clock):
    servos = GateBackedServos(gate)
    actions = ActionPlayer(servos, PassiveDisplay())
    motion = MotionController(
        actions,
        AllowAllSafety(),
        FakeReflex(),
        lambda: clock[0],
    )
    return motion, actions, servos


class FakeOta:
    def __init__(self):
        self.valid_signature = True
        self.writes = []
        self.begin_args = None
        self.begin_error = None
        self.abort_calls = 0
        self.abort_results = []
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
        self.running_target_value = False
        self.native_active = False
        self.session_active_error = None

    def verify_manifest(self, manifest_bytes, signature_der):
        assert isinstance(manifest_bytes, bytes)
        assert isinstance(signature_der, bytes)
        return self.valid_signature

    def begin(self, image_size, expected_sha256_bytes, sequence):
        self.begin_args = (image_size, expected_sha256_bytes, sequence)
        self.native_active = True
        if self.begin_error:
            raise OSError(self.begin_error)
        return True

    def write(self, chunk):
        self.writes.append(bytes(chunk))
        return len(chunk)

    def finish(self):
        self.finish_calls += 1
        if self.finish_error:
            raise OSError(self.finish_error)
        self.staged = True
        self.native_active = False
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
        result = self.abort_results.pop(0) if self.abort_results else True
        if result is not True:
            return result
        self.native_active = False
        self.staged = False
        return True

    def session_active(self):
        if self.session_active_error:
            raise OSError(self.session_active_error)
        return self.native_active

    def pending_verify(self):
        return self.pending

    def current_sequence(self):
        return self.current

    def pending_sequence(self):
        return self.pending_sequence_value

    def phase(self):
        return self.phase_value

    def running_target(self):
        return self.running_target_value

    def status(self):
        return {
            "state": "ready_to_reboot" if self.boot_selected else "idle",
        }


class FakeOtaWorker:
    def __init__(self, ota):
        self.ota = ota
        self.runtime_state = "running"
        self.pending = None
        self.stalled_operations = set()

    def snapshot(self):
        return {
            "runtime_state": self.runtime_state,
            "busy": self.pending is not None,
            "last_error": None,
        }

    def submit(self, operation):
        if self.runtime_state != "running":
            return {"accepted": False, "reason": "OTA worker not running"}
        if self.pending is not None:
            return {"accepted": False, "reason": "OTA worker busy"}
        request = {
            "accepted": True,
            "operation": operation,
            "done": False,
            "result": None,
            "error": None,
        }
        self.pending = request
        return request

    @staticmethod
    def poll(request):
        return dict(request)

    def complete_pending(self):
        request = self.pending
        if request is None or request["operation"] in self.stalled_operations:
            return False
        try:
            request["result"] = getattr(self.ota, request["operation"])()
        except Exception as exc:
            request["error"] = str(exc)
        request["done"] = True
        self.pending = None
        return True


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
    dependencies["ota_worker"] = FakeOtaWorker(dependencies["ota_module"])
    dependencies.update(coordinator_kwargs)
    coordinator = module.UpdateCoordinator(**dependencies)
    assert coordinator.request_install(sequence) is True
    return coordinator, dependencies, clock


def tick_until(coordinator, state, limit=30):
    for _ in range(limit):
        worker = getattr(coordinator, "ota_worker", None)
        if hasattr(worker, "complete_pending"):
            worker.complete_pending()
        coordinator.tick()
        if coordinator.status()["ota_state"] == state:
            return
    pytest.fail(f"OTA state did not reach {state}: {coordinator.status()}")


def complete_native_operation(coordinator):
    assert coordinator.ota_worker.complete_pending() is True
    return coordinator.tick()


def settle_cleanup(coordinator, limit=10):
    for _ in range(limit):
        coordinator.ota_worker.complete_pending()
        coordinator.tick()
        if not coordinator._cleanup_pending:
            return coordinator.status()
    pytest.fail(f"OTA cleanup did not settle: {coordinator.status()}")


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
    assert dependencies["motion"].motion_inhibited is False


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
    assert coordinator.status()["ota_state"] == "activating"
    assert ota.activate_calls == 0
    assert coordinator.tick()["ota_state"] == "activating"
    assert ota.activate_calls == 0
    complete_native_operation(coordinator)
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
    assert dependencies["ota_module"].activate_calls == 0
    complete_native_operation(coordinator)

    ota = dependencies["ota_module"]
    status = coordinator.status()
    assert status["ota_state"] == "activated_reboot_failed"
    assert "activated but reboot failed" in status["ota_error"]
    assert ota.boot_selected is True
    assert ota.pending_sequence() == 2
    assert ota.phase() == "activated"
    assert ota.abort_calls == 0
    assert dependencies["motion"].motion_inhibited is True


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
    assert coordinator.status()["ota_state"] == "activating"
    assert dependencies["ota_module"].activate_calls == 0
    complete_native_operation(coordinator)
    assert dependencies["ota_module"].activate_calls == 1


def test_gate_loss_during_install_aborts_and_keeps_oe_disabled() -> None:
    coordinator, dependencies, clock = make_system(image=b"x" * 8192)
    tick_until(coordinator, "waiting_for_gates")
    clock[0] = 3000
    tick_until(coordinator, "installing")
    coordinator.tick()
    dependencies["power"].values["charging"] = None

    coordinator.tick()
    assert dependencies["ota_module"].abort_calls == 0
    assert dependencies["motion"].motion_inhibited is True
    settle_cleanup(coordinator)

    assert coordinator.status()["ota_state"] == "aborted"
    assert "charging not confirmed" in coordinator.status()["ota_error"]
    assert dependencies["ota_module"].abort_calls == 1
    assert dependencies["servo_gate"].disabled is True
    assert dependencies["motion"].motion_inhibited is False


def test_ota_motion_lock_blocks_network_intent_and_keeps_shared_output_disabled() -> None:
    clock = [0]
    gate = FakeGate()
    motion, actions, servos = make_real_motion(gate, clock)
    coordinator, dependencies, coordinator_clock = make_system(
        motion=motion,
        servo_gate=gate,
        clock_ms=lambda: clock[0],
    )
    assert coordinator_clock == [0]
    assert dependencies["motion"] is motion
    assert motion.motion_inhibited is True
    assert gate.disabled is True

    ws = RecordingWs()
    message = {
        "id": "ota-blocked-intent",
        "type": "intent",
        "ts_ms": 1_000,
        "deadline_ms": 1_000,
        "payload": {"actions": [{"name": "wave", "args": {"level": 1}}]},
    }
    firmware_main.execute_intent(
        message,
        actions,
        AllowAllSafety(),
        firmware_main.FirmwareProtocol("ota-test"),
        ws,
        motion=motion,
        reflex=FakeReflex(),
        state={"last_host_ts_ms": 1_000, "last_host_clock_seen_ms": 0},
        clock=lambda: 0,
    )
    motion.tick()

    assert ws.sent[-1]["type"] == "error"
    assert ws.sent[-1]["payload"]["code"] == "EXECUTION_FAILED"
    assert "motion inhibited: ota" in ws.sent[-1]["payload"]["message"]
    assert motion.current is None
    assert motion.queue == []
    assert servos.pose_calls == 0
    assert servos.enabled is False
    assert gate.disabled is True

    stop = {
        "id": "ota-stop",
        "type": "intent",
        "payload": {"actions": [{"name": "stop", "args": {}}]},
    }
    firmware_main.execute_intent(
        stop,
        actions,
        AllowAllSafety(),
        firmware_main.FirmwareProtocol("ota-test"),
        ws,
        motion=motion,
        reflex=None,
    )
    assert ws.sent[-1]["type"] == "ack"
    assert gate.disabled is True
    assert coordinator.status()["ota_state"] == "loading_manifest"


def test_successful_native_abort_releases_motion_lock_and_actions_can_start_again() -> None:
    clock = [0]
    gate = FakeGate()
    motion, _actions, servos = make_real_motion(gate, clock)
    coordinator, dependencies, _ = make_system(
        image=b"x" * 8192,
        motion=motion,
        servo_gate=gate,
        clock_ms=lambda: clock[0],
    )
    tick_until(coordinator, "waiting_for_gates")
    clock[0] = 3000
    tick_until(coordinator, "installing")
    dependencies["power"].values["charging"] = False

    coordinator.tick()
    assert dependencies["ota_module"].abort_calls == 0
    assert motion.motion_inhibited is True
    settle_cleanup(coordinator)

    assert coordinator.status()["ota_state"] == "aborted"
    assert dependencies["ota_module"].abort_calls == 1
    assert motion.motion_inhibited is False
    accepted, reason = motion.submit_action({"name": "wave", "args": {"level": 1}})
    assert accepted, reason
    motion.tick()
    assert servos.pose_calls == 1
    assert servos.enabled is True


def test_failed_native_abort_keeps_motion_lock_until_retry_is_confirmed() -> None:
    coordinator, dependencies, clock = make_system(image=b"x" * 8192)
    tick_until(coordinator, "waiting_for_gates")
    clock[0] = 3000
    tick_until(coordinator, "installing")
    dependencies["power"].values["charging"] = False
    dependencies["ota_module"].abort_results = [False, True]

    coordinator.tick()
    complete_native_operation(coordinator)

    assert coordinator.status()["ota_state"] == "failed"
    assert dependencies["motion"].motion_inhibited is True

    coordinator.tick()
    complete_native_operation(coordinator)
    assert dependencies["motion"].motion_inhibited is False


def test_new_request_is_rejected_until_failed_native_abort_retry_completes() -> None:
    coordinator, dependencies, clock = make_system(image=b"x" * 8192)
    ota = dependencies["ota_module"]
    ota.abort_results = [False, True]
    tick_until(coordinator, "waiting_for_gates")
    clock[0] = 3000
    tick_until(coordinator, "installing")
    dependencies["power"].values["charging"] = False

    coordinator.tick()
    if dependencies["ota_worker"].pending is not None:
        complete_native_operation(coordinator)

    assert ota.abort_calls == 1
    assert ota.session_active() is True
    assert dependencies["motion"].motion_inhibited is True
    assert coordinator.request_install(3) is False

    coordinator.tick()
    assert dependencies["ota_worker"].pending["operation"] == "abort"
    complete_native_operation(coordinator)

    assert ota.abort_calls == 2
    assert ota.session_active() is False
    assert dependencies["motion"].motion_inhibited is False
    assert coordinator.request_install(3) is True


def test_partial_begin_abort_failure_is_retried_before_motion_unlock() -> None:
    coordinator, dependencies, clock = make_system()
    ota = dependencies["ota_module"]
    ota.begin_error = "sha256 initialization failed; esp_ota_abort failed"
    ota.abort_results = [False, True]
    tick_until(coordinator, "waiting_for_gates")
    clock[0] = 3000

    coordinator.tick()

    assert ota.session_active() is True
    assert dependencies["motion"].motion_inhibited is True
    assert dependencies["ota_worker"].pending["operation"] == "abort"
    assert ota.abort_calls == 0

    complete_native_operation(coordinator)
    assert ota.session_active() is True
    assert dependencies["motion"].motion_inhibited is True

    coordinator.tick()
    complete_native_operation(coordinator)
    assert ota.session_active() is False
    assert dependencies["motion"].motion_inhibited is False
    assert "sha256 initialization failed" in coordinator.status()["ota_error"]


def test_new_coordinator_detects_and_cleans_orphan_native_session() -> None:
    module = load_coordinator_module()
    ota = FakeOta()
    ota.native_active = True
    motion = FakeMotion()
    worker = FakeOtaWorker(ota)
    coordinator = module.UpdateCoordinator(
        FakeStorage({}),
        FakePower(),
        motion,
        FakeGate(),
        FakeReflex(),
        FakeTouch(),
        ota_module=ota,
        ota_worker=worker,
        clock_ms=lambda: 0,
        reboot=lambda: None,
    )

    coordinator.tick()

    assert motion.motion_inhibited is True
    assert worker.pending["operation"] == "abort"
    assert ota.abort_calls == 0
    complete_native_operation(coordinator)
    assert ota.session_active() is False
    assert motion.motion_inhibited is False


def test_unknown_native_session_state_rejects_request_and_stays_fail_closed() -> None:
    coordinator, dependencies, _clock = make_system()
    coordinator._state = "idle"
    coordinator._release_motion_lock()
    dependencies["ota_module"].session_active_error = "native state unavailable"

    assert coordinator.request_install(3) is False
    assert dependencies["motion"].motion_inhibited is True

    coordinator.tick()
    if dependencies["ota_worker"].pending is not None:
        complete_native_operation(coordinator)
    assert dependencies["motion"].motion_inhibited is True
    assert "native state unavailable" in coordinator.status()["ota_error"]


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
    if dependencies["ota_worker"].pending is not None:
        complete_native_operation(coordinator)

    assert coordinator.status()["ota_state"] == "failed"
    assert "charging not confirmed" in coordinator.status()["ota_error"]
    assert "abort dependency failed" in coordinator.status()["ota_error"]


def test_persistent_cleanup_poll_error_does_not_stop_l0_and_retries_next_ticks() -> None:
    coordinator, dependencies, clock = make_system(image=b"x" * 8192)
    tick_until(coordinator, "waiting_for_gates")
    clock[0] = 3000
    tick_until(coordinator, "installing")
    dependencies["power"].values["charging"] = False
    coordinator.tick()

    worker = dependencies["ota_worker"]
    original_poll = worker.poll

    def fail_poll(_request):
        raise OSError("cleanup poll unavailable")

    worker.poll = fail_poll
    l0_ticks = 0
    for _ in range(3):
        coordinator.tick()
        l0_ticks += 1
        assert dependencies["motion"].motion_inhibited is True
        assert dependencies["servo_gate"].disabled is True
        assert dependencies["ota_module"].session_active() is True

    assert l0_ticks == 3
    assert "cleanup poll unavailable" in coordinator.status()["ota_error"]

    worker.poll = original_poll
    assert worker.complete_pending() is True
    coordinator.tick()

    assert dependencies["ota_module"].session_active() is False
    assert dependencies["motion"].motion_inhibited is False


def test_cleanup_submit_error_is_contained_and_retried_next_tick() -> None:
    coordinator, dependencies, clock = make_system(image=b"x" * 8192)
    tick_until(coordinator, "waiting_for_gates")
    clock[0] = 3000
    tick_until(coordinator, "installing")
    dependencies["power"].values["charging"] = False

    worker = dependencies["ota_worker"]
    original_submit = worker.submit

    def fail_submit(_operation):
        raise OSError("cleanup submit unavailable")

    worker.submit = fail_submit
    for _ in range(2):
        coordinator.tick()
        assert dependencies["motion"].motion_inhibited is True
        assert dependencies["servo_gate"].disabled is True
        assert dependencies["ota_module"].session_active() is True

    assert "cleanup submit unavailable" in coordinator.status()["ota_error"]

    worker.submit = original_submit
    coordinator.tick()
    assert worker.pending["operation"] == "abort"
    assert worker.complete_pending() is True
    coordinator.tick()

    assert dependencies["ota_module"].session_active() is False
    assert dependencies["motion"].motion_inhibited is False


def test_cleanup_motion_release_error_is_contained_until_release_succeeds() -> None:
    coordinator, dependencies, clock = make_system(image=b"x" * 8192)
    tick_until(coordinator, "waiting_for_gates")
    clock[0] = 3000
    tick_until(coordinator, "installing")
    dependencies["power"].values["charging"] = False
    coordinator.tick()

    worker = dependencies["ota_worker"]
    assert worker.complete_pending() is True
    motion = dependencies["motion"]
    original_set_motion_inhibited = motion.set_motion_inhibited

    def fail_release(inhibited, reason):
        if not inhibited:
            raise OSError("motion release unavailable")
        return original_set_motion_inhibited(inhibited, reason)

    motion.set_motion_inhibited = fail_release
    coordinator.tick()

    assert dependencies["ota_module"].session_active() is False
    assert motion.motion_inhibited is True
    assert dependencies["servo_gate"].disabled is True
    assert "motion release unavailable" in coordinator.status()["ota_error"]

    motion.set_motion_inhibited = original_set_motion_inhibited
    coordinator.tick()

    assert motion.motion_inhibited is False
    assert coordinator._cleanup_pending is False


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
    complete_native_operation(coordinator)

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
    settle_cleanup(coordinator)

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
    complete_native_operation(coordinator)

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


def test_finish_is_submitted_and_pending_ticks_never_call_native_synchronously() -> None:
    coordinator, dependencies, clock = make_system()
    dependencies["ota_worker"].stalled_operations.add("finish")
    tick_until(coordinator, "waiting_for_gates")
    clock[0] = 3000
    tick_until(coordinator, "finalizing")

    assert dependencies["ota_module"].finish_calls == 0
    pending = coordinator.tick()
    assert pending["ota_state"] == "finalizing"
    assert dependencies["ota_module"].finish_calls == 0

    dependencies["ota_worker"].stalled_operations.clear()
    complete_native_operation(coordinator)
    assert coordinator.status()["ota_state"] == "awaiting_activation_release"
    assert dependencies["ota_module"].finish_calls == 1


def test_gate_loss_during_background_finish_defers_abort_until_worker_completes() -> None:
    coordinator, dependencies, clock = make_system()
    dependencies["ota_worker"].stalled_operations.add("finish")
    tick_until(coordinator, "waiting_for_gates")
    clock[0] = 3000
    tick_until(coordinator, "finalizing")
    dependencies["power"].values["charging"] = False

    coordinator.tick()

    assert dependencies["ota_module"].finish_calls == 0
    assert dependencies["ota_module"].abort_calls == 0
    assert dependencies["servo_gate"].disabled is True

    dependencies["ota_worker"].stalled_operations.clear()
    complete_native_operation(coordinator)
    assert dependencies["ota_module"].finish_calls == 1
    complete_native_operation(coordinator)
    assert dependencies["ota_module"].abort_calls == 1
    assert "charging not confirmed" in coordinator.status()["ota_error"]


def test_background_finish_timeout_is_fail_closed_without_concurrent_abort() -> None:
    coordinator, dependencies, clock = make_system()
    dependencies["ota_worker"].stalled_operations.add("finish")
    tick_until(coordinator, "waiting_for_gates")
    clock[0] = 3000
    tick_until(coordinator, "finalizing")

    clock[0] = 7999
    coordinator.tick()
    assert coordinator.status()["ota_state"] == "finalizing"
    clock[0] = 8000
    result = coordinator.tick()

    assert result["ota_state"] == "finalize_timeout"
    assert "finish timeout" in result["ota_error"]
    assert dependencies["ota_module"].finish_calls == 0
    assert dependencies["ota_module"].abort_calls == 0
    assert dependencies["servo_gate"].disabled is True
    assert dependencies["motion"].motion_inhibited is True


def test_unavailable_ota_worker_keeps_native_session_and_motion_lock() -> None:
    coordinator, dependencies, clock = make_system()
    dependencies["ota_worker"].runtime_state = "disabled"
    tick_until(coordinator, "waiting_for_gates")
    clock[0] = 3000
    tick_until(coordinator, "failed")

    assert "OTA worker not running" in coordinator.status()["ota_error"]
    assert dependencies["ota_module"].finish_calls == 0
    assert dependencies["ota_module"].abort_calls == 0
    assert dependencies["servo_gate"].disabled is True
    assert dependencies["motion"].motion_inhibited is True


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
        complete_native_operation(coordinator)
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
        "session_active",
        "status",
        "pending_verify",
        "current_sequence",
        "pending_sequence",
        "phase",
        "running_target",
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
    assert "target_link_libraries(usermod_ota" not in cmake
    for idf_target in ("__idf_app_update", "__idf_mbedtls", "__idf_nvs_flash"):
        assert idf_target not in cmake
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
    assert "transaction.target_found" in mark_body
    target_check = mark_body.index("running->address != transaction.target_address")
    assert "NAOBOT_OTA_PHASE_CONFIRMING" in mark_body
    confirming_commit = mark_body.index("naobot_nvs_begin_confirming")
    mark_valid = mark_body.index("esp_ota_mark_app_valid_cancel_rollback")
    clear_transaction = mark_body.index("naobot_nvs_clear_transaction")
    assert target_check < confirming_commit < mark_valid < clear_transaction
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


def test_native_long_image_validation_releases_the_micropython_gil() -> None:
    source = (NATIVE_ROOT / "modnao_ota.c").read_text(encoding="utf-8")
    finish_body = source[
        source.index("static mp_obj_t nao_ota_finish")
        : source.index("static MP_DEFINE_CONST_FUN_OBJ_0(nao_ota_finish_obj")
    ]
    activate_body = source[
        source.index("static mp_obj_t nao_ota_activate")
        : source.index("static MP_DEFINE_CONST_FUN_OBJ_0(nao_ota_activate_obj")
    ]

    for body, native_call in (
        (finish_body, "esp_ota_end(ota_handle)"),
        (activate_body, "esp_ota_set_boot_partition(ota_partition)"),
    ):
        release = body.index("MP_THREAD_GIL_EXIT()")
        call = body.index(native_call)
        acquire = body.index("MP_THREAD_GIL_ENTER()")
        assert release < call < acquire


def test_native_failed_terminal_abort_confirms_prior_write_or_digest_cleanup() -> None:
    source = (NATIVE_ROOT / "modnao_ota.c").read_text(encoding="utf-8")
    write_body = source[
        source.index("static mp_obj_t nao_ota_write")
        : source.index("static MP_DEFINE_CONST_FUN_OBJ_1(nao_ota_write_obj")
    ]
    finish_body = source[
        source.index("static mp_obj_t nao_ota_finish")
        : source.index("static MP_DEFINE_CONST_FUN_OBJ_0(nao_ota_finish_obj")
    ]
    abort_body = source[
        source.index("static mp_obj_t nao_ota_abort")
        : source.index("static MP_DEFINE_CONST_FUN_OBJ_0(nao_ota_abort_obj")
    ]

    assert 'naobot_ota_record_failure_after_abort(message);' in write_body
    assert 'naobot_ota_record_failure_after_abort("firmware digest mismatch");' in finish_body
    assert "ota_state == NAOBOT_OTA_FAILED" in abort_body
    assert "!ota_active && ota_partition == NULL" in abort_body
    assert "return mp_const_true;" in abort_body


def test_native_session_active_reports_the_real_handle_state() -> None:
    source = (NATIVE_ROOT / "modnao_ota.c").read_text(encoding="utf-8")
    session_body = source[
        source.index("static mp_obj_t nao_ota_session_active")
        : source.index("static MP_DEFINE_CONST_FUN_OBJ_0(nao_ota_session_active_obj")
    ]

    assert "mp_obj_new_bool(ota_active)" in session_body
    assert "ota_state" not in session_body


def test_native_abort_failure_keeps_active_session_for_retry() -> None:
    source = (NATIVE_ROOT / "modnao_ota.c").read_text(encoding="utf-8")
    helper_body = source[
        source.index("static esp_err_t naobot_ota_abort_active")
        : source.index("static void naobot_ota_record_failure_after_abort")
    ]
    abort_body = source[
        source.index("static mp_obj_t nao_ota_abort")
        : source.index("static MP_DEFINE_CONST_FUN_OBJ_0(nao_ota_abort_obj")
    ]

    native_abort = helper_body.index("esp_ota_abort(ota_handle)")
    failure_guard = helper_body.index("if (result != ESP_OK)")
    clear_active = helper_body.index("ota_active = false")
    clear_partition = helper_body.index("ota_partition = NULL")
    assert native_abort < failure_guard < clear_active < clear_partition
    assert "return result;" in helper_body[failure_guard:clear_active]
    assert "ota_state == NAOBOT_OTA_FAILED" in abort_body
    assert "esp_ota_abort" in abort_body
    assert "abort_result != ESP_OK" in abort_body


def test_native_worker_abort_releases_the_micropython_gil() -> None:
    source = (NATIVE_ROOT / "modnao_ota.c").read_text(encoding="utf-8")
    helper_body = source[
        source.index("static esp_err_t naobot_ota_abort_active")
        : source.index("static void naobot_ota_record_failure_after_abort")
    ]
    abort_body = source[
        source.index("static mp_obj_t nao_ota_abort")
        : source.index("static MP_DEFINE_CONST_FUN_OBJ_0(nao_ota_abort_obj")
    ]

    assert "naobot_ota_abort_active(true)" in abort_body
    release = helper_body.index("MP_THREAD_GIL_EXIT()")
    native_abort = helper_body.index("esp_ota_abort(ota_handle)")
    acquire = helper_body.index("MP_THREAD_GIL_ENTER()")
    assert release < native_abort < acquire


def test_native_abort_does_not_touch_activation_transaction_or_boot_partition() -> None:
    source = (NATIVE_ROOT / "modnao_ota.c").read_text(encoding="utf-8")
    abort_body = source[
        source.index("static mp_obj_t nao_ota_abort")
        : source.index("static MP_DEFINE_CONST_FUN_OBJ_0(nao_ota_abort_obj")
    ]

    assert "NAOBOT_OTA_WRITING" in abort_body
    assert "NAOBOT_OTA_STAGED" in abort_body
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


def test_main_merges_boot_health_error_without_overwriting_coordinator_error() -> None:
    import main as firmware_main

    coordinator_error = firmware_main.merge_ota_status(
        {"ota_state": "failed", "ota_error": "coordinator failed"},
        {"state": "error", "error": "boot health failed"},
    )
    boot_error = firmware_main.merge_ota_status(
        {"ota_state": "idle", "ota_error": None},
        {"state": "error", "error": "boot health failed"},
    )

    assert coordinator_error["ota_error"] == "coordinator failed"
    assert boot_error["ota_error"] == "boot health failed"


def test_main_integrates_boot_health_before_motion_tick() -> None:
    source = (FIRMWARE_ROOT / "main.py").read_text(encoding="utf-8")
    assert "from update.boot_health import BootHealthMonitor" in source
    assert "from update.ota_worker import create_default_worker as create_ota_worker" in source
    assert "boot_health = BootHealthMonitor(" in source
    assert "ota_worker = create_ota_worker()" in source
    assert "ota_worker=ota_worker" in source
    assert "ota_worker.start()" in source
    assert "ota_worker.stop()" in source
    assert "OTA_CURRENT_SEQUENCE" not in source
    assert "boot_health_status = boot_health.tick()" in source
    assert "merge_ota_status(ota.status(), boot_health_status)" in source
    assert source.index("boot_health.tick()") < source.index(
        "motion.tick()", source.index("while True:")
    )


def test_three_argument_begin_is_documented_as_intentional_security_contract() -> None:
    readme = (FIRMWARE_ROOT / "README.md").read_text(encoding="utf-8")

    assert "begin(image_size, expected_sha256_bytes, sequence)" in readme
    assert "不提供两参数兼容入口" in readme
