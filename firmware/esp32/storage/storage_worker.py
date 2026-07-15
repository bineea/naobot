from storage.sd_storage import SDStorage


class StorageWorker:
    """存储请求的协作式单一运行时所有者。"""

    def __init__(self, storage=None, queue_limit=16):
        self.storage = storage or SDStorage()
        self.queue_limit = max(1, int(queue_limit))
        self._queue = []
        self._dropped = 0
        self._last_error = None

    def submit_log(self, record):
        if not SDStorage.is_structured_record(record) or SDStorage.contains_binary(record):
            self._last_error = "diagnostic record rejected"
            return False
        if len(self._queue) >= self.queue_limit:
            self._dropped += 1
            return False
        self._queue.append({"kind": "log", "record": record})
        return True

    def submit_update_read(self, sequence, filename, offset=0):
        if len(self._queue) >= self.queue_limit:
            self._last_error = "storage queue full"
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
        return request

    def tick(self):
        if not self._queue:
            return False
        request = self._queue.pop(0)
        try:
            if request["kind"] == "log":
                if not self.storage.append_log(request["record"]):
                    self._last_error = self.storage.snapshot()["last_error"]
            else:
                result = self.storage.read_update(
                    request["sequence"], request["filename"], request["offset"]
                )
                request["request"]["result"] = result
                if result is None:
                    request["request"]["error"] = self.storage.snapshot()["last_error"]
                    self._last_error = request["request"]["error"]
        except Exception as exc:
            self._last_error = str(exc)
            if request["kind"] == "update_read":
                request["request"]["error"] = self._last_error
        return True

    def snapshot(self):
        snapshot = self.storage.snapshot()
        snapshot["queue_depth"] = len(self._queue)
        snapshot["dropped"] = self._dropped
        if self._last_error is not None:
            snapshot["last_error"] = self._last_error
        return snapshot


def create_default_worker():
    from config import STORAGE_QUEUE_LIMIT, STORAGE_READ_CHUNK_BYTES

    return StorageWorker(SDStorage(read_chunk_bytes=STORAGE_READ_CHUNK_BYTES), STORAGE_QUEUE_LIMIT)
