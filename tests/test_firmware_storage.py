import sys
import threading
from pathlib import Path

import pytest

FIRMWARE_ROOT = Path(__file__).resolve().parents[1] / "firmware" / "esp32"
if str(FIRMWARE_ROOT) not in sys.path:
    sys.path.insert(0, str(FIRMWARE_ROOT))

class FakeSDCard:
    calls = []
    deinit_calls = 0

    def __init__(self, **kwargs):
        type(self).calls.append(kwargs)

    def deinit(self):
        type(self).deinit_calls += 1


class ManualThread:
    def __init__(self):
        self.target = None
        self.args = None

    @staticmethod
    def allocate_lock():
        return threading.Lock()

    def start_new_thread(self, target, args):
        self.target = target
        self.args = args
        return 1


class GatedThread(ManualThread):
    def __init__(self):
        super().__init__()
        self.thread = None

    def release(self):
        self.thread = threading.Thread(target=self.target, args=self.args, daemon=True)
        self.thread.start()


class InstrumentedLock:
    def __init__(self):
        self._lock = threading.RLock()
        self.depth = 0

    @property
    def held(self):
        return self.depth > 0

    def acquire(self):
        self._lock.acquire()
        self.depth += 1

    def release(self):
        self.depth -= 1
        self._lock.release()


class InstrumentedThread(ManualThread):
    def __init__(self):
        super().__init__()
        self.lock = InstrumentedLock()

    def allocate_lock(self):
        return self.lock


def create_storage(tmp_path, **kwargs):
    from storage.sd_storage import SDStorage

    FakeSDCard.calls = []
    FakeSDCard.deinit_calls = 0
    mount_point = tmp_path / "sd"

    def mount(_card, path):
        Path(path).mkdir(parents=True, exist_ok=True)

    return SDStorage(
        mount_point=str(mount_point),
        sdcard_factory=FakeSDCard,
        mount_fn=mount,
        **kwargs,
    )


def storage_worker_class():
    from storage.storage_worker import StorageWorker

    return StorageWorker


def error_checked_worker_class():
    worker_class = storage_worker_class()

    class ErrorCheckedWorker(worker_class):
        def __setattr__(self, name, value):
            lock = self.__dict__.get("_lock")
            if (
                name == "_last_error"
                and "_last_error" in self.__dict__
                and lock is not None
                and not lock.held
            ):
                raise AssertionError("_last_error must be published while holding the worker lock")
            super().__setattr__(name, value)

    return ErrorCheckedWorker


def test_mounting_is_lazy_idempotent_and_uses_xiao_pins(tmp_path):
    storage = create_storage(tmp_path)

    assert FakeSDCard.calls == []
    assert storage.snapshot() == {
        "available": False,
        "mounted": False,
        "log_bytes": 0,
        "last_error": None,
        "pins": {"cs": 3, "sck": 7, "miso": 8, "mosi": 9},
    }

    assert storage.ensure_mounted() is True
    assert storage.ensure_mounted() is True
    assert FakeSDCard.calls == [{"slot": 2, "cs": 3, "sck": 7, "miso": 8, "mosi": 9}]
    assert storage.snapshot()["available"] is True
    assert storage.snapshot()["mounted"] is True


def test_missing_card_degrades_without_raising(tmp_path):
    def missing_card(**_kwargs):
        raise OSError("no card")

    from storage.sd_storage import SDStorage

    storage = SDStorage(
        mount_point=str(tmp_path / "sd"),
        sdcard_factory=missing_card,
        mount_fn=lambda _card, _path: None,
    )

    assert storage.append_log({"event": "boot"}) is False
    snapshot = storage.snapshot()
    assert snapshot["available"] is False
    assert snapshot["mounted"] is False
    assert snapshot["last_error"] == "no card"


def test_diagnostic_logs_reject_binary_media_recursively(tmp_path):
    storage = create_storage(tmp_path)

    assert storage.append_log({"event": "audio", "payload": [b"pcm"]}) is False
    assert storage.append_log({"event": "video", "payload": {"frame": memoryview(b"jpeg")}}) is False
    assert not (tmp_path / "sd" / "naobot.log").exists()
    assert storage.snapshot()["last_error"] == "binary media records are not supported"


def test_log_rotation_retains_only_configured_archives(tmp_path):
    storage = create_storage(tmp_path, log_limit_bytes=32, archive_limit=2)
    log_path = tmp_path / "sd" / "naobot.log"
    archive_one = tmp_path / "sd" / "naobot.log.1"
    archive_two = tmp_path / "sd" / "naobot.log.2"

    log_path.parent.mkdir(parents=True)
    log_path.write_bytes(b"x" * 32)
    archive_one.write_text("newer archive")
    archive_two.write_text("oldest archive")
    assert storage.ensure_mounted() is True

    assert storage.append_log({"event": "rotated"}) is True
    assert archive_one.read_bytes() == b"x" * 32
    assert archive_two.read_text() == "newer archive"
    assert log_path.read_text() == '{"event":"rotated"}\n'


