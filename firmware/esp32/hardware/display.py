try:
    import utime as time
except ImportError:
    import time

from config import OLED_ADDR, OLED_HEIGHT, OLED_WIDTH
from hardware.i2c import SharedI2C

FACE_NAMES = ("idle", "happy", "sleepy", "alert", "dizzy", "sad")
# 动画 face 的帧序列与每帧延时(ms)；非动画 face(dizzy/sad 等)单帧直绘。
# 供 DisplaySkill 按 tick 推进，避免 set_face 经动作队列时阻塞安全循环。
FACE_ANIMATIONS = {
    "idle": (("idle", "idle_left", "idle_right", "idle"), 110),
    "happy": (("happy_open", "happy_half", "happy"), 90),
    "alert": (("alert_left", "alert_right", "alert_left", "alert"), 70),
    "sleepy": (("sleepy_open", "sleepy_half", "sleepy_low", "sleepy"), 110),
}
BLINK_DELAY_MS = 80
EXPRESSION_EMOTIONS = {
    "idle": "idle",
    "happy": "happy",
    "sad": "sad",
    "dizzy": "dizzy",
    "sleepy": "sleepy",
    "alert": "alert",
    "curious": "idle",
    "confused": "dizzy",
    "proud": "happy",
    "shy": "happy",
}
EYE_CENTERS = ((42, 28), (86, 28))


def sleep_ms(ms):
    if hasattr(time, "sleep_ms"):
        time.sleep_ms(ms)
    else:
        time.sleep(ms / 1000)


