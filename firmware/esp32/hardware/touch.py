from config import MPR121_ADDR
from hardware.i2c import SharedI2C


class TouchInputs:
    def __init__(self, i2c=None, touch_threshold=12, release_threshold=6, debounce_samples=2):
        self.i2c = i2c if i2c is not None else SharedI2C.get()
        self.available = False
        self.touch_threshold = touch_threshold
        self.release_threshold = release_threshold
        self.debounce_samples = max(2, int(debounce_samples))
        self._stable_mask = 0
        self._candidate_mask = 0
        self._candidate_count = 0
        self._pending = []
        try:
            if self.i2c is None:
                raise RuntimeError("i2c unavailable")
            if hasattr(self.i2c, "scan") and MPR121_ADDR not in self.i2c.scan():
                raise RuntimeError("mpr121 not found")
            self._configure()
            self.available = True
        except Exception as exc:
            print("touch fallback:", exc)

    def _write(self, register, value):
        self.i2c.writeto_mem(MPR121_ADDR, register, bytes((value,)))

    def _configure(self):
        self._write(0x5E, 0x00)
        self._write(0x80, 0x63)
        for electrode in (0, 1):
            self._write(0x41 + electrode * 2, self.touch_threshold)
            self._write(0x42 + electrode * 2, self.release_threshold)
        self._write(0x5E, 0x82)

    def _read_mask(self):
        data = self.i2c.readfrom_mem(MPR121_ADDR, 0x00, 2)
        return ((data[1] << 8) | data[0]) & 0x03

    def poll(self):
        if self._pending:
            return self._pending.pop(0)
        if not self.available:
            return None
        try:
            mask = self._read_mask()
        except Exception as exc:
            self.available = False
            print("touch read failed:", exc)
            return None
        if mask == self._stable_mask:
            self._candidate_mask = mask
            self._candidate_count = 0
            return None
        if mask != self._candidate_mask:
            self._candidate_mask = mask
            self._candidate_count = 1
            return None
        self._candidate_count += 1
        if self._candidate_count < self.debounce_samples:
            return None
        rising = mask & ~self._stable_mask
        self._stable_mask = mask
        self._candidate_count = 0
        if rising & 0x01:
            self._pending.append("touch_head")
        if rising & 0x02:
            self._pending.append("touch_back")
        return self._pending.pop(0) if self._pending else None
