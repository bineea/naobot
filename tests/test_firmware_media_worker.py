from __future__ import annotations

import asyncio
import importlib
import sys
import threading
import time
from pathlib import Path

FIRMWARE_ROOT = Path(__file__).resolve().parents[1] / "firmware" / "esp32"
if str(FIRMWARE_ROOT) not in sys.path:
    sys.path.insert(0, str(FIRMWARE_ROOT))

runtime_worker = importlib.import_module("media.runtime_worker")
MediaRuntimeWorker = runtime_worker.MediaRuntimeWorker
MediaClient = importlib.import_module("media.client").MediaClient


class ThreadModule:
    allocate_lock = staticmethod(threading.Lock)

    def __init__(self) -> None:
        self.threads = []

    def start_new_thread(self, target, args):
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()
        self.threads.append(thread)
        return thread.ident


def wait_until(predicate, timeout=1.0):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return False


class RecordingClient:
    def __init__(self, state, release=None) -> None:
        self.state = state
        self.release = release
        self.created_thread = threading.get_ident()
        self.step_threads = []
        self.close_threads = []
        self.steps = 0

    def step(self):
        self.steps += 1
        self.step_threads.append(threading.get_ident())
        if self.release is not None:
            self.release.wait(timeout=1)
        self.state.update(
            {
                "media_connected": True,
                "camera_fps": 10,
                "audio_state": "listening",
                "media_queue": 2,
                "media_dropped": 1,
                "psram_free": 1234,
            }
        )
        return True

    def close(self):
        self.close_threads.append(threading.get_ident())


def test_worker_owns_client_step_and_close_on_media_thread() -> None:
    caller_thread = threading.get_ident()
    thread_module = ThreadModule()
    clients = []

    def factory(state):
        client = RecordingClient(state)
        clients.append(client)
        return client

    worker = MediaRuntimeWorker(
        factory,
        thread_module=thread_module,
        active_delay_ms=1,
        reconnect_delay_ms=1,
    )

    assert worker.start() is True
    assert wait_until(lambda: clients and clients[0].steps > 0)
    assert worker.stop() is True
    assert wait_until(worker.is_stopped)

    client = clients[0]
    assert client.created_thread != caller_thread
    assert set(client.step_threads) == {client.created_thread}
    assert client.close_threads == [client.created_thread]
    assert worker.snapshot()["runtime_state"] == "stopped"


def test_blocking_media_step_does_not_stall_asyncio_safety_ticks() -> None:
    release = threading.Event()
    entered = threading.Event()

    class BlockingClient(RecordingClient):
        def step(self):
            entered.set()
            return super().step()

    worker = MediaRuntimeWorker(
        lambda state: BlockingClient(state, release),
        thread_module=ThreadModule(),
        active_delay_ms=1,
        reconnect_delay_ms=1,
    )
    assert worker.start() is True
    assert entered.wait(timeout=1)

    async def safety_ticks():
        started = time.perf_counter()
        for _ in range(4):
            await asyncio.sleep(0.005)
        return time.perf_counter() - started

    elapsed = asyncio.run(safety_ticks())
    assert elapsed < 0.1
    started = time.perf_counter()
    assert worker.stop() is True
    assert time.perf_counter() - started < 0.02
    assert worker.is_stopped() is False
    release.set()
    assert wait_until(worker.is_stopped)


def test_thread_unavailable_disables_media_without_constructing_client() -> None:
    calls = []
    worker = MediaRuntimeWorker(lambda state: calls.append(state), thread_module=None)

    assert worker.start() is False
    assert calls == []
    assert worker.snapshot() == {
        "runtime_state": "disabled",
        "media_connected": False,
        "camera_fps": 0,
        "audio_state": "unavailable",
        "media_queue": 0,
        "media_dropped": 0,
        "psram_free": 0,
        "last_error": "_thread unavailable",
    }


def test_event_boost_uses_command_mailbox_and_snapshot_is_a_copy() -> None:
    release = threading.Event()
    clients = []

    def factory(state):
        client = RecordingClient(state, release)
        clients.append(client)
        return client

    worker = MediaRuntimeWorker(factory, thread_module=ThreadModule())
    assert worker.start() is True
    assert wait_until(lambda: bool(clients))
    assert worker.request_event_boost(4321) is True
    release.set()
    assert wait_until(lambda: clients[0].state.get("event_boost_until_ms") == 4321)

    snapshot = worker.snapshot()
    snapshot["camera_fps"] = 999
    assert worker.snapshot()["camera_fps"] != 999
    worker.stop()
    assert wait_until(worker.is_stopped)


def test_real_media_client_connects_transport_on_its_owner_thread() -> None:
    owner_threads = []
    transport_threads = []

    class Device:
        available = False

        def psram_free(self):
            return 0

        def close(self):
            return None

        def capture(self):
            return None

        def read_chunk(self):
            return None

        def write(self, _payload):
            return 0

    class Transport:
        connected = False
        tx_pending = False

        def connect(self):
            transport_threads.append(threading.get_ident())
            self.connected = True
            return True

        def send_text(self, _payload):
            return True

        def recv_frame(self):
            return None

        def send_binary(self, _payload):
            return True

        def close(self, **_kwargs):
            self.connected = False

    transport = Transport()

    def factory(state):
        owner_threads.append(threading.get_ident())
        return MediaClient(
            "ws://host/ws/media",
            device_id="robot-1",
            token="secret",
            boot_id="boot-1",
            camera=Device(),
            audio_input=Device(),
            audio_output=Device(),
            transport_factory=lambda _url: transport,
            state=state,
        )

    worker = MediaRuntimeWorker(
        factory,
        thread_module=ThreadModule(),
        active_delay_ms=1,
        reconnect_delay_ms=1,
    )
    assert worker.start() is True
    assert wait_until(lambda: bool(transport_threads))
    worker.stop()
    assert wait_until(worker.is_stopped)

    assert transport_threads == owner_threads


def test_worker_failure_stops_media_and_preserves_diagnostic_error() -> None:
    class FailingClient:
        def __init__(self, _state):
            self.closed = False

        def step(self):
            raise OSError("camera stalled")

        def close(self):
            self.closed = True

    clients = []

    def factory(state):
        client = FailingClient(state)
        clients.append(client)
        return client

    worker = MediaRuntimeWorker(factory, thread_module=ThreadModule())
    assert worker.start() is True
    assert wait_until(worker.is_stopped)

    assert clients[0].closed is True
    assert worker.snapshot()["runtime_state"] == "stopped"
    assert worker.snapshot()["last_error"] == "camera stalled"
