try:
    import ujson as json
except ImportError:
    import json

try:
    import uos as os
except ImportError:
    import os

try:
    import machine
except ImportError:
    machine = None


DEFAULT_LOG_LIMIT_BYTES = 128 * 1024
DEFAULT_ARCHIVE_LIMIT = 7
DEFAULT_READ_CHUNK_BYTES = 4096


class SDStorage:
    """microSD 的唯一挂载和文件访问边界。"""

    def __init__(
        self,
        pins=None,
        mount_point="/sd",
        log_limit_bytes=DEFAULT_LOG_LIMIT_BYTES,
        archive_limit=DEFAULT_ARCHIVE_LIMIT,
        read_chunk_bytes=DEFAULT_READ_CHUNK_BYTES,
        sdcard_factory=None,
        mount_fn=None,
        umount_fn=None,
        os_module=os,
        open_fn=open,
    ):
        if pins is None:
            from config import SD_PINS

            pins = SD_PINS
        self.pins = dict(pins)
        self.mount_point = mount_point.rstrip("/") or "/sd"
        self.log_limit_bytes = max(1, int(log_limit_bytes))
        self.archive_limit = max(0, int(archive_limit))
        self.read_chunk_bytes = max(1, int(read_chunk_bytes))
        self.os = os_module
        self.open_fn = open_fn
        self.sdcard_factory = sdcard_factory or getattr(machine, "SDCard", None)
        self.mount_fn = mount_fn or getattr(os_module, "mount", None)
        self.umount_fn = umount_fn or getattr(os_module, "umount", None)
        self._card = None
        self._mounted = False
        self._available = False
        self._last_error = None

    def ensure_mounted(self):
        if self._mounted:
            return True
        if self.sdcard_factory is None or self.mount_fn is None:
            self._set_error("SDCard support unavailable")
            return False
        try:
            self._card = self.sdcard_factory(slot=2, **self.pins)
            self.mount_fn(self._card, self.mount_point)
        except Exception as exc:
            self._card = None
            self._available = False
            self._set_error(exc)
            return False
        self._mounted = True
        self._available = True
        self._last_error = None
        return True

    def unmount(self):
        if not self._mounted:
            return False
        try:
            if self.umount_fn is not None:
                self.umount_fn(self.mount_point)
        except Exception as exc:
            self._set_error(exc)
            return False
        self._card = None
        self._mounted = False
        return True

    def append_log(self, record):
        if not self.is_structured_record(record):
            self._set_error("diagnostic record must be structured")
            return False
        if self.contains_binary(record):
            self._set_error("binary media records are not supported")
            return False
        try:
            entry = self._encode_record(record)
        except Exception as exc:
            self._set_error(exc)
            return False
        if not self.ensure_mounted():
            return False
        try:
            log_path = self._log_path()
            if self._file_size(log_path) and self._file_size(log_path) + len(entry) > self.log_limit_bytes:
                self._rotate_logs()
            with self.open_fn(log_path, "a") as log_file:
                log_file.write(entry)
        except Exception as exc:
            self._set_error(exc)
            return False
        self._last_error = None
        return True

    def read_update(self, sequence, filename, offset=0):
        try:
            path = self._update_path(sequence, filename)
            if not isinstance(offset, int) or offset < 0:
                raise ValueError("invalid update offset")
        except ValueError:
            self._set_error("invalid update path")
            return None
        if not self.ensure_mounted():
            return None
        try:
            with self.open_fn(path, "rb") as update_file:
                if offset:
                    update_file.seek(offset)
                data = update_file.read(self.read_chunk_bytes)
        except Exception as exc:
            self._set_error(exc)
            return None
        self._last_error = None
        return data

    def snapshot(self):
        return {
            "available": self._available,
            "mounted": self._mounted,
            "log_bytes": self._file_size(self._log_path()) if self._mounted else 0,
            "last_error": self._last_error,
            "pins": dict(self.pins),
        }

    @staticmethod
    def is_structured_record(record):
        return isinstance(record, dict)

    @staticmethod
    def contains_binary(value):
        if isinstance(value, (bytes, bytearray, memoryview)):
            return True
        if isinstance(value, dict):
            return any(
                SDStorage.contains_binary(key) or SDStorage.contains_binary(item)
                for key, item in value.items()
            )
        if isinstance(value, (list, tuple)):
            return any(SDStorage.contains_binary(item) for item in value)
        return False

    def _encode_record(self, record):
        try:
            return json.dumps(record, separators=(",", ":")) + "\n"
        except TypeError:
            return json.dumps(record) + "\n"

    def _rotate_logs(self):
        log_path = self._log_path()
        for index in range(self.archive_limit, 0, -1):
            target = f"{log_path}.{index}"
            source = log_path if index == 1 else f"{log_path}.{index - 1}"
            if self._exists(target):
                self.os.remove(target)
            if self._exists(source):
                self.os.rename(source, target)
        if self.archive_limit == 0 and self._exists(log_path):
            self.os.remove(log_path)

    def _update_path(self, sequence, filename):
        sequence = self._safe_segment(sequence)
        filename = self._safe_segment(filename)
        return f"{self.mount_point}/updates/{sequence}/{filename}"

    @staticmethod
    def _safe_segment(value):
        value = str(value)
        if not value or value == "." or ".." in value or "/" in value or "\\" in value:
            raise ValueError("invalid update path")
        return value

    def _log_path(self):
        return f"{self.mount_point}/naobot.log"

    def _file_size(self, path):
        try:
            stat = self.os.stat(path)
        except Exception:
            return 0
        return stat[6] if isinstance(stat, tuple) else stat.st_size

    def _exists(self, path):
        try:
            self.os.stat(path)
            return True
        except Exception:
            return False

    def _set_error(self, error):
        self._last_error = str(error)
