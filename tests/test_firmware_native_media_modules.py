from __future__ import annotations

import errno
import importlib
import sys
import threading
import time
from collections import deque
from pathlib import Path

import pytest


FIRMWARE_ROOT = Path(__file__).resolve().parents[1] / "firmware" / "esp32"
BUILD_ROOT = FIRMWARE_ROOT / "build"
if str(FIRMWARE_ROOT) not in sys.path:
    sys.path.insert(0, str(FIRMWARE_ROOT))

MediaRuntimeWorker = importlib.import_module("media.runtime_worker").MediaRuntimeWorker
SUPPORTED_SENSORS = {
    0x2642: "OV2640",
    0x3660: "OV3660",
    0x5640: "OV5640",
}


class FakeNativePDM:
    """CPython fake，刻画原生模块的线程所有权和非阻塞语义。"""

    def __init__(self, chunks=()):
        self._chunks = deque(bytes(chunk) for chunk in chunks)
        self._owner = None
        self._initialized = False
        self._rate_hz = 0
        self._buffer_bytes = 0
        self._queued_bytes = 0
        self._dropped_bytes = 0
        self._read_bytes = 0
        self._read_calls = 0
        self._overruns = 0
        self._last_error = 0

    def _guard(self):
        if self._owner is not None and threading.get_ident() != self._owner:
            raise OSError(errno.EPERM, "pdm owner thread required")

    def init(
        self,
        *,
        clk_pin: int,
        data_pin: int,
        sample_rate_hz=16_000,
        bits=16,
        channels=1,
        buffer_bytes=8_000,
    ) -> None:
        del clk_pin, data_pin
        self._guard()
        if (sample_rate_hz, bits, channels) != (16_000, 16, 1):
            raise ValueError("pdm supports only 16000 Hz, 16-bit, mono")
        if buffer_bytes <= 0 or buffer_bytes % 2:
            raise ValueError("buffer_bytes must be a positive even integer")
        if self._owner is None:
            self._owner = threading.get_ident()
        self._initialized = True
        self._rate_hz = sample_rate_hz
        self._buffer_bytes = buffer_bytes
        self._queued_bytes = sum(map(len, self._chunks))
        return None

    def read(self, max_bytes: int):
        self._guard()
        if max_bytes <= 0 or max_bytes % 2:
            raise ValueError("max_bytes must be a positive even integer")
        self._read_calls += 1
        if not self._initialized or not self._chunks:
            return None
        payload = self._chunks.popleft()
        result = payload[:max_bytes]
        remainder = payload[max_bytes:]
        if remainder:
            self._chunks.appendleft(remainder)
        self._queued_bytes -= len(result)
        self._read_bytes += len(result)
        return result

    def available(self) -> bool:
        self._guard()
        return self._initialized and bool(self._chunks)

    def deinit(self) -> None:
        self._guard()
        self._initialized = False
        self._rate_hz = 0
        self._queued_bytes = 0
        self._chunks.clear()
        return None

    def stats(self) -> dict:
        self._guard()
        return {
            "initialized": self._initialized,
            "rate_hz": self._rate_hz,
            "queued_bytes": self._queued_bytes,
            "dropped_bytes": self._dropped_bytes,
            "read_bytes": self._read_bytes,
            "read_calls": self._read_calls,
            "overruns": self._overruns,
            "last_error": self._last_error,
        }


class FakeNativeCamera:
    def __init__(self, sensor_pid, frames=(b"jpeg",)):
        self.sensor_pid = sensor_pid
        self.sensor_name = SUPPORTED_SENSORS.get(sensor_pid, "unknown")
        self.frames = deque(frames)
        self.initialized = False
        self.init_err = 0
        self.capture_errors = 0
        self.deinit_calls = 0

    def init(self):
        self.initialized = True
        if self.sensor_pid not in SUPPORTED_SENSORS:
            self.init_err = errno.ENODEV
            self.deinit()
            raise OSError(errno.ENODEV, "unsupported camera sensor")

    def capture(self):
        if not self.frames:
            self.capture_errors += 1
            return None
        return self.frames.popleft()

    def deinit(self):
        self.deinit_calls += 1
        self.initialized = False

    def diagnostics(self):
        return {
            "initialized": self.initialized,
            "init_err": self.init_err,
            "sensor_pid": self.sensor_pid,
            "sensor_name": self.sensor_name,
            "frame_size": 5,
            "pixel_format": 4,
            "jpeg_quality": 12,
            "fb_count": 2,
            "psram_free": 7_654_321,
            "capture_errors": self.capture_errors,
        }


