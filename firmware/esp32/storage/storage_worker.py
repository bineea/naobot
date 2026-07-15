try:
    import _thread
except ImportError:
    _thread = None

try:
    import utime as time
except ImportError:
    import time

from storage.sd_storage import SDStorage

_UNSET = object()


def sleep_ms(delay_ms):
    if hasattr(time, "sleep_ms"):
        time.sleep_ms(delay_ms)
    else:
        time.sleep(delay_ms / 1000)


class StorageWorker:
    """独立线程独占 SDStorage；主循环只进行有界的内存交换。"""

    def __init__(
        self,
        storage=None,
        queue_limit=16,
        thread_module=_thread,
        sleeper=sleep_ms,
        idle_delay_ms=25,
    ):
        self._storage = storage or SDStorage()
        self.queue_limit = max(1, int(queue_limit))
        self.thread_module = thread_module
        self.sleeper = sleeper
        self.idle_delay_ms = max(1, int(idle_delay_ms))
        self._lock = thread_module.allocate_lock() if thread_module is not None else None
        self._queue = []
        self._dropped = 0
        self._last_error = None
        self._stop_requested = False
        self._state = "idle"
        self._snapshot = self._snapshot_with_queue(self._storage.snapshot())

    def start(self):
        if self.thread_module is None:
            self._publish_disabled("_thread unavailable")
            return False
        self._acquire()
        try:
            if self._state not in ("idle", "stopped"):
                return False
            self._stop_requested = False
            self._publish_locked(runtime_state="starting")
        finally:
            self._release()
        try:
            self.thread_module.start_new_thread(self._run, ())
            return True
        except Exception as exc:
            self._publish_disabled(str(exc))
            return False

    def stop(self):
        self._acquire()
        try:
            if self._state in ("idle", "stopped", "disabled"):
                return False
            self._stop_requested = True
            return True
        finally:
            self._release()

    def submit_log(self, record):
        if not SDStorage.is_structured_record(record):
            self._set_main_error("diagnostic record rejected")
            return False
        self._acquire()
        try:
            if len(self._queue) >= self.queue_limit:
                self._dropped += 1
                self._update_queue_snapshot_locked()
                return False
            self._queue.append({"kind": "log", "record": record})
            self._update_queue_snapshot_locked()
            return True
        finally:
            self._release()

    def submit_update_read(self, sequence, filename, offset=0):
        self._acquire()
        try:
            if len(self._queue) >= self.queue_limit:
                self._publish_locked(error="storage queue full")
                return {"accepted": False, "reason": "storage queue full"}
            request = {"accepted": True, "result": None, "error": None}
            self._queue.append(
                {
                    "kind": "update_read",
                    "sequence": sequence,
                    "filename": filename,
                    "offset": offset,
                    "request": request,
                }
            )
            self._update_queue_snapshot_locked()
            return request
        finally:
            self._release()

    def snapshot(self):
        self._acquire()
        try:
            return dict(self._snapshot)
        finally:
            self._release()

    def poll(self, request=None):
        if request is None:
            return None
        self._acquire()
        try:
            return dict(request)
        finally:
            self._release()

    def tick(self):
        """保留兼容入口；它只交换内存状态，绝不执行存储操作。"""
        return False

    def _run(self):
        self._set_state("running")
        shutdown_error = _UNSET
        try:
            while not self._should_stop():
                if not self._run_one():
                    self.sleeper(self.idle_delay_ms)
        finally:
            try:
                self._storage.unmount()
            except Exception as exc:
                shutdown_error = str(exc)
            self._publish_storage_snapshot("stopped", shutdown_error)

    def _run_one(self):
        self._acquire()
        try:
            if not self._queue:
                return False
            item = self._queue.pop(0)
            self._update_queue_snapshot_locked()
        finally:
            self._release()

        result = _UNSET
        error = _UNSET
        try:
            if item["kind"] == "log":
                if not self._storage.append_log(item["record"]):
                    error = self._storage_error()
            else:
                result = self._storage.read_update(
                    item["sequence"], item["filename"], item["offset"]
                )
                if result is None:
                    error = self._storage_error()
        except Exception as exc:
            error = str(exc)
        self._publish_item_result(item, result, error)
        return True

    def _publish_item_result(self, item, result, error):
        storage_snapshot = self._storage.snapshot()
        self._acquire()
        try:
            if item["kind"] == "update_read":
                item["request"]["result"] = None if result is _UNSET else result
                if error is not _UNSET:
                    item["request"]["error"] = error
            self._publish_locked(storage_snapshot, error=error)
        finally:
            self._release()

    def _publish_storage_snapshot(self, runtime_state=None, error=_UNSET):
        storage_snapshot = self._storage.snapshot()
        self._acquire()
        try:
            self._publish_locked(storage_snapshot, runtime_state, error)
        finally:
            self._release()

    def _snapshot_with_queue(self, storage_snapshot):
        snapshot = dict(storage_snapshot)
        snapshot["queue_depth"] = len(self._queue)
        snapshot["dropped"] = self._dropped
        if self._last_error is not None:
            snapshot["last_error"] = self._last_error
        snapshot["runtime_state"] = self._state
        return snapshot

    def _update_queue_snapshot_locked(self):
        self._snapshot["queue_depth"] = len(self._queue)
        self._snapshot["dropped"] = self._dropped
        if self._last_error is not None:
            self._snapshot["last_error"] = self._last_error

    def _set_main_error(self, error):
        self._acquire()
        try:
            self._publish_locked(error=error)
        finally:
            self._release()

    def _publish_disabled(self, error):
        self._publish_storage_snapshot("disabled", error)

    def _set_state(self, state):
        self._publish_storage_snapshot(runtime_state=state)

    def _publish_locked(self, storage_snapshot=None, runtime_state=None, error=_UNSET):
        if error is not _UNSET:
            self._last_error = error
        if runtime_state is not None:
            self._state = runtime_state
        if storage_snapshot is None:
            self._update_queue_snapshot_locked()
        else:
            self._snapshot = self._snapshot_with_queue(storage_snapshot)

    def _storage_error(self):
        return self._storage.snapshot().get("last_error") or "storage operation failed"

    def _should_stop(self):
        self._acquire()
        try:
            return self._stop_requested
        finally:
            self._release()

    def _acquire(self):
        if self._lock is not None:
            self._lock.acquire()

    def _release(self):
        if self._lock is not None:
            self._lock.release()


def create_default_worker():
    from config import (
        STORAGE_ARCHIVE_LIMIT,
        STORAGE_LOG_LIMIT_BYTES,
        STORAGE_QUEUE_LIMIT,
        STORAGE_READ_CHUNK_BYTES,
        STORAGE_RETRY_BASE_MS,
        STORAGE_RETRY_MAX_MS,
        STORAGE_WORKER_IDLE_MS,
    )

    storage = SDStorage(
        log_limit_bytes=STORAGE_LOG_LIMIT_BYTES,
        archive_limit=STORAGE_ARCHIVE_LIMIT,
        read_chunk_bytes=STORAGE_READ_CHUNK_BYTES,
        retry_base_ms=STORAGE_RETRY_BASE_MS,
        retry_max_ms=STORAGE_RETRY_MAX_MS,
    )
    return StorageWorker(storage, STORAGE_QUEUE_LIMIT, idle_delay_ms=STORAGE_WORKER_IDLE_MS)
