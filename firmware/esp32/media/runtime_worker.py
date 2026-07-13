try:
    import _thread
except ImportError:
    _thread = None

try:
    import utime as time
except ImportError:
    import time


DEFAULT_SNAPSHOT = {
    "runtime_state": "idle",
    "media_connected": False,
    "camera_fps": 0,
    "audio_state": "unavailable",
    "media_queue": 0,
    "media_dropped": 0,
    "psram_free": 0,
    "last_error": None,
}


def _sleep_ms(delay_ms):
    if hasattr(time, "sleep_ms"):
        time.sleep_ms(delay_ms)
    else:
        time.sleep(delay_ms / 1000)


class MediaRuntimeWorker:
    """让媒体设备和传输从创建到关闭都由单一线程独占。"""

    def __init__(
        self,
        client_factory,
        thread_module=_thread,
        sleeper=_sleep_ms,
        active_delay_ms=5,
        reconnect_delay_ms=1000,
    ):
        self.client_factory = client_factory
        self.thread_module = thread_module
        self.sleeper = sleeper
        self.active_delay_ms = max(1, active_delay_ms)
        self.reconnect_delay_ms = max(1, reconnect_delay_ms)
        self._lock = thread_module.allocate_lock() if thread_module is not None else None
        self._snapshot = dict(DEFAULT_SNAPSHOT)
        self._stop_requested = False
        self._event_boost_until_ms = 0
        self._state = "idle"

    def start(self):
        if self.thread_module is None:
            self._publish_disabled("_thread unavailable")
            return False
        self._acquire()
        try:
            if self._state not in ("idle", "stopped"):
                return False
            self._stop_requested = False
            self._state = "starting"
            self._snapshot = self._with_runtime(self._snapshot, "starting")
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
            if self._state in ("disabled", "stopped", "idle"):
                return False
            self._stop_requested = True
            return True
        finally:
            self._release()

    def request_event_boost(self, until_ms):
        self._acquire()
        try:
            if self._state in ("disabled", "stopped"):
                return False
            self._event_boost_until_ms = until_ms
            return True
        finally:
            self._release()

    def snapshot(self):
        self._acquire()
        try:
            return dict(self._snapshot)
        finally:
            self._release()

    def is_stopped(self):
        self._acquire()
        try:
            return self._state in ("disabled", "stopped")
        finally:
            self._release()

    def _run(self):
        client = None
        private_state = {}
        last_error = None
        self._set_state("running")
        try:
            client = self.client_factory(private_state)
            while not self._should_stop():
                private_state["event_boost_until_ms"] = self._read_event_boost()
                connected = bool(client.step())
                self._publish(private_state, "running")
                delay = self.active_delay_ms if connected else self.reconnect_delay_ms
                self._sleep_interruptibly(delay)
        except Exception as exc:
            last_error = str(exc)
            self._publish(private_state, "fault", last_error)
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception as exc:
                    last_error = str(exc)
                    self._publish(private_state, "fault", last_error)
            self._publish(private_state, "stopped", last_error)

    def _sleep_interruptibly(self, delay_ms):
        remaining = delay_ms
        while remaining > 0 and not self._should_stop():
            chunk = min(remaining, 50)
            self.sleeper(chunk)
            remaining -= chunk

    def _publish(self, client_state, runtime_state, error=None):
        inactive = runtime_state in ("disabled", "stopped")
        snapshot = {
            "runtime_state": runtime_state,
            "media_connected": False
            if inactive
            else bool(client_state.get("media_connected", False)),
            "camera_fps": int(client_state.get("camera_fps", 0)),
            "audio_state": "unavailable"
            if inactive
            else str(client_state.get("audio_state", "unavailable")),
            "media_queue": int(client_state.get("media_queue", 0)),
            "media_dropped": int(client_state.get("media_dropped", 0)),
            "psram_free": int(client_state.get("psram_free", 0)),
            "last_error": error,
        }
        self._acquire()
        try:
            self._snapshot = snapshot
            self._state = runtime_state
        finally:
            self._release()

    def _publish_disabled(self, error):
        self._publish({}, "disabled", error)

    def _set_state(self, runtime_state):
        self._acquire()
        try:
            self._state = runtime_state
            self._snapshot = self._with_runtime(self._snapshot, runtime_state)
        finally:
            self._release()

    @staticmethod
    def _with_runtime(snapshot, runtime_state):
        updated = dict(snapshot)
        updated["runtime_state"] = runtime_state
        return updated

    def _should_stop(self):
        self._acquire()
        try:
            return self._stop_requested
        finally:
            self._release()

    def _read_event_boost(self):
        self._acquire()
        try:
            return self._event_boost_until_ms
        finally:
            self._release()

    def _acquire(self):
        if self._lock is not None:
            self._lock.acquire()

    def _release(self):
        if self._lock is not None:
            self._lock.release()


def create_default_worker():
    from config import MEDIA_LOOP_INTERVAL_MS, MEDIA_RECONNECT_DELAY_MS

    from media.client import create_media_client

    return MediaRuntimeWorker(
        create_media_client,
        active_delay_ms=MEDIA_LOOP_INTERVAL_MS,
        reconnect_delay_ms=MEDIA_RECONNECT_DELAY_MS,
    )