class ThreadModule:
    allocate_lock = staticmethod(threading.Lock)

    def start_new_thread(self, target, args):
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()
        return thread.ident


def wait_until(predicate, timeout=1.0):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return False


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"sample_rate_hz": 8_000}, "16000"),
        ({"bits": 24}, "16-bit"),
        ({"channels": 2}, "mono"),
        ({"buffer_bytes": 7_999}, "buffer_bytes"),
    ],
)
def test_pdm_fake_rejects_unsupported_parameters(override, message) -> None:
    config = {"clk_pin": 42, "data_pin": 41}
    config.update(override)

    with pytest.raises(ValueError, match=message):
        FakeNativePDM().init(**config)


def test_pdm_fake_has_nonblocking_pcm_stats_and_idempotent_deinit() -> None:
    pdm = FakeNativePDM([b"\x01\x02" * 3])
    assert pdm.init(clk_pin=42, data_pin=41) is None
    assert pdm.available() is True
    assert pdm.read(4) == b"\x01\x02" * 2
    assert pdm.read(4) == b"\x01\x02"
    assert pdm.read(4) is None
    assert pdm.stats() == {
        "initialized": True,
        "rate_hz": 16_000,
        "queued_bytes": 0,
        "dropped_bytes": 0,
        "read_bytes": 6,
        "read_calls": 3,
        "overruns": 0,
        "last_error": 0,
    }
    pdm.deinit()
    pdm.deinit()
    assert pdm.stats()["initialized"] is False


@pytest.mark.parametrize("max_bytes", [0, -2, 1, 3])
def test_pdm_fake_requires_positive_even_read_length(max_bytes) -> None:
    pdm = FakeNativePDM()
    pdm.init(clk_pin=42, data_pin=41)

    with pytest.raises(ValueError, match="max_bytes"):
        pdm.read(max_bytes)


def test_pdm_fake_maps_cross_thread_access_to_eperm() -> None:
    pdm = FakeNativePDM()
    pdm.init(clk_pin=42, data_pin=41)
    errors = []

    def access_from_non_owner():
        for operation in (pdm.available, pdm.stats, pdm.deinit, lambda: pdm.read(2)):
            try:
                operation()
            except OSError as exc:
                errors.append(exc.errno)

    thread = threading.Thread(target=access_from_non_owner)
    thread.start()
    thread.join()

    assert errors == [errno.EPERM] * 4


@pytest.mark.parametrize(("pid", "name"), SUPPORTED_SENSORS.items())
def test_camera_fake_accepts_supported_sensor_and_reports_diagnostics(pid, name) -> None:
    camera = FakeNativeCamera(pid)
    camera.init()

    assert camera.diagnostics()["sensor_name"] == name
    assert camera.diagnostics()["sensor_pid"] == pid
    assert camera.diagnostics()["initialized"] is True


def test_camera_fake_deinitializes_unknown_sensor_and_counts_capture_errors() -> None:
    unknown = FakeNativeCamera(0x1234)
    with pytest.raises(OSError, match="unsupported camera sensor"):
        unknown.init()
    assert unknown.deinit_calls == 1
    assert unknown.diagnostics()["initialized"] is False

    camera = FakeNativeCamera(0x2642, frames=())
    camera.init()
    assert camera.capture() is None
    assert camera.diagnostics()["capture_errors"] == 1


