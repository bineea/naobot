try:
    from machine import I2C, Pin
except ImportError:
    I2C = None
    Pin = None

from config import I2C_FREQ, I2C_ID, I2C_SCL, I2C_SDA, OLED_ADDR, OLED_HEIGHT, OLED_WIDTH

FACE_LINES = {
    "idle": ("KT2", "-_-", "READY"),
    "happy": ("KT2", "^_^", "HAPPY"),
    "sleepy": ("KT2", "-.- zZ", "LOW POWER"),
    "alert": ("KT2", "!_!", "ALERT"),
    "dizzy": ("KT2", "@_@", "CHECK"),
    "sad": ("KT2", "T_T", "SAD"),
}


class Display:
    def __init__(self, i2c=None, oled=None):
        self.face = "idle"
        self.available = False
        self.oled = oled
        self.last_status = ""

        if self.oled:
            self.available = True
            self._safe_render_face(self.face)
            return

        try:
            i2c = i2c or self._create_i2c()
            if not i2c:
                raise RuntimeError("i2c unavailable")
            self.oled = self._create_oled(i2c)
            self.available = True
            self._safe_render_face(self.face)
        except Exception as exc:
            self.oled = None
            self.available = False
            print("display fallback:", exc)

    def _create_i2c(self):
        if not I2C or not Pin:
            return None
        return I2C(I2C_ID, scl=Pin(I2C_SCL), sda=Pin(I2C_SDA), freq=I2C_FREQ)

    def _create_oled(self, i2c):
        try:
            from hardware.ssd1306_i2c import SSD1306_I2C
        except ImportError:
            try:
                from ssd1306 import SSD1306_I2C
            except ImportError as exc:
                raise RuntimeError("ssd1306 driver unavailable") from exc
        return SSD1306_I2C(OLED_WIDTH, OLED_HEIGHT, i2c, addr=OLED_ADDR)

    def set_face(self, face):
        if face not in FACE_LINES:
            face = "idle"
        self.face = face
        self._safe_render_face(face)

    def blink(self):
        self._safe_render_lines(("KT2", "o_o", "BLINK"))
        self.set_face(self.face)

    def show_status(self, line):
        self.last_status = str(line)
        self._safe_render_lines(("KT2", FACE_LINES.get(self.face, FACE_LINES["idle"])[1], self.last_status))

    def _safe_render_face(self, face):
        self._safe_render_lines(FACE_LINES.get(face, FACE_LINES["idle"]))

    def _safe_render_lines(self, lines):
        if not self.oled:
            print("display:", " | ".join(lines))
            return
        try:
            self.oled.fill(0)
            y = 0
            for line in lines:
                self.oled.text(str(line)[:16], 0, y)
                y += 16
            self.oled.show()
        except Exception as exc:
            self.oled = None
            self.available = False
            print("display fallback:", exc)
            print("display:", " | ".join(lines))
