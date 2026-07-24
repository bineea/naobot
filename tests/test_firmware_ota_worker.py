from __future__ import annotations

import importlib.util
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "firmware" / "esp32" / "update" / "ota_worker.py"


def load_module():
    assert MODULE_PATH.exists(), "OTA 专属后台 worker 尚未实现"
    spec = importlib.util.spec_from_file_location("ota_worker", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeThreadModule:
    def __init__(self):
        self.started = None

    @staticmethod
    def allocate_lock():
        return threading.Lock()

    def start_new_thread(self, target, args):
        self.started = (target, args)
        return 1


class FakeOta:
    def __init__(self):
        self.calls = []
        self.finish_error = None

    def finish(self):
        self.calls.append("finish")
        if self.finish_error:
            raise OSError(self.finish_error)
        return True

    def activate(self):
        self.calls.append("activate")
        return True

    def begin(self, *_args):
        self.calls.append("begin")

    def write(self, *_args):
        self.calls.append("write")

    def abort(self):
        self.calls.append("abort")
        return True


def make_worker():
    module = load_module()
    ota = FakeOta()
    thread_module = FakeThreadModule()
    worker = module.OtaWorker(
        ota,
        thread_module=thread_module,
        sleeper=lambda _delay: None,
    )
    assert worker.start() is True
    worker._set_state("running")
    return worker, ota, thread_module


def test_worker_serializes_finish_activate_and_abort_outside_submitter() -> None:
    worker, ota, thread_module = make_worker()
    assert thread_module.started is not None

    finish = worker.submit("finish")
    rejected = worker.submit("activate")

    assert finish["accepted"] is True
    assert rejected == {"accepted": False, "reason": "OTA worker busy"}
    assert ota.calls == []
    assert worker._run_one() is True
    assert worker.poll(finish) == {
        "accepted": True,
        "operation": "finish",
        "done": True,
        "result": True,
        "error": None,
    }
    assert ota.calls == ["finish"]

    activate = worker.submit("activate")
    assert worker._run_one() is True
    assert worker.poll(activate)["result"] is True
    assert ota.calls == ["finish", "activate"]

    abort = worker.submit("abort")
    assert worker._run_one() is True
    assert worker.poll(abort)["result"] is True
    assert ota.calls == ["finish", "activate", "abort"]

    for forbidden in ("begin", "write", "unknown"):
        assert worker.submit(forbidden) == {
            "accepted": False,
            "reason": "unsupported OTA worker operation",
        }
    assert ota.calls == ["finish", "activate", "abort"]


def test_worker_publishes_native_exception_without_escaping() -> None:
    worker, ota, _thread_module = make_worker()
    ota.finish_error = "esp_ota_end failed"
    request = worker.submit("finish")

    assert worker._run_one() is True

    assert worker.poll(request)["done"] is True
    assert worker.poll(request)["result"] is None
    assert worker.poll(request)["error"] == "esp_ota_end failed"
    assert worker.snapshot()["last_error"] == "esp_ota_end failed"


def test_worker_is_fail_closed_without_thread_support() -> None:
    module = load_module()
    worker = module.OtaWorker(FakeOta(), thread_module=None)

    assert worker.start() is False
    assert worker.submit("finish") == {
        "accepted": False,
        "reason": "OTA worker not running",
    }
    assert worker.snapshot()["runtime_state"] == "disabled"
