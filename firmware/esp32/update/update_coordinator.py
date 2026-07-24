try:
    import ujson as json
except ImportError:
    import json

try:
    import utime as time
except ImportError:
    import time

try:
    import nao_ota as default_ota_module
except ImportError:
    default_ota_module = None


BOARD_ID = "XIAO_ESP32S3_SENSE"
IMAGE_NAME = "firmware.bin"
MAX_IMAGE_SIZE = 0x280000
MAX_CHUNK_SIZE = 4096
MAX_MANIFEST_SIZE = 1024
MAX_SIGNATURE_SIZE = 128
MAX_TEXT_LENGTH = 64
RUNTIME_API = 1
TOUCH_HOLD_MS = 3000
RELEASE_HOLD_MS = 500
READ_TIMEOUT_MS = 5000
NATIVE_OPERATION_TIMEOUT_MS = 5000
UINT32_MAX = 0xFFFFFFFF
MANIFEST_FIELDS = {
    "schema",
    "board_id",
    "key_id",
    "sequence",
    "version",
    "image_name",
    "image_size",
    "sha256",
    "min_runtime_api",
}
MOTION_INHIBIT_STATES = (
    "loading_manifest",
    "loading_signature",
    "waiting_for_gates",
    "installing",
    "finalizing",
    "finalize_timeout",
    "awaiting_activation_release",
    "waiting_for_activation",
    "activating",
    "activation_timeout",
    "activated",
    "activated_reboot_failed",
)


def now_ms():
    if hasattr(time, "ticks_ms"):
        return time.ticks_ms()
    return int(time.time() * 1000)


def ticks_diff(end, start):
    if hasattr(time, "ticks_diff"):
        return time.ticks_diff(end, start)
    return end - start


def default_reboot():
    import machine

    machine.reset()


def _is_integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _json_string(value):
    escaped = ['"']
    short_escapes = {
        '"': '\\"',
        "\\": "\\\\",
        "\b": "\\b",
        "\f": "\\f",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
    }
    for char in value:
        replacement = short_escapes.get(char)
        if replacement is not None:
            escaped.append(replacement)
            continue
        codepoint = ord(char)
        if 0x20 <= codepoint <= 0x7E:
            escaped.append(char)
        elif codepoint <= 0xFFFF:
            escaped.append(f"\\u{codepoint:04x}")
        else:
            codepoint -= 0x10000
            escaped.append(f"\\u{0xD800 + (codepoint >> 10):04x}")
            escaped.append(f"\\u{0xDC00 + (codepoint & 0x3FF):04x}")
    escaped.append('"')
    return "".join(escaped)


def _canonical_manifest_bytes(manifest):
    ordered = (
        ("board_id", _json_string(manifest["board_id"])),
        ("image_name", _json_string(manifest["image_name"])),
        ("image_size", str(manifest["image_size"])),
        ("key_id", _json_string(manifest["key_id"])),
        ("min_runtime_api", str(manifest["min_runtime_api"])),
        ("schema", str(manifest["schema"])),
        ("sequence", str(manifest["sequence"])),
        ("sha256", _json_string(manifest["sha256"])),
        ("version", _json_string(manifest["version"])),
    )
    return ("{" + ",".join(_json_string(key) + ":" + value for key, value in ordered) + "}").encode(
        "ascii"
    )