def test_worker_samples_diagnostics_on_owner_thread_and_publishes_copies() -> None:
    owner_threads = []
    diagnostic_threads = []

    class Device:
        def diagnostics(self):
            diagnostic_threads.append(threading.get_ident())
            return {"initialized": True, "sensor_name": "OV5640", "sensor_pid": 0x5640}

        def stats(self):
            diagnostic_threads.append(threading.get_ident())
            return {"initialized": True, "rate_hz": 16_000, "read_calls": 2}

    class Client:
        def __init__(self, state):
            self.state = state
            self.camera = Device()
            self.audio_input = Device()
            owner_threads.append(threading.get_ident())

        def step(self):
            return True

        def close(self):
            return None

    worker = MediaRuntimeWorker(Client, thread_module=ThreadModule(), active_delay_ms=1)
    assert worker.start() is True
    assert wait_until(lambda: worker.snapshot()["camera_sensor"].get("sensor_name") == "OV5640")
    snapshot = worker.snapshot()
    snapshot["camera_sensor"]["sensor_name"] = "mutated"
    snapshot["pdm"]["read_calls"] = 0
    worker.stop()
    assert wait_until(worker.is_stopped)

    assert diagnostic_threads and set(diagnostic_threads) == set(owner_threads)
    assert worker.snapshot()["camera_sensor"]["sensor_name"] == "OV5640"
    assert worker.snapshot()["pdm"]["read_calls"] == 2


def test_pdm_c_module_uses_idf_new_driver_nonblocking_read_and_owner_guard() -> None:
    source = (BUILD_ROOT / "pdm_module" / "modpdm.c").read_text(encoding="utf-8")

    for required in (
        '#include "driver/i2s_pdm.h"',
        "i2s_new_channel",
        "i2s_channel_init_pdm_rx_mode",
        "i2s_channel_enable",
        "i2s_channel_read",
        "i2s_channel_disable",
        "i2s_del_channel",
        "xTaskGetCurrentTaskHandle",
        "MP_EPERM",
        "MP_THREAD_GIL_EXIT",
        "MP_THREAD_GIL_ENTER",
        "MP_QSTR_available",
        "MP_QSTR_stats",
    ):
        assert required in source
    assert "portMAX_DELAY" not in source
    assert "i2s_get_buffered_data_len" not in source


def test_camera_c_module_validates_sensor_and_exposes_complete_diagnostics() -> None:
    source = (BUILD_ROOT / "camera_module" / "modcamera.c").read_text(encoding="utf-8")

    for required in (
        "OV2640_PID",
        "OV3660_PID",
        "OV5640_PID",
        "esp_camera_deinit",
        "MP_QSTR_diagnostics",
        "MP_QSTR_initialized",
        "MP_QSTR_init_err",
        "MP_QSTR_sensor_pid",
        "MP_QSTR_sensor_name",
        "MP_QSTR_frame_size",
        "MP_QSTR_pixel_format",
        "MP_QSTR_jpeg_quality",
        "MP_QSTR_fb_count",
        "MP_QSTR_psram_free",
        "MP_QSTR_capture_errors",
    ):
        assert required in source


def test_build_recipe_registers_both_native_modules_and_checks_firmware_size() -> None:
    script = (BUILD_ROOT / "build.ps1").read_text(encoding="utf-8")
    cmake = (BUILD_ROOT / "camera_module" / "micropython.cmake").read_text(
        encoding="utf-8"
    )
    pdm_cmake = (BUILD_ROOT / "pdm_module" / "micropython.cmake").read_text(
        encoding="utf-8"
    )

    assert script.count("USER_C_MODULES=") == 1
    assert "modcamera.c" in cmake
    assert "../pdm_module/micropython.cmake" in cmake
    assert "modpdm.c" in pdm_cmake
    assert "__idf_esp32-camera" in cmake
    assert "__idf_esp_driver_i2s" in pdm_cmake
    assert "firmware.bin" in script
    assert "0x280000" in script
    assert "Length" in script