class Display:
    def __init__(self, i2c=None, oled=None):
        self.face = "idle"
        self.available = False
        self.oled = oled
        self.i2c = i2c
        self.last_status = ""

        if self.oled:
            self.available = True
            self._safe_render_frame(self.face)
            return

        try:
            self.i2c = self.i2c if self.i2c is not None else SharedI2C.get()
            if not self.i2c:
                raise RuntimeError("i2c unavailable")
            self.oled = self._create_oled(self.i2c)
            self.available = True
            self._safe_render_frame(self.face)
        except Exception as exc:
            self.oled = None
            self.available = False
            print("display fallback:", exc)

    def _create_i2c(self):
        return SharedI2C.get()

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
        if face not in FACE_NAMES:
            face = "idle"
        self.face = face
        self._animate(face)

    def set_expression(self, params):
        params = params or {}
        emotion = str(params.get("emotion", "idle"))
        face = EXPRESSION_EMOTIONS.get(emotion, "idle")
        self.face = face
        eye_open = clamp_float(params.get("eye_open", 0.8), 0.0, 1.0)
        offset = int(clamp_float(params.get("pupil_offset_x", 0.0), -1.0, 1.0) * 8)
        arousal = clamp_float(params.get("arousal", 0.3), 0.0, 1.0)
        upper_lid = int((1.0 - eye_open) * 18)
        lower_lid = int((1.0 - eye_open) * 8)
        pupil_size = 10 if arousal > 0.75 else 11
        self._safe_render_expression(face, offset, upper_lid, lower_lid, pupil_size)

    def blink(self):
        current = self.face
        self._safe_render_frame("blink")
        sleep_ms(80)
        self._safe_render_frame(current)
        self.face = current

    def show_status(self, line):
        self.last_status = str(line)
        self._safe_render_frame(self.face, status=self.last_status)

    def render_frame(self, frame, status=None):
        """渲染单帧（供 DisplaySkill.tick 调用，非阻塞）。"""
        self._safe_render_frame(frame, status=status)

    def _animate(self, face):
        entry = FACE_ANIMATIONS.get(face)
        if entry is not None:
            frames, delay_ms = entry
            self._play(frames, delay_ms)
        else:
            self._safe_render_frame(face)

    def _play(self, frames, delay_ms):
        for frame in frames:
            self._safe_render_frame(frame)
            sleep_ms(delay_ms)

    def _safe_render_frame(self, frame, status=None):
        if not self.oled:
            print("display eye:", frame, status or "")
            return
        try:
            self.oled.fill(0)
            self._draw_eye_frame(frame)
            if status:
                self._draw_status(status)
            self.oled.show()
        except Exception as exc:
            self.oled = None
            self.available = False
            print("display fallback:", exc)
            print("display eye:", frame, status or "")

    def _safe_render_expression(self, face, pupil_x, upper_lid, lower_lid, pupil_size):
        if not self.oled:
            print("display expression:", face)
            return
        try:
            self.oled.fill(0)
            if face == "sleepy":
                self._draw_sleepy_lines()
            elif face == "dizzy":
                self._draw_dizzy_pair()
            else:
                self._draw_pair(
                    pupil_x=pupil_x,
                    upper_lid=upper_lid,
                    lower_lid=lower_lid,
                    pupil_size=pupil_size,
                )
            self.oled.show()
        except Exception as exc:
            self.oled = None
            self.available = False
            print("display fallback:", exc)
            print("display expression:", face)

    def _draw_status(self, status):
        text = str(status)[:16]
        x = max(0, (OLED_WIDTH - len(text) * 8) // 2)
        self.oled.text(text, x, 56)

    def _draw_eye_frame(self, frame):
        if frame == "idle_left":
            self._draw_pair(pupil_x=-6)
        elif frame == "idle_right":
            self._draw_pair(pupil_x=6)
        elif frame == "happy_open":
            self._draw_pair(upper_lid=7, lower_lid=4, pupil_y=-1)
        elif frame == "happy_half":
            self._draw_pair(upper_lid=11, lower_lid=7, pupil_y=-2)
        elif frame == "happy":
            self._draw_pair(upper_lid=14, lower_lid=10, pupil_y=-3)
        elif frame == "sad":
            self._draw_pair(pupil_y=5, upper_lid=2, lower_lid=0)
            self._draw_tear(50, 42)
        elif frame == "sleepy_open":
            self._draw_pair(upper_lid=8, lower_lid=3)
        elif frame == "sleepy_half":
            self._draw_pair(upper_lid=13, lower_lid=8)
        elif frame == "sleepy_low":
            self._draw_pair(upper_lid=17, lower_lid=12)
        elif frame == "sleepy":
            self._draw_sleepy_lines()
        elif frame == "alert_left":
            self._draw_pair(pupil_x=-2, eye_dx=-2, eye_radius=19, pupil_size=8)
        elif frame == "alert_right":
            self._draw_pair(pupil_x=2, eye_dx=2, eye_radius=19, pupil_size=8)
        elif frame == "alert":
            self._draw_pair(pupil_x=0, eye_radius=19, pupil_size=8)
        elif frame == "dizzy":
            self._draw_dizzy_pair()
        elif frame == "blink":
            self._draw_closed_lines()
        else:
            self._draw_pair()

    def _draw_pair(
        self,
        pupil_x=0,
        pupil_y=0,
        upper_lid=0,
        lower_lid=0,
        eye_dx=0,
        sparkle=True,
        pupil_size=11,
        eye_radius=18,
    ):
        for cx, cy in EYE_CENTERS:
            self._draw_eye(cx + eye_dx, cy, pupil_x, pupil_y, upper_lid, lower_lid, sparkle, pupil_size, eye_radius)

    def _draw_eye(self, cx, cy, pupil_x, pupil_y, upper_lid, lower_lid, sparkle, pupil_size, eye_radius):
        self._fill_circle(cx, cy, eye_radius, 1)
        self._circle(cx, cy, eye_radius, 1)
        self._circle(cx, cy, eye_radius - 1, 1)

        px = cx + pupil_x
        py = cy + pupil_y
        self._fill_circle(px, py, pupil_size, 0)
        self._circle(px, py, pupil_size, 0)
        if sparkle and pupil_size >= 6:
            self._fill_circle(px - 2, py - 2, 2, 1)

        if upper_lid:
            self.oled.fill_rect(cx - eye_radius - 2, cy - eye_radius - 2, eye_radius * 2 + 4, upper_lid, 0)
        if lower_lid:
            self.oled.fill_rect(cx - eye_radius - 2, cy + eye_radius + 2 - lower_lid, eye_radius * 2 + 4, lower_lid, 0)

    def _draw_sleepy_lines(self):
        for cx, cy in EYE_CENTERS:
            self._closed_arc(cx, cy, 15)
            self.oled.hline(cx - 11, cy + 4, 22, 1)

    def _draw_closed_lines(self):
        for cx, cy in EYE_CENTERS:
            self._closed_arc(cx, cy, 16)
            self.oled.hline(cx - 11, cy + 2, 22, 1)

    def _draw_dizzy_pair(self):
        for cx, cy in EYE_CENTERS:
            self._fill_circle(cx, cy, 18, 1)
            self._circle(cx, cy, 18, 1)
            self._draw_spiral_eye(cx, cy)

    def _draw_spiral_eye(self, cx, cy):
        points = (
            (cx - 8, cy - 6),
            (cx + 7, cy - 6),
            (cx + 7, cy + 6),
            (cx - 5, cy + 6),
            (cx - 5, cy - 2),
            (cx + 3, cy - 2),
            (cx + 3, cy + 2),
            (cx - 1, cy + 2),
        )
        for index in range(len(points) - 1):
            x1, y1 = points[index]
            x2, y2 = points[index + 1]
            self.oled.line(x1, y1, x2, y2, 0)

    def _draw_tear(self, x, y):
        self.oled.pixel(x, y, 1)
        self.oled.fill_rect(x - 1, y + 1, 3, 4, 1)

    def _fill_circle(self, cx, cy, radius, color):
        for dy in range(-radius, radius + 1):
            dx = self._circle_extent(radius, dy)
            self.oled.hline(cx - dx, cy + dy, dx * 2 + 1, color)

    def _circle(self, cx, cy, radius, color):
        x = radius
        y = 0
        err = 1 - x
        while x >= y:
            self._plot_circle_points(cx, cy, x, y, color)
            y += 1
            if err < 0:
                err += 2 * y + 1
            else:
                x -= 1
                err += 2 * (y - x) + 1

    def _plot_circle_points(self, cx, cy, x, y, color):
        self.oled.pixel(cx + x, cy + y, color)
        self.oled.pixel(cx + y, cy + x, color)
        self.oled.pixel(cx - y, cy + x, color)
        self.oled.pixel(cx - x, cy + y, color)
        self.oled.pixel(cx - x, cy - y, color)
        self.oled.pixel(cx - y, cy - x, color)
        self.oled.pixel(cx + y, cy - x, color)
        self.oled.pixel(cx + x, cy - y, color)

    def _circle_extent(self, radius, dy):
        value = radius * radius - dy * dy
        dx = 0
        while (dx + 1) * (dx + 1) <= value:
            dx += 1
        return dx

    def _closed_arc(self, cx, cy, radius):
        for offset in range(-radius, radius + 1):
            y = cy + abs(offset) // 5
            self.oled.pixel(cx + offset, y, 1)
            if -radius + 3 < offset < radius - 3:
                self.oled.pixel(cx + offset, y + 1, 1)


def clamp_float(value, minimum, maximum):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = minimum
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value