def validate_manifest(manifest_bytes, requested_sequence, current_sequence):
    if not isinstance(manifest_bytes, bytes) or not 0 < len(manifest_bytes) <= MAX_MANIFEST_SIZE:
        raise ValueError("invalid manifest size")
    try:
        manifest = json.loads(manifest_bytes.decode("ascii"))
    except Exception as exc:
        raise ValueError("malformed manifest") from exc
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_FIELDS:
        raise ValueError("manifest fields do not match schema")
    if manifest.get("schema") != 1 or not _is_integer(manifest.get("schema")):
        raise ValueError("unsupported manifest schema")
    if manifest.get("board_id") != BOARD_ID:
        raise ValueError("wrong board_id")
    if manifest.get("image_name") != IMAGE_NAME:
        raise ValueError("wrong image_name")
    for name in ("version", "key_id"):
        value = manifest.get(name)
        if not isinstance(value, str) or not value or len(value) > MAX_TEXT_LENGTH:
            raise ValueError("invalid " + name)
    sequence = manifest.get("sequence")
    if (
        not _is_integer(sequence)
        or not 0 <= sequence <= UINT32_MAX
        or sequence != requested_sequence
    ):
        raise ValueError("invalid sequence")
    if (
        not _is_integer(current_sequence)
        or not 0 <= current_sequence <= UINT32_MAX
        or sequence <= current_sequence
    ):
        raise ValueError("stale update sequence")
    image_size = manifest.get("image_size")
    if not _is_integer(image_size) or not 0 < image_size <= MAX_IMAGE_SIZE:
        raise ValueError("invalid image_size")
    digest = manifest.get("sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise ValueError("invalid sha256")
    min_runtime_api = manifest.get("min_runtime_api")
    if (
        not _is_integer(min_runtime_api)
        or min_runtime_api < 0
        or min_runtime_api > RUNTIME_API
    ):
        raise ValueError("unsupported min_runtime_api")
    if _canonical_manifest_bytes(manifest) != manifest_bytes:
        raise ValueError("manifest is not canonical")
    return manifest


class UpdateCoordinator:
    def __init__(
        self,
        storage,
        power,
        motion,
        servo_gate,
        reflex,
        touch,
        ota_module=default_ota_module,
        ota_worker=None,
        clock_ms=now_ms,
        chunk_size=MAX_CHUNK_SIZE,
        reboot=default_reboot,
    ):
        self.storage = storage
        self.power = power
        self.motion = motion
        self.servo_gate = servo_gate
        self.reflex = reflex
        self.touch = touch
        self.ota = ota_module
        self.ota_worker = ota_worker
        self.clock_ms = clock_ms
        self.reboot = reboot
        self.chunk_size = int(chunk_size)
        if not 1 <= self.chunk_size <= MAX_CHUNK_SIZE:
            raise ValueError("OTA chunk size must be between 1 and 4096 bytes")
        available = ota_module is not None and ota_worker is not None
        self._state = "idle" if available else "unavailable"
        self._error = None if available else "nao_ota or OTA worker unavailable"
        self._sequence = None
        self._current_sequence = None
        self._manifest = None
        self._manifest_bytes = None
        self._request = None
        self._request_started_ms = None
        self._offset = 0
        self._touch_started_ms = None
        self._release_started_ms = None
        self._activation_touch_started_ms = None
        self._storage_start_wait_started_ms = None
        self._native_request = None
        self._native_started_ms = None
        self._native_gate_error = None
        self._native_timed_out = False
        self._motion_lock_held = False
        self._cleanup_pending = False
        self._cleanup_request = None
        self._cleanup_abort_required = False
        self._cleanup_target_state = None
        self._cleanup_base_error = None

    def request_install(self, sequence):
        if (
            self.ota is None
            or self._state in MOTION_INHIBIT_STATES
            or self._cleanup_pending
        ):
            return False
        native_active, native_error = self._native_session_state()
        if native_active is not False:
            self._start_cleanup(
                native_error or "native OTA session already active",
                target_state="failed",
                abort_required=True,
            )
            self._tick_cleanup()
            return False
        if self.ota_worker is None:
            return False
        worker = self.ota_worker.snapshot()
        if worker.get("runtime_state") not in ("starting", "running") or worker.get("busy"):
            return False
        if not _is_integer(sequence) or not 0 <= sequence <= UINT32_MAX:
            return False
        try:
            current_sequence = self.ota.current_sequence()
            pending_sequence = self.ota.pending_sequence()
            pending_verify = self.ota.pending_verify()
        except Exception:
            return False
        if (
            not _is_integer(current_sequence)
            or not 0 <= current_sequence <= UINT32_MAX
            or sequence <= current_sequence
            or pending_sequence is not None
            or pending_verify is not False
        ):
            return False
        if not self._acquire_motion_lock():
            return False
        self._state = "loading_manifest"
        self._error = None
        self._sequence = sequence
        self._current_sequence = current_sequence
        self._manifest = None
        self._manifest_bytes = None
        self._clear_request()
        self._offset = 0
        self._touch_started_ms = self.clock_ms() if self._both_touched() else None
        self._release_started_ms = None
        self._activation_touch_started_ms = None
        self._storage_start_wait_started_ms = None
        self._clear_native_request()
        return True

    def tick(self):
        try:
            if self._cleanup_pending:
                self._tick_cleanup()
                return self.status()
            if self._state not in MOTION_INHIBIT_STATES and self.ota is not None:
                native_active, native_error = self._native_session_state()
                if native_active is not False:
                    self._start_cleanup(
                        native_error or "orphan native OTA session active",
                        target_state="failed",
                        abort_required=True,
                    )
                    self._tick_cleanup()
                    return self.status()
            if self._state in MOTION_INHIBIT_STATES:
                if not self._acquire_motion_lock():
                    raise RuntimeError("OTA motion inhibit unavailable")
            if self._state in (
                "loading_manifest",
                "loading_signature",
                "waiting_for_gates",
                "installing",
            ):
                self._update_touch_hold()
            if self._state == "loading_manifest":
                self._tick_file("manifest.json", MAX_MANIFEST_SIZE, self._manifest_loaded)
            elif self._state == "loading_signature":
                self._tick_file("signature.der", MAX_SIGNATURE_SIZE, self._signature_loaded)
            elif self._state == "waiting_for_gates":
                self._tick_waiting()
            elif self._state == "installing":
                self._tick_installing()
            elif self._state in ("finalizing", "finalize_timeout"):
                self._tick_native_operation("finish")
            elif self._state == "awaiting_activation_release":
                self._tick_activation_release()
            elif self._state == "waiting_for_activation":
                self._tick_activation_wait()
            elif self._state in ("activating", "activation_timeout"):
                self._tick_native_operation("activate")
        except Exception as exc:
            self._fail_closed_exception(exc)
        return self.status()

    def status(self):
        progress = 0
        if self._manifest is not None and self._manifest["image_size"]:
            progress = min(100, (self._offset * 100) // self._manifest["image_size"])
        pending = False
        pending_error = None
        if (
            self.ota is not None
            and self._native_request is None
            and not self._cleanup_pending
        ):
            try:
                native_pending = self.ota.pending_verify()
                pending = native_pending if native_pending in (True, False) else "unknown"
                if pending == "unknown":
                    pending_error = "pending verify state unavailable"
            except Exception as exc:
                pending = "unknown"
                pending_error = str(exc) or "pending verify state unavailable"
        return {
            "ota_state": self._state,
            "ota_progress_pct": progress,
            "ota_error": self._error or pending_error,
            "ota_pending_verify": pending,
            "ota_sequence": self._sequence,
        }

    def _tick_file(self, filename, max_size, callback):
        if self._request is None:
            request = self._submit_read(filename, 0, max_size + 1)
            if request is None:
                return
            self._request = request
            return
        result = self.storage.poll(self._request)
        if result.get("error"):
            self._deny(result["error"])
            return
        data = result.get("result")
        if data is None:
            if self._request_timed_out():
                self._deny(filename + " read timeout")
            return
        self._clear_request()
        if not isinstance(data, bytes) or not 0 < len(data) <= max_size:
            self._deny("invalid " + filename + " size")
            return
        callback(data)

    def _manifest_loaded(self, manifest_bytes):
        self._manifest_bytes = manifest_bytes
        self._state = "loading_signature"

    def _signature_loaded(self, signature_der):
        try:
            signature_valid = self.ota.verify_manifest(self._manifest_bytes, signature_der)
        except Exception:
            signature_valid = False
        if signature_valid is not True:
            self._deny("invalid manifest signature")
            return
        try:
            self._manifest = validate_manifest(
                self._manifest_bytes,
                requested_sequence=self._sequence,
                current_sequence=self._current_sequence,
            )
        except ValueError as exc:
            self._deny(str(exc))
            return
        self._state = "waiting_for_gates"
        self._error = None

    def _tick_waiting(self):
        error = self._gate_error(require_touch_hold=True)
        if error is not None:
            self._error = error
            return
        try:
            self.motion.cancel("ota")
            self.ota.begin(
                self._manifest["image_size"],
                bytes.fromhex(self._manifest["sha256"]),
                self._sequence,
            )
        except Exception as exc:
            self._fail(str(exc), abort=True)
            return
        native_active, native_error = self._native_session_state()
        if native_active is not True:
            self._fail(
                native_error or "native OTA session was not confirmed active",
                abort=True,
            )
            return
        self._state = "installing"
        self._error = None
        self._clear_request()
        self._offset = 0

    def _tick_installing(self):
        error = self._gate_error(require_touch_hold=True)
        if error is not None:
            self._abort(error)
            return
        image_size = self._manifest["image_size"]
        if self._offset == image_size:
            self._tick_trailing_byte_probe()
            return
        if self._request is None:
            read_size = min(self.chunk_size, image_size - self._offset)
            request = self._submit_read(IMAGE_NAME, self._offset, read_size, abort=True)
            if request is None:
                return
            self._request = request
            return
        result = self.storage.poll(self._request)
        if result.get("error"):
            self._abort(result["error"])
            return
        chunk = result.get("result")
        if chunk is None:
            if self._request_timed_out():
                self._abort("firmware read timeout")
            return
        self._clear_request()
        if not isinstance(chunk, bytes) or not chunk or len(chunk) > self.chunk_size:
            self._abort("invalid firmware chunk")
            return
        if self._offset + len(chunk) > image_size:
            self._abort("firmware exceeds manifest size")
            return
        try:
            written = self.ota.write(chunk)
        except Exception as exc:
            self._fail(str(exc), abort=True)
            return
        if written != len(chunk):
            self._abort("short OTA write")
            return
        self._offset += len(chunk)

    def _tick_trailing_byte_probe(self):
        if self._request is None:
            request = self._submit_read(IMAGE_NAME, self._offset, 1, abort=True)
            if request is None:
                return
            self._request = request
            return
        result = self.storage.poll(self._request)
        if result.get("error"):
            self._abort(result["error"])
            return
        trailing = result.get("result")
        if trailing is None:
            if self._request_timed_out():
                self._abort("firmware trailing-byte read timeout")
            return
        self._clear_request()
        if trailing:
            self._abort("firmware exceeds manifest size")
            return
        self._submit_native_operation("finish")

    def _tick_activation_release(self):
        error = self._gate_error(require_touch_hold=False)
        if error is not None:
            self._abort(error)
            return
        if not self._fully_released():
            self._release_started_ms = None
            return
        current_ms = self.clock_ms()
        if self._release_started_ms is None:
            self._release_started_ms = current_ms
            return
        if ticks_diff(current_ms, self._release_started_ms) < RELEASE_HOLD_MS:
            return
        self._state = "waiting_for_activation"
        self._activation_touch_started_ms = None
        self._error = None

    def _tick_activation_wait(self):
        error = self._gate_error(require_touch_hold=False)
        if error is not None:
            self._abort(error)
            return
        current_ms = self.clock_ms()
        if not self._both_touched():
            self._activation_touch_started_ms = None
            self._error = "dual touch activation incomplete"
            return
        if self._activation_touch_started_ms is None:
            self._activation_touch_started_ms = current_ms
            self._error = "dual touch activation incomplete"
            return
        if ticks_diff(current_ms, self._activation_touch_started_ms) < TOUCH_HOLD_MS:
            self._error = "dual touch activation incomplete"
            return
        try:
            self.motion.cancel("ota_activate")
        except Exception as exc:
            self._fail(str(exc), abort=True)
            return
        self._submit_native_operation("activate")

    def _submit_native_operation(self, operation):
        request = self.ota_worker.submit(operation)
        if not request.get("accepted"):
            self._abort(request.get("reason") or "OTA worker request rejected")
            return
        self._native_request = request
        self._native_started_ms = self.clock_ms()
        self._native_gate_error = None
        self._native_timed_out = False
        self._state = "finalizing" if operation == "finish" else "activating"
        self._error = None

    def _tick_native_operation(self, operation):
        gate_error = self._gate_error(require_touch_hold=False)
        if gate_error is not None and self._native_gate_error is None:
            self._native_gate_error = gate_error

        result = self.ota_worker.poll(self._native_request)
        if result.get("done") is not True:
            if (
                not self._native_timed_out
                and ticks_diff(self.clock_ms(), self._native_started_ms)
                >= NATIVE_OPERATION_TIMEOUT_MS
            ):
                self._native_timed_out = True
                self._state = (
                    "finalize_timeout"
                    if operation == "finish"
                    else "activation_timeout"
                )
                self._error = "OTA " + operation + " timeout"
            return

        worker_error = result.get("error")
        native_result = result.get("result")
        gate_error = self._native_gate_error
        timed_out = self._native_timed_out
        self._clear_native_request()

        if worker_error:
            self._fail(worker_error)
            return
        if native_result is not True:
            self._fail("OTA " + operation + " failed")
            return
        if operation == "finish":
            if timed_out or gate_error is not None:
                reason = "OTA finish timeout" if timed_out else gate_error
                self._abort(reason)
                return
            self._state = "awaiting_activation_release"
            self._error = None
            self._release_started_ms = None
            self._activation_touch_started_ms = None
            return

        self._state = "activated"
        self._error = None
        if timed_out or gate_error is not None:
            detail = "activation timeout" if timed_out else gate_error
            self._state = "activated_reboot_failed"
            self._error = "activated but reboot suppressed: " + detail
            return
        try:
            self.reboot()
        except Exception as exc:
            self._state = "activated_reboot_failed"
            self._error = "activated but reboot failed: " + (
                str(exc) or "reboot callback failed"
            )

    def _gate_error(self, require_touch_hold):
        storage = self.storage.snapshot()
        if (
            storage.get("runtime_state") != "running"
            or storage.get("available") is not True
            or storage.get("mounted") is not True
        ):
            return "SD unavailable"
        power = self.power.snapshot()
        if power.get("available") is not True or power.get("fault") is not False:
            return "power unhealthy"
        if power.get("soc_precise") is not True or power.get("source") not in (
            "bq34z100",
            "bq34z100+ina226",
        ):
            return "precise SOC unavailable"
        battery_pct = power.get("battery_pct")
        if not _is_integer(battery_pct):
            return "precise SOC unavailable"
        if battery_pct < 50:
            return "SOC below 50"
        if power.get("charging") is not True:
            return "charging not confirmed"
        if (
            self.motion.current is not None
            or self.motion.queue
            or self.motion.motion_state not in ("idle", "inhibited")
            or getattr(self.motion, "motion_inhibited", False) is not True
        ):
            return "motion not idle"
        if getattr(self.servo_gate, "available", False) is not True:
            return "OE unavailable"
        if self.servo_gate.set_disabled(True) is not True:
            return "OE disable unconfirmed"
        if not hasattr(self.servo_gate, "confirm_disabled"):
            return "OE disable unconfirmed"
        if self.servo_gate.confirm_disabled() is not True:
            return "OE disable unconfirmed"
        if (
            getattr(self.reflex, "state", "fault") not in ("none", "recovered")
            or getattr(self.reflex, "authority", "emergency") != "idle"
            or getattr(self.reflex, "emergency_stop", True) is not False
            or getattr(self.reflex, "shutdown_failed_latched", True) is not False
        ):
            return "reflex active"
        if getattr(self.touch, "available", False) is not True:
            return "MPR121 unavailable"
        if require_touch_hold and not self._touch_hold_complete():
            return "dual touch hold incomplete"
        return None

    def _both_touched(self):
        return (
            getattr(self.touch, "available", False) is True
            and getattr(self.touch, "both_touched", False) is True
            and getattr(self.touch, "touch_mask", None) == 0x03
        )

    def _fully_released(self):
        return (
            getattr(self.touch, "available", False) is True
            and getattr(self.touch, "both_touched", True) is False
            and getattr(self.touch, "touch_mask", None) == 0
        )

    def _update_touch_hold(self):
        if not self._both_touched():
            self._touch_started_ms = None
        elif self._touch_started_ms is None:
            self._touch_started_ms = self.clock_ms()

    def _touch_hold_complete(self):
        return self._touch_started_ms is not None and ticks_diff(
            self.clock_ms(), self._touch_started_ms
        ) >= TOUCH_HOLD_MS

    def _deny(self, error):
        self._start_cleanup(error, target_state="denied", abort_required=False)
        self._tick_cleanup()

    def _abort(self, error):
        self._start_cleanup(
            error or "OTA aborted",
            target_state="aborted",
            abort_required=True,
        )
        self._tick_cleanup()

    def _fail(self, error, abort=False):
        if self._state in ("activated", "activated_reboot_failed"):
            self._state = "activated_reboot_failed"
            self._error = error or "OTA failed"
            self._clear_request()
            return
        abort_required = abort or self._state in (
            "installing",
            "finalizing",
            "finalize_timeout",
            "awaiting_activation_release",
            "waiting_for_activation",
            "activating",
            "activation_timeout",
        )
        self._start_cleanup(
            error or "OTA failed",
            target_state="failed",
            abort_required=abort_required,
        )
        self._tick_cleanup()

    def _fail_closed_exception(self, exc):
        if self._native_request is not None:
            self._native_gate_error = str(exc) or "OTA coordinator failed"
            self._error = self._native_gate_error
            return
        if self._state in ("activated", "activated_reboot_failed"):
            self._state = "activated_reboot_failed"
            self._error = "activated but coordinator failed: " + (
                str(exc) or "OTA coordinator failed"
            )
            self._clear_request()
            return
        self._start_cleanup(
            str(exc) or "OTA coordinator failed",
            target_state="failed",
            abort_required=self._state in MOTION_INHIBIT_STATES,
        )

    def _start_cleanup(self, error, *, target_state, abort_required):
        if self._cleanup_pending:
            self._cleanup_target_state = "failed"
            self._cleanup_base_error = self._join_errors(
                self._cleanup_base_error,
                error,
            )
            self._cleanup_abort_required = (
                self._cleanup_abort_required or bool(abort_required)
            )
            return
        self._cleanup_pending = True
        self._cleanup_request = None
        self._cleanup_abort_required = bool(abort_required)
        self._cleanup_target_state = target_state
        self._cleanup_base_error = error
        self._state = "failed"
        self._error = error
        self._clear_request()

    def _tick_cleanup(self):
        if not self._cleanup_pending:
            return
        errors = []
        try:
            if not self._acquire_motion_lock():
                errors.append("OTA motion inhibit unavailable")
        except Exception as exc:
            errors.append(str(exc) or "OTA motion inhibit unavailable")
        try:
            self.motion.cancel("ota")
        except Exception as exc:
            errors.append(str(exc) or "motion cleanup failed")
        try:
            if not self._disable_servo_output():
                errors.append("OE cleanup unconfirmed")
        except Exception as exc:
            errors.append(str(exc) or "OE cleanup failed")

        if self._cleanup_request is not None:
            try:
                result = self.ota_worker.poll(self._cleanup_request)
            except Exception as exc:
                errors.append(str(exc) or "native abort poll failed")
                self._publish_cleanup_errors(errors)
                return
            if not isinstance(result, dict):
                errors.append("native abort poll invalid")
                self._publish_cleanup_errors(errors)
                return
            if not result.get("done"):
                self._publish_cleanup_errors(errors)
                return
            self._cleanup_request = None
            if result.get("error"):
                self._cleanup_abort_required = True
                errors.append(result["error"])
                self._publish_cleanup_errors(errors)
                return
            if result.get("result") is not True:
                self._cleanup_abort_required = True
                errors.append("native abort unconfirmed")
                self._publish_cleanup_errors(errors)
                return
            self._cleanup_abort_required = False

        native_active, native_error = self._native_session_state()
        if native_active is not False:
            self._cleanup_abort_required = True
            if native_error:
                errors.append(native_error)

        if self._cleanup_abort_required:
            if self.ota_worker is None:
                errors.append("OTA worker unavailable")
                self._publish_cleanup_errors(errors)
                return
            try:
                request = self.ota_worker.submit("abort")
            except Exception as exc:
                errors.append(str(exc) or "native abort submit failed")
                self._publish_cleanup_errors(errors)
                return
            if not isinstance(request, dict):
                errors.append("native abort submit invalid")
                self._publish_cleanup_errors(errors)
                return
            if not request.get("accepted"):
                errors.append(request.get("reason") or "native abort submit failed")
            else:
                self._cleanup_request = request
            self._publish_cleanup_errors(errors)
            return

        if errors:
            self._publish_cleanup_errors(errors)
            return
        try:
            released = self._release_motion_lock()
        except Exception as exc:
            self._publish_cleanup_errors(
                [str(exc) or "motion inhibit release failed"]
            )
            return
        if not released:
            self._publish_cleanup_errors(["motion inhibit release failed"])
            return

        self._state = self._cleanup_target_state
        self._error = self._cleanup_base_error
        self._cleanup_pending = False
        self._cleanup_request = None
        self._cleanup_abort_required = False
        self._cleanup_target_state = None
        self._cleanup_base_error = None

    def _native_session_state(self):
        if self.ota is None or not hasattr(self.ota, "session_active"):
            return None, "native session state unavailable"
        try:
            active = self.ota.session_active()
        except Exception as exc:
            return None, str(exc) or "native session state unavailable"
        if active is True or active is False:
            return active, None
        return None, "native session state unavailable"

    def _publish_cleanup_errors(self, errors):
        self._state = "failed"
        self._error = self._join_errors(self._cleanup_base_error, *errors)

    @staticmethod
    def _join_errors(*errors):
        unique = []
        for error in errors:
            if error and error not in unique:
                unique.append(error)
        return "; ".join(unique)

    def _disable_servo_output(self):
        if self.servo_gate.set_disabled(True) is not True:
            return False
        if not hasattr(self.servo_gate, "confirm_disabled"):
            return False
        return self.servo_gate.confirm_disabled() is True

    def _acquire_motion_lock(self):
        if self._motion_lock_held:
            if hasattr(self.motion, "has_motion_inhibit"):
                return self.motion.has_motion_inhibit("ota") is True
            return (
                getattr(self.motion, "motion_inhibited", False) is True
                and getattr(self.motion, "motion_inhibit_reason", None) == "ota"
            )
        if not hasattr(self.motion, "set_motion_inhibited"):
            return False
        if self.motion.set_motion_inhibited(True, "ota") is not True:
            return False
        self._motion_lock_held = True
        return True

    def _release_motion_lock(self):
        if not self._motion_lock_held:
            return True
        if self.motion.set_motion_inhibited(False, "ota") is not True:
            return False
        self._motion_lock_held = False
        return True

    def _submit_read(self, filename, offset, max_bytes, abort=False):
        request = self.storage.submit_update_read(
            self._sequence,
            filename,
            offset,
            max_bytes,
        )
        if not request.get("accepted"):
            error = request.get("reason") or "storage request rejected"
            if (
                error == "storage worker not running"
                and self.storage.snapshot().get("runtime_state") == "starting"
            ):
                current_ms = self.clock_ms()
                if self._storage_start_wait_started_ms is None:
                    self._storage_start_wait_started_ms = current_ms
                elif ticks_diff(
                    current_ms,
                    self._storage_start_wait_started_ms,
                ) >= READ_TIMEOUT_MS:
                    if abort:
                        self._abort("storage worker startup timeout")
                    else:
                        self._deny("storage worker startup timeout")
                    return None
                self._error = error
                return None
            if abort:
                self._abort(error)
            else:
                self._deny(error)
            return None
        self._storage_start_wait_started_ms = None
        self._request_started_ms = self.clock_ms()
        return request

    def _request_timed_out(self):
        return (
            self._request_started_ms is not None
            and ticks_diff(self.clock_ms(), self._request_started_ms) >= READ_TIMEOUT_MS
        )

    def _clear_request(self):
        self._request = None
        self._request_started_ms = None

    def _clear_native_request(self):
        self._native_request = None
        self._native_started_ms = None
        self._native_gate_error = None
        self._native_timed_out = False
