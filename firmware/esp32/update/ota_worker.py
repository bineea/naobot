try:
    import _thread
except ImportError:
    _thread = None

try:
    import utime as time
except ImportError:
    import time

try:
    import nao_ota as default_ota_module
except ImportError:
    default_ota_module = None


SUPPORTED_OPERATIONS = ("finish", "activate", "abort")


def sleep_ms(delay_ms):
    if hasattr(time, "sleep_ms"):
        time.sleep_ms(delay_ms)
    else:
        time.sleep(delay_ms / 1000)


class OtaWorker:
    """单线程串行执行可能阻塞安全循环的 OTA 原生操作。"""

    def __init__(
        self,
        ota_module=default_ota_module,
        thread_module=_thread,
        sleeper=sleep_ms,
        idle_delay_ms=10,
    ):
        self.ota = ota_module
        self.thread_module = thread_module
        self.sleeper = sleeper
        self.idle_delay_ms = max(1, int(idle_delay_ms))
        self._lock = thread_module.allocate_lock() if thread_module is not None else None
        self._state = "idle"
        self._pending = None
        self._active = False
        self._stop_requested = False
        self._last_error = None

    def start(self):
        if self.thread_module is None or self.ota is None:
            self._publish_disabled("_thread unavailable" if self.thread_module is None else "nao_ota unavailable")
            return False
        self._acquire()
        try:
            if self._state not in ("idle", "stopped"):
                return False
            self._state = "starting"
            self._stop_requested = False
            self._last_error = None
        finally:
            self._release()
        try:
            self.thread_module.start_new_thread(self._run, ())
            return True
        except Exception as exc:
            self._publish_disabled(str(exc) or "OTA worker start failed")
            return False

    def stop(self):
        self._acquire()
        try:
            if self._state in ("idle", "disabled", "stopped"):
                return False
            self._stop_requested = True
            return True
        finally:
            self._release()

    def submit(self, operation):
        if operation not in SUPPORTED_OPERATIONS:
            return {
                "accepted": False,
                "reason": "unsupported OTA worker operation",
            }
        self._acquire()
        try:
            if self._state != "running":
                return {"accepted": False, "reason": "OTA worker not running"}
            if self._pending is not None or self._active:
                return {"accepted": False, "reason": "OTA worker busy"}
            request = {
                "accepted": True,
                "operation": operation,
                "done": False,
                "result": None,
                "error": None,
            }
            self._pending = request
            return request
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

    def snapshot(self):
        self._acquire()
        try:
            return {
                "runtime_state": self._state,
                "busy": self._pending is not None or self._active,
                "last_error": self._last_error,
            }
        finally:
            self._release()

    def tick(self):
        return False

    def _run(self):
        self._set_state("running")
        try:
            while not self._should_stop():
                if not self._run_one():
                    self.sleeper(self.idle_delay_ms)
        finally:
            self._set_state("stopped")

    def _run_one(self):
        self._acquire()
        try:
            if self._pending is None:
                return False
            request = self._pending
            self._pending = None
            self._active = True
        finally:
            self._release()

        result = None
        error = None
        try:
            result = getattr(self.ota, request["operation"])()
        except Exception as exc:
            error = str(exc) or "OTA native operation failed"

        self._acquire()
        try:
            request["result"] = result
            request["error"] = error
            request["done"] = True
            self._active = False
            if error is not None:
                self._last_error = error
        finally:
            self._release()
        return True

    def _publish_disabled(self, error):
        self._acquire()
        try:
            self._state = "disabled"
            self._last_error = error
        finally:
            self._release()

    def _set_state(self, state):
        self._acquire()
        try:
            self._state = state
        finally:
            self._release()

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
    return OtaWorker()
