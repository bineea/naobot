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


def now_ms():
    if hasattr(time, "ticks_ms"):
        return time.ticks_ms()
    return int(time.time() * 1000)


def ticks_diff(end, start):
    if hasattr(time, "ticks_diff"):
        return time.ticks_diff(end, start)
    return end - start


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
    if not _is_integer(sequence) or sequence < 0 or sequence != requested_sequence:
        raise ValueError("invalid sequence")
    if not _is_integer(current_sequence) or current_sequence < 0 or sequence <= current_sequence:
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
        clock_ms=now_ms,
        chunk_size=MAX_CHUNK_SIZE,
        current_sequence=0,
    ):
        self.storage = storage
        self.power = power
        self.motion = motion
        self.servo_gate = servo_gate
        self.reflex = reflex
        self.touch = touch
        self.ota = ota_module
        self.clock_ms = clock_ms
        self.chunk_size = int(chunk_size)
        if not 1 <= self.chunk_size <= MAX_CHUNK_SIZE:
            raise ValueError("OTA chunk size must be between 1 and 4096 bytes")
        if not _is_integer(current_sequence) or current_sequence < 0:
            raise ValueError("current_sequence must be a non-negative integer")
        self.current_sequence = current_sequence
        self._state = "idle" if ota_module is not None else "unavailable"
        self._error = None if ota_module is not None else "nao_ota unavailable"
        self._sequence = None
        self._manifest = None
        self._manifest_bytes = None
        self._request = None
        self._offset = 0
        self._touch_started_ms = None

    def request_install(self, sequence):
        if self.ota is None or self._state in ("loading_manifest", "loading_signature", "waiting_for_gates", "installing"):
            return False
        if not _is_integer(sequence) or sequence < 0:
            return False
        self._state = "loading_manifest"
        self._error = None
        self._sequence = sequence
        self._manifest = None
        self._manifest_bytes = None
        self._request = None
        self._offset = 0
        self._touch_started_ms = self.clock_ms() if self._both_touched() else None
        return True

    def tick(self):
        self._update_touch_hold()
        if self._state == "loading_manifest":
            self._tick_file("manifest.json", MAX_MANIFEST_SIZE, self._manifest_loaded)
        elif self._state == "loading_signature":
            self._tick_file("signature.der", MAX_SIGNATURE_SIZE, self._signature_loaded)
        elif self._state == "waiting_for_gates":
            self._tick_waiting()
        elif self._state == "installing":
            self._tick_installing()
        return self.status()

    def status(self):
        progress = 0
        if self._manifest is not None and self._manifest["image_size"]:
            progress = min(100, (self._offset * 100) // self._manifest["image_size"])
        pending = False
        if self.ota is not None:
            try:
                pending = self.ota.pending_verify() is True
            except Exception:
                pending = False
        return {
            "ota_state": self._state,
            "ota_progress_pct": progress,
            "ota_error": self._error,
            "ota_pending_verify": pending,
            "ota_sequence": self._sequence,
        }

    def _tick_file(self, filename, max_size, callback):
        if self._request is None:
            self._request = self.storage.submit_update_read(
                self._sequence,
                filename,
                0,
                max_size + 1,
            )
            if not self._request.get("accepted"):
                self._deny(self._request.get("reason") or "storage request rejected")
            return
        result = self.storage.poll(self._request)
        if result.get("error"):
            self._deny(result["error"])
            return
        data = result.get("result")
        if data is None:
            return
        self._request = None
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
                current_sequence=self.current_sequence,
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
            )
        except Exception as exc:
            self._fail(str(exc))
            return
        self._state = "installing"
        self._error = None
        self._request = None
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
            self._request = self.storage.submit_update_read(
                self._sequence,
                IMAGE_NAME,
                self._offset,
                read_size,
            )
            if not self._request.get("accepted"):
                self._abort(self._request.get("reason") or "storage request rejected")
            return
        result = self.storage.poll(self._request)
        if result.get("error"):
            self._abort(result["error"])
            return
        chunk = result.get("result")
        if chunk is None:
            return
        self._request = None
        if not isinstance(chunk, bytes) or not chunk or len(chunk) > self.chunk_size:
            self._abort("invalid firmware chunk")
            return
        if self._offset + len(chunk) > image_size:
            self._abort("firmware exceeds manifest size")
            return
        try:
            written = self.ota.write(chunk)
        except Exception as exc:
            self._abort(str(exc))
            return
        if written != len(chunk):
            self._abort("short OTA write")
            return
        self._offset += len(chunk)

    def _tick_trailing_byte_probe(self):
        if self._request is None:
            self._request = self.storage.submit_update_read(
                self._sequence,
                IMAGE_NAME,
                self._offset,
                1,
            )
            if not self._request.get("accepted"):
                self._abort(self._request.get("reason") or "storage request rejected")
            return
        result = self.storage.poll(self._request)
        if result.get("error"):
            self._abort(result["error"])
            return
        trailing = result.get("result")
        if trailing is None:
            return
        self._request = None
        if trailing:
            self._abort("firmware exceeds manifest size")
            return
        try:
            if self.ota.finish() is not True:
                raise OSError("OTA finish failed")
        except Exception as exc:
            self._fail(str(exc), abort=True)
            return
        self._state = "ready_to_reboot"
        self._error = None

    def _gate_error(self, require_touch_hold):
        storage = self.storage.snapshot()
        if storage.get("available") is not True or storage.get("mounted") is not True:
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
            or self.motion.motion_state != "idle"
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
        self._state = "denied"
        self._error = error
        self._request = None

    def _abort(self, error):
        try:
            self.servo_gate.set_disabled(True)
        except Exception:
            pass
        try:
            self.ota.abort()
        except Exception:
            pass
        self._state = "aborted"
        self._error = error or "OTA aborted"
        self._request = None

    def _fail(self, error, abort=False):
        if abort:
            try:
                self.ota.abort()
            except Exception:
                pass
        self._state = "failed"
        self._error = error or "OTA failed"
        self._request = None
