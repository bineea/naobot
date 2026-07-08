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


class FakeOled:
    def __init__(self):
        self.calls = []

    def fill(self, color):
        self.calls.append(("fill", color))

    def text(self, text, x, y, color=1):
        self.calls.append(("text", text, x, y, color))

    def show(self):
        self.calls.append(("show",))

    def pixel(self, x, y, color):
        self.calls.append(("pixel", x, y, color))

    def hline(self, x, y, width, color):
        self.calls.append(("hline", x, y, width, color))

    def vline(self, x, y, height, color):
        self.calls.append(("vline", x, y, height, color))

    def line(self, x1, y1, x2, y2, color):
        self.calls.append(("line", x1, y1, x2, y2, color))

    def rect(self, x, y, width, height, color):
        self.calls.append(("rect", x, y, width, height, color))

    def fill_rect(self, x, y, width, height, color):
        self.calls.append(("fill_rect", x, y, width, height, color))


def test_display_falls_back_without_oled_driver() -> None:
    display = Display()
    display.set_face("happy")
    display.show_status("READY")

    assert display.available is False
    assert display.face == "happy"
    assert display.last_status == "READY"


def count_calls(oled: FakeOled, name: str) -> int:
    return sum(1 for call in oled.calls if call[0] == name)


def test_display_draws_eye_faces_on_oled() -> None:
    oled = FakeOled()
    display = Display(oled=oled)

    for face in ("idle", "happy", "sad", "dizzy", "sleepy", "alert"):
        display.set_face(face)

    drawing_calls = [call for call in oled.calls if call[0] in {"fill_rect", "rect", "line", "hline", "vline"}]
    assert len(drawing_calls) > 20
    assert any(call[0] == "fill_rect" for call in oled.calls)
    assert oled.calls[-1] == ("show",)


def test_display_idle_uses_round_eye_drawing() -> None:
    oled = FakeOled()
    display = Display(oled=oled)

    display.set_face("idle")

    assert count_calls(oled, "hline") >= 120
    assert count_calls(oled, "pixel") >= 80


def test_display_idle_uses_larger_black_pupils() -> None:
    oled = FakeOled()
    display = Display(oled=oled)

    display.set_face("idle")

    black_hlines = [call for call in oled.calls if call[0] == "hline" and call[4] == 0]
    assert max(call[3] for call in black_hlines) >= 23


def test_display_idle_animation_uses_multiple_eye_frames() -> None:
    oled = FakeOled()
    display = Display(oled=oled)
    before = count_calls(oled, "show")

    display.set_face("idle")

    assert count_calls(oled, "show") - before == 4
    assert not any(call[0] == "text" for call in oled.calls)


def test_display_animated_faces_use_short_frame_sequences() -> None:
    expected_frames = {"happy": 3, "alert": 4, "sleepy": 4}

    for face, frame_count in expected_frames.items():
        oled = FakeOled()
        display = Display(oled=oled)
        before = count_calls(oled, "show")

        display.set_face(face)

        assert count_calls(oled, "show") - before == frame_count
        assert display.face == face


def test_display_blink_restores_current_eye_face() -> None:
    oled = FakeOled()
    display = Display(oled=oled)
    display.set_face("happy")
    before = count_calls(oled, "show")

    display.blink()

    assert display.face == "happy"
    assert count_calls(oled, "show") - before == 2


def test_display_status_uses_bottom_text_only_when_requested() -> None:
    oled = FakeOled()
    display = Display(oled=oled)

    display.set_face("idle")
    assert not any(call[0] == "text" for call in oled.calls)

    display.show_status("agent online")

    assert any(call[0] == "text" and call[3] == 56 for call in oled.calls)


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
