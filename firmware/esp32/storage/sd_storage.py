try:
    import ujson as json
except ImportError:
    import json

try:
    import uos as os
except ImportError:
    import os

try:
    import utime as time
except ImportError:
    import time

try:
    import machine
except ImportError:
    machine = None


DEFAULT_LOG_LIMIT_BYTES = 128 * 1024
DEFAULT_ARCHIVE_LIMIT = 7
DEFAULT_READ_CHUNK_BYTES = 4096
DEFAULT_RETRY_BASE_MS = 1000
DEFAULT_RETRY_MAX_MS = 30000
DEFAULT_RECORD_MAX_DEPTH = 16


def now_ms():
    if hasattr(time, "ticks_ms"):
        return time.ticks_ms()
    return int(time.time() * 1000)


def ticks_diff(end, start):
    if hasattr(time, "ticks_diff"):
        return time.ticks_diff(end, start)
    return end - start


class SDStorage:
    """microSD 的唯一挂载和文件访问边界，仅由存储线程调用 I/O 方法。"""

    def __init__(
        self,
        pins=None,
        mount_point="/sd",
        log_limit_bytes=DEFAULT_LOG_LIMIT_BYTES,
        archive_limit=DEFAULT_ARCHIVE_LIMIT,
        read_chunk_bytes=DEFAULT_READ_CHUNK_BYTES,
        retry_base_ms=DEFAULT_RETRY_BASE_MS,
        retry_max_ms=DEFAULT_RETRY_MAX_MS,
        record_max_depth=DEFAULT_RECORD_MAX_DEPTH,
        sdcard_factory=None,
        mount_fn=None,
        umount_fn=None,
        os_module=os,
        open_fn=open,
        clock=now_ms,
    ):
        if pins is None:
            from config import SD_PINS

            pins = SD_PINS
        self.pins = dict(pins)
        self.mount_point = mount_point.rstrip("/") or "/sd"
        self.log_limit_bytes = max(1, int(log_limit_bytes))
        self.archive_limit = max(0, int(archive_limit))
        self.read_chunk_bytes = max(1, int(read_chunk_bytes))
        self.retry_base_ms = max(1, int(retry_base_ms))
        self.retry_max_ms = max(self.retry_base_ms, int(retry_max_ms))
        self.record_max_depth = max(1, int(record_max_depth))
        self.os = os_module
        self.open_fn = open_fn
        self.clock = clock
        self.sdcard_factory = sdcard_factory or getattr(machine, "SDCard", None)
        self.mount_fn = mount_fn or getattr(os_module, "mount", None)
        self.umount_fn = umount_fn or getattr(os_module, "umount", None)
        self._card = None
        self._mounted = False
        self._available = False
        self._last_error = None
        self._log_bytes = 0
        self._retry_at_ms = None
        self._retry_delay_ms = self.retry_base_ms

    def ensure_mounted(self):
        if self._mounted:
            return True
        if not self._retry_due():
            return False
        if self.sdcard_factory is None or self.mount_fn is None:
            self._invalidate("SDCard support unavailable")
            return False
        try:
            self._card = self.sdcard_factory(slot=2, **self.pins)
            self.mount_fn(self._card, self.mount_point)
            self._log_bytes = self._file_size(self._log_path())
        except Exception as exc:
            self._invalidate(exc)
            return False
        self._mounted = True
        self._available = True
        self._last_error = None
        self._retry_at_ms = None
        self._retry_delay_ms = self.retry_base_ms
        return True

    def unmount(self):
        if self._card is None and not self._mounted:
            return False
        failures = self._cleanup_card()
        if failures:
            self._set_error(failures[0])
            return False
        return True

    def append_log(self, record):
        error = self.record_error(record, self.record_max_depth)
        if error is not None:
            self._set_error(error)
            return False
        try:
            entry = self._encode_record(record).encode("utf-8")
        except Exception as exc:
            self._set_error(exc)
            return False
        if len(entry) > self.log_limit_bytes:
            self._set_error("log record too large")
            return False
        if not self.ensure_mounted():
            return False
        try:
            if self._log_bytes + len(entry) > self.log_limit_bytes:
                self._rotate_logs()
            with self.open_fn(self._log_path(), "ab") as log_file:
                log_file.write(entry)
            self._log_bytes += len(entry)
        except Exception as exc:
            self._invalidate(exc)
            return False
        self._last_error = None
        return True

    def read_update(self, sequence, filename, offset=0, max_bytes=None):
        try:
            path = self._update_path(sequence, filename)
            if not isinstance(offset, int) or offset < 0:
                raise ValueError("invalid update offset")
            if max_bytes is None:
                read_size = self.read_chunk_bytes
            elif (
                not isinstance(max_bytes, int)
                or isinstance(max_bytes, bool)
                or not 1 <= max_bytes <= 4096
            ):
                raise ValueError("invalid update read size")
            else:
                read_size = min(self.read_chunk_bytes, max_bytes)
        except ValueError:
            self._set_error("invalid update path")
            return None
        if not self.ensure_mounted():
            return None
        try:
            with self.open_fn(path, "rb") as update_file:
                if offset:
                    update_file.seek(offset)
                data = update_file.read(read_size)
        except Exception as exc:
            self._invalidate(exc)
            return None
        self._last_error = None
        return data

    def snapshot(self):
        """纯内存快照，供安全主循环读取且绝不触碰文件系统。"""
        return {
            "available": self._available,
            "mounted": self._mounted,
            "log_bytes": self._log_bytes,
            "last_error": self._last_error,
            "pins": dict(self.pins),
        }

    @staticmethod
    def is_structured_record(record):
        return isinstance(record, dict)

    @classmethod
    def contains_binary(cls, value, max_depth=DEFAULT_RECORD_MAX_DEPTH):
        return cls.record_error(value, max_depth) is not None

    @classmethod
    def record_error(cls, record, max_depth=DEFAULT_RECORD_MAX_DEPTH):
        if not cls.is_structured_record(record):
            return "diagnostic record must be structured"
        active = set()
        return cls._value_error(record, 0, max_depth, active)

    @classmethod
    def _value_error(cls, value, depth, max_depth, active):
        if depth > max_depth:
            return "diagnostic record nesting too deep"
        if isinstance(value, (bytes, bytearray, memoryview)):
            return "binary media records are not supported"
        if not isinstance(value, (dict, list, tuple)):
            return None
        value_id = id(value)
        if value_id in active:
            return "diagnostic record contains cycle"
        active.add(value_id)
        try:
            values = value.items() if isinstance(value, dict) else enumerate(value)
            for key, item in values:
                error = cls._value_error(key, depth + 1, max_depth, active)
                if error is not None:
                    return error
                error = cls._value_error(item, depth + 1, max_depth, active)
                if error is not None:
                    return error
        finally:
            active.remove(value_id)
        return None

    def _encode_record(self, record):
        try:
            return json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
        except TypeError:
            return json.dumps(record, separators=(",", ":")) + "\n"

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
        self._log_bytes = 0

    def _invalidate(self, error):
        self._cleanup_card()
        self._set_error(error)
        self._retry_at_ms = self.clock() + self._retry_delay_ms
        self._retry_delay_ms = min(self.retry_max_ms, self._retry_delay_ms * 2)

    def _cleanup_card(self):
        card = self._card
        failures = []
        if card is not None and self.umount_fn is not None:
            try:
                self.umount_fn(self.mount_point)
            except Exception as exc:
                failures.append(exc)
        if card is not None and hasattr(card, "deinit"):
            try:
                card.deinit()
            except Exception as exc:
                failures.append(exc)
        self._card = None
        self._mounted = False
        self._available = False
        self._log_bytes = 0
        return failures

    def _retry_due(self):
        return self._retry_at_ms is None or ticks_diff(self.clock(), self._retry_at_ms) >= 0

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
