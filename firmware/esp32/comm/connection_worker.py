try:
    import _thread
except ImportError:
    _thread = None


class ConnectionWorker:
    def __init__(self, transport_factory):
        self.transport_factory = transport_factory
        self._lock = _thread.allocate_lock() if _thread is not None else None
        self._state = "idle"
        self._result = None
        self.last_error = None

    @property
    def pending(self):
        return self._read_state() == "pending"

    def start(self):
        self._acquire()
        try:
            if self._state != "idle":
                return False
            if _thread is None:
                self.last_error = RuntimeError("_thread unavailable")
                self._state = "ready"
                return False
            self._state = "pending"
            self._result = None
            self.last_error = None
        finally:
            self._release()
        _thread.start_new_thread(self._run, ())
        return True

    def poll(self):
        self._acquire()
        try:
            if self._state != "ready":
                return False, None
            result = self._result
            self._result = None
            self._state = "idle"
            return True, result
        finally:
            self._release()

    def _run(self):
        transport = None
        error = None
        try:
            transport = self.transport_factory()
            if transport is None or not transport.connect():
                transport = None
        except Exception as exc:
            error = exc
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass
            transport = None
        self._acquire()
        try:
            self._result = transport
            self.last_error = error
            self._state = "ready"
        finally:
            self._release()

    def _read_state(self):
        self._acquire()
        try:
            return self._state
        finally:
            self._release()

    def _acquire(self):
        if self._lock is not None:
            self._lock.acquire()

    def _release(self):
        if self._lock is not None:
            self._lock.release()
