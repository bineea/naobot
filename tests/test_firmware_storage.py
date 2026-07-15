import sys
from pathlib import Path

import pytest

FIRMWARE_ROOT = Path(__file__).resolve().parents[1] / "firmware" / "esp32"
if str(FIRMWARE_ROOT) not in sys.path:
    sys.path.insert(0, str(FIRMWARE_ROOT))

class FakeSDCard:
    calls = []

    def __init__(self, **kwargs):
        type(self).calls.append(kwargs)


def create_storage(tmp_path, **kwargs):
    from storage.sd_storage import SDStorage

    FakeSDCard.calls = []
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

    assert storage.ensure_mounted() is True
    log_path.write_bytes(b"x" * 32)
    archive_one.write_text("newer archive")
    archive_two.write_text("oldest archive")

    assert storage.append_log({"event": "rotated"}) is True
    assert archive_one.read_bytes() == b"x" * 32
    assert archive_two.read_text() == "newer archive"
    assert log_path.read_text() == '{"event":"rotated"}\n'


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
    worker = storage_worker_class()(create_storage(tmp_path), queue_limit=1)

    assert worker.submit_log({"event": "first"}) is True
    assert worker.submit_log({"event": "second"}) is False
    rejected = worker.submit_update_read("20260715", "firmware.bin")

    assert rejected == {"accepted": False, "reason": "storage queue full"}
    assert worker.snapshot()["queue_depth"] == 1
    assert worker.snapshot()["dropped"] == 1
    assert worker.tick() is True
    assert worker.snapshot()["queue_depth"] == 0


def test_worker_processes_bounded_update_reads_cooperatively(tmp_path):
    storage = create_storage(tmp_path, read_chunk_bytes=3)
    worker = storage_worker_class()(storage, queue_limit=2)
    update_dir = tmp_path / "sd" / "updates" / "20260715"

    assert storage.ensure_mounted() is True
    update_dir.mkdir(parents=True)
    (update_dir / "firmware.bin").write_bytes(b"abcdef")

    request = worker.submit_update_read("20260715", "firmware.bin")

    assert request == {"accepted": True, "result": None, "error": None}
    assert worker.tick() is True
    assert request == {"accepted": True, "result": b"abc", "error": None}


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