def test_log_never_exceeds_exact_utf8_limit_and_rotates_non_ascii_records(tmp_path):
    entry = '{"event":"\u96ea"}\n'.encode()
    storage = create_storage(tmp_path, log_limit_bytes=len(entry))
    log_path = tmp_path / "sd" / "naobot.log"

    assert storage.append_log({"event": "\u96ea"}) is True
    assert storage.snapshot()["log_bytes"] == len(entry)
    assert log_path.read_bytes() == entry
    assert storage.append_log({"event": "\u96ea"}) is True
    assert log_path.read_bytes() == entry
    assert (tmp_path / "sd" / "naobot.log.1").read_bytes() == entry
    assert storage.snapshot()["log_bytes"] == len(entry)


def test_log_rejects_a_single_record_larger_than_the_active_log_limit(tmp_path):
    storage = create_storage(tmp_path, log_limit_bytes=16)

    assert storage.append_log({"event": "x" * 32}) is False
    assert storage.snapshot()["log_bytes"] == 0
    assert storage.snapshot()["last_error"] == "log record too large"
    assert not (tmp_path / "sd" / "naobot.log").exists()


def test_default_rotation_keeps_seven_archives_and_tolerates_gaps(tmp_path):
    storage = create_storage(tmp_path, log_limit_bytes=8)
    root = tmp_path / "sd"
    log_path = root / "naobot.log"

    root.mkdir(parents=True)
    log_path.write_bytes(b"old-log!")
    (root / "naobot.log.1").write_bytes(b"one")
    (root / "naobot.log.3").write_bytes(b"three")
    (root / "naobot.log.7").write_bytes(b"seven")
    assert storage.ensure_mounted() is True

    assert storage.append_log({"x": 1}) is True
    assert (root / "naobot.log.1").read_bytes() == b"old-log!"
    assert (root / "naobot.log.2").read_bytes() == b"one"
    assert not (root / "naobot.log.3").exists()
    assert (root / "naobot.log.4").read_bytes() == b"three"
    assert not (root / "naobot.log.7").exists()


def test_binary_validation_rejects_deep_and_cyclic_diagnostics_without_recursing_forever(tmp_path):
    storage = create_storage(tmp_path)
    cyclic = {"event": "cycle"}
    cyclic["self"] = cyclic
    deeply_nested = {"event": "deep"}
    cursor = deeply_nested
    for _ in range(32):
        cursor["child"] = {}
        cursor = cursor["child"]

    assert storage.append_log(cyclic) is False
    assert storage.snapshot()["last_error"] == "diagnostic record contains cycle"
    assert storage.append_log(deeply_nested) is False
    assert storage.snapshot()["last_error"] == "diagnostic record nesting too deep"


def test_mount_and_io_failures_invalidate_card_then_retry_after_backoff(tmp_path):
    from storage.sd_storage import SDStorage

    clock = [0]
    mounts = []
    umounts = []

    def mount(_card, path):
        mounts.append(path)
        if len(mounts) == 1:
            raise OSError("mount failed")
        Path(path).mkdir(parents=True, exist_ok=True)

    storage = SDStorage(
        mount_point=str(tmp_path / "sd"),
        sdcard_factory=FakeSDCard,
        mount_fn=mount,
        umount_fn=lambda path: umounts.append(path),
        clock=lambda: clock[0],
        retry_base_ms=10,
        retry_max_ms=40,
    )

    assert storage.ensure_mounted() is False
    assert storage.snapshot()["mounted"] is False
    assert FakeSDCard.deinit_calls == 1
    assert storage.ensure_mounted() is False
    assert len(mounts) == 1
    clock[0] = 10
    assert storage.ensure_mounted() is True
    storage.open_fn = lambda *_args: (_ for _ in ()).throw(OSError("card removed"))
    assert storage.append_log({"event": "write"}) is False
    assert storage.snapshot()["available"] is False
    assert storage.snapshot()["mounted"] is False
    assert FakeSDCard.deinit_calls == 2
    assert umounts == [str(tmp_path / "sd"), str(tmp_path / "sd")]


@pytest.mark.parametrize("sequence, filename", [
    ("../escape", "firmware.bin"),
    ("20260715", "../firmware.bin"),
    ("20260715", "nested/firmware.bin"),
    ("20260715", "nested\\firmware.bin"),
    ("/absolute", "firmware.bin"),
])
def test_update_reads_are_bounded_and_reject_path_escape(tmp_path, sequence, filename):
    storage = create_storage(tmp_path, read_chunk_bytes=4)
    update_dir = tmp_path / "sd" / "updates" / "20260715"

    assert storage.ensure_mounted() is True
    update_dir.mkdir(parents=True)
    (update_dir / "firmware.bin").write_bytes(b"abcdefgh")

    assert storage.read_update("20260715", "firmware.bin") == b"abcd"
    assert storage.read_update(sequence, filename) is None
    assert storage.snapshot()["last_error"] == "invalid update path"


