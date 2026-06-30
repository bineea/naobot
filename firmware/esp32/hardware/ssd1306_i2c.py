try:
    import framebuf
except ImportError as exc:
    raise ImportError("framebuf unavailable") from exc


SET_CONTRAST = 0x81
SET_ENTIRE_ON = 0xA4
SET_NORM_INV = 0xA6
SET_DISP = 0xAE
SET_MEM_ADDR = 0x20
SET_COL_ADDR = 0x21
SET_PAGE_ADDR = 0x22
SET_DISP_START_LINE = 0x40
SET_SEG_REMAP = 0xA0
SET_MUX_RATIO = 0xA8
SET_COM_OUT_DIR = 0xC0
SET_DISP_OFFSET = 0xD3
SET_COM_PIN_CFG = 0xDA
SET_DISP_CLK_DIV = 0xD5
SET_PRECHARGE = 0xD9
SET_VCOM_DESEL = 0xDB
SET_CHARGE_PUMP = 0x8D


class SSD1306_I2C:
    def __init__(self, width, height, i2c, addr=0x3C):
        self.width = width
        self.height = height
        self.i2c = i2c
        self.addr = addr
        self.pages = self.height // 8
        self.buffer = bytearray(self.pages * self.width)
        self.framebuf = framebuf.FrameBuffer(self.buffer, self.width, self.height, framebuf.MONO_VLSB)
        self._init_display()

    def _write_cmd(self, cmd):
        self.i2c.writeto(self.addr, bytearray([0x80, cmd]))

    def _write_data(self, data):
        self.i2c.writeto(self.addr, b"\x40" + data)

    def _init_display(self):
        for cmd in (
            SET_DISP,
            SET_MEM_ADDR,
            0x00,
            SET_DISP_START_LINE,
            SET_SEG_REMAP | 0x01,
            SET_MUX_RATIO,
            self.height - 1,
            SET_COM_OUT_DIR | 0x08,
            SET_DISP_OFFSET,
            0x00,
            SET_COM_PIN_CFG,
            0x12 if self.height == 64 else 0x02,
            SET_DISP_CLK_DIV,
            0x80,
            SET_PRECHARGE,
            0xF1,
            SET_VCOM_DESEL,
            0x30,
            SET_CONTRAST,
            0xFF,
            SET_ENTIRE_ON,
            SET_NORM_INV,
            SET_CHARGE_PUMP,
            0x14,
            SET_DISP | 0x01,
        ):
            self._write_cmd(cmd)
        self.fill(0)
        self.show()

    def fill(self, color):
        self.framebuf.fill(color)

    def text(self, text, x, y, color=1):
        self.framebuf.text(text, x, y, color)

    def show(self):
        self._write_cmd(SET_COL_ADDR)
        self._write_cmd(0)
        self._write_cmd(self.width - 1)
        self._write_cmd(SET_PAGE_ADDR)
        self._write_cmd(0)
        self._write_cmd(self.pages - 1)
        self._write_data(self.buffer)

