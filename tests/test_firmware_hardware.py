import sys
from pathlib import Path

FIRMWARE_ROOT = Path(__file__).resolve().parents[1] / "firmware" / "esp32"
if str(FIRMWARE_ROOT) not in sys.path:
    sys.path.insert(0, str(FIRMWARE_ROOT))

from hardware.display import Display  # noqa: E402
from hardware.imu import IMU  # noqa: E402


def encode_word(value: int) -> bytes:
    if value < 0:
        value += 65536
    return bytes([(value >> 8) & 0xFF, value & 0xFF])


class FakeI2C:
    def __init__(self, devices=None, ax=0, ay=0, az=16384, gx=0, gy=0, gz=0, temp=0):
        self.devices = [0x68] if devices is None else devices
        self.writes = []
        self.values = {
            0x3B: ax,
            0x3D: ay,
            0x3F: az,
            0x41: temp,
            0x43: gx,
            0x45: gy,
            0x47: gz,
        }

    def scan(self):
        return self.devices

    def writeto_mem(self, addr, reg, data):
        self.writes.append((addr, reg, data))

    def readfrom_mem(self, addr, reg, length):
        assert length == 2
        return encode_word(self.values.get(reg, 0))


def test_display_falls_back_without_oled_driver() -> None:
    display = Display()
    display.set_face("happy")
    display.show_status("READY")

    assert display.available is False
    assert display.face == "happy"
    assert display.last_status == "READY"


def test_imu_reads_upright_posture() -> None:
    imu = IMU(i2c=FakeI2C(az=16384), calibrate=False)

    assert imu.available is True
    assert imu.read_posture() == "upright"
    assert imu.is_fault() is False


def test_imu_detects_fallen_posture() -> None:
    imu = IMU(i2c=FakeI2C(ax=16384, az=0), calibrate=False)

    assert imu.read_posture() == "fallen"
    assert imu.is_fault() is True


def test_imu_missing_device_is_unknown_and_fault() -> None:
    imu = IMU(i2c=FakeI2C(devices=[]), calibrate=False)

    assert imu.available is False
    assert imu.posture == "unknown"
    assert imu.is_fault() is True