def test_worker_drops_logs_under_queue_pressure_and_rejects_updates(tmp_path):
    worker = storage_worker_class()(create_storage(tmp_path), queue_limit=1, thread_module=ManualThread())

    assert worker.start() is True
    assert worker.submit_log({"event": "first"}) is True
    assert worker.submit_log({"event": "second"}) is False
    rejected = worker.submit_update_read("20260715", "firmware.bin")

    assert rejected == {"accepted": False, "reason": "storage queue full"}
    assert worker.snapshot()["queue_depth"] == 1
    assert worker.snapshot()["dropped"] == 1
    assert worker.tick() is False
    assert worker._run_one() is True
    assert worker.snapshot()["queue_depth"] == 0


def test_worker_processes_bounded_update_reads_cooperatively(tmp_path):
    storage = create_storage(tmp_path, read_chunk_bytes=3)
    worker = storage_worker_class()(storage, queue_limit=2, thread_module=ManualThread())
    update_dir = tmp_path / "sd" / "updates" / "20260715"

    assert storage.ensure_mounted() is True
    update_dir.mkdir(parents=True)
    (update_dir / "firmware.bin").write_bytes(b"abcdef")

    assert worker.start() is True
    request = worker.submit_update_read("20260715", "firmware.bin")

    assert worker.poll(request) == {"accepted": True, "result": None, "error": None}
    assert worker._run_one() is True
    assert worker.poll(request) == {"accepted": True, "result": b"abc", "error": None}


def test_main_side_worker_snapshot_tick_and_poll_do_not_run_blocking_storage(tmp_path):
    class BlockingStorage:
        def __init__(self):
            self.entered = threading.Event()
            self.release = threading.Event()
            self.append_calls = 0

        def append_log(self, _record):
            self.append_calls += 1
            self.entered.set()
            self.release.wait(1)
            return True

        @staticmethod
        def snapshot():
            return {
                "available": False,
                "mounted": False,
                "log_bytes": 0,
                "last_error": None,
                "pins": {},
            }

    thread_module = GatedThread()
    storage = BlockingStorage()
    worker = storage_worker_class()(storage, queue_limit=1, thread_module=thread_module, idle_delay_ms=1)

    assert worker.start() is True
    assert worker.submit_log({"event": "queued"}) is True
    assert worker.snapshot()["queue_depth"] == 1
    assert worker.tick() is False
    assert worker.poll() is None
    assert storage.append_calls == 0
    thread_module.release()
    assert storage.entered.wait(1)
    assert worker.snapshot()["queue_depth"] == 0
    worker.stop()
    storage.release.set()
    thread_module.thread.join(1)


@pytest.mark.parametrize("mode", ["log_failure", "read_failure", "read_exception", "unmount_exception"])
def test_worker_publishes_every_thread_error_under_lock(mode):
    class ErrorStorage:
        def append_log(self, _record):
            return False

        def read_update(self, *_args):
            if mode == "read_exception":
                raise OSError("read exploded")
            return None

        def unmount(self):
            if mode == "unmount_exception":
                raise OSError("unmount exploded")

        @staticmethod
        def snapshot():
            return {
                "available": False,
                "mounted": False,
                "log_bytes": 0,
                "last_error": "storage failed",
                "pins": {},
            }

    thread_module = InstrumentedThread()
    worker = error_checked_worker_class()(ErrorStorage(), thread_module=thread_module)

    assert worker.start() is True
    if mode == "log_failure":
        assert worker.submit_log({"event": "failed"}) is True
        assert worker._run_one() is True
    elif mode in ("read_failure", "read_exception"):
        request = worker.submit_update_read("20260715", "firmware.bin")
        assert worker._run_one() is True
        assert worker.poll(request)["error"] in ("storage failed", "read exploded")
    else:
        assert thread_module.target is not None
        assert worker.stop() is True
        thread_module.target(*thread_module.args)

    snapshot = worker.snapshot()
    assert snapshot["runtime_state"] in ("starting", "stopped")
    assert snapshot["last_error"] in ("storage failed", "read exploded", "unmount exploded")


def test_protocol_heartbeat_includes_storage_telemetry():
    import main as firmware_main

    class Power:
        battery_pct = 80

    class Imu:
        posture = "upright"

    payload = firmware_main.FirmwareProtocol("storage-test").heartbeat(
        Power(),
        Imu(),
        state={
            "storage": {
                "available": True,
                "mounted": True,
                "queue_depth": 2,
                "dropped": 3,
                "last_error": "retrying",
            }
        },
    )["payload"]

    assert payload["sd_available"] is True
    assert payload["sd_mounted"] is True
    assert payload["storage_queue"] == 2
    assert payload["storage_dropped"] == 3
    assert payload["storage_last_error"] == "retrying"
