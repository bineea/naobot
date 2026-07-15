from boards.xiao_esp32s3_sense import CAMERA_PINS, MAX98357A_PINS, PDM_MIC_PINS

PCM_SAMPLE_RATE_HZ = 16000
PCM_CHUNK_BYTES = 640
I2S_BUFFER_BYTES = 8000
MAX_TRANSIENT_ERRORS = 3


def _load_camera_module():
    try:
        import camera

        return camera
    except (ImportError, RuntimeError):
        return None


def _load_i2s():
    try:
        from machine import I2S, Pin

        return I2S, Pin
    except (ImportError, RuntimeError):
        return None, None


def _load_pdm_module():
    try:
        import pdm

        return pdm
    except (ImportError, RuntimeError):
        return None


class Camera:
    def __init__(self, camera_module=None):
        self.module = camera_module if camera_module is not None else _load_camera_module()
        self.available = False
        self.last_error = None
        self._closed = False
        self._error_count = 0
        if self.module is None:
            return
        try:
            config = {
                "pin_d0": CAMERA_PINS["d0"],
                "pin_d1": CAMERA_PINS["d1"],
                "pin_d2": CAMERA_PINS["d2"],
                "pin_d3": CAMERA_PINS["d3"],
                "pin_d4": CAMERA_PINS["d4"],
                "pin_d5": CAMERA_PINS["d5"],
                "pin_d6": CAMERA_PINS["d6"],
                "pin_d7": CAMERA_PINS["d7"],
                "pin_xclk": CAMERA_PINS["xclk"],
                "pin_pclk": CAMERA_PINS["pclk"],
                "pin_vsync": CAMERA_PINS["vsync"],
                "pin_href": CAMERA_PINS["href"],
                "pin_sccb_sda": CAMERA_PINS["sccb_sda"],
                "pin_sccb_scl": CAMERA_PINS["sccb_scl"],
                "sccb_i2c_port": 1,
                "reuse_sccb_i2c": False,
                "pin_pwdn": -1,
                "pin_reset": -1,
                "xclk_freq_hz": 20000000,
                "frame_size": self.module.FRAME_QVGA,
                "pixel_format": self.module.PIXFORMAT_JPEG,
                "jpeg_quality": 12,
                "fb_count": 2,
                "fb_location": self.module.CAMERA_FB_IN_PSRAM,
                "grab_mode": self.module.CAMERA_GRAB_LATEST,
            }
            initialized = self.module.init(config)
            dma_enabled = self.module.set_psram_dma(True)
            self.available = initialized is not False and dma_enabled is not False
        except Exception as exc:
            self.last_error = exc
            self.available = False

    def capture(self):
        if not self.available:
            return None
        try:
            if hasattr(self.module, "available_frames") and not self.module.available_frames():
                return None
            payload = self.module.capture()
            self._error_count = 0
            return bytes(payload) if payload else None
        except Exception as exc:
            self.last_error = exc
            self._error_count += 1
            if self._error_count >= MAX_TRANSIENT_ERRORS:
                self.available = False
            return None

    def psram_free(self):
        if self.module is None or not hasattr(self.module, "psram_free"):
            return 0
        try:
            return int(self.module.psram_free())
        except Exception:
            return 0

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.available = False
        if self.module is not None and hasattr(self.module, "deinit"):
            try:
                self.module.deinit()
            except Exception as exc:
                self.last_error = exc


class AudioInput:
    def __init__(self, pdm_module=None, chunk_bytes=PCM_CHUNK_BYTES):
        self.pdm = pdm_module if pdm_module is not None else _load_pdm_module()
        self.available = False
        self.chunk_bytes = chunk_bytes
        self.last_error = None
        self._error_count = 0
        if self.pdm is None:
            return
        try:
            if not hasattr(self.pdm, "read"):
                raise RuntimeError("PDM non-blocking read unavailable")
            initialized = self.pdm.init(
                clk_pin=PDM_MIC_PINS["clk"],
                data_pin=PDM_MIC_PINS["data"],
                sample_rate_hz=PCM_SAMPLE_RATE_HZ,
                bits=16,
                channels=1,
                buffer_bytes=I2S_BUFFER_BYTES,
            )
            self.available = initialized is not False
        except Exception as exc:
            self.last_error = exc
            self._deinit()

    def read_chunk(self):
        if not self.available or self.pdm is None:
            return None
        try:
            payload = self.pdm.read(self.chunk_bytes)
            if not payload:
                return None
            self._error_count = 0
            return bytes(payload)
        except Exception as exc:
            self.last_error = exc
            self._error_count += 1
            if self._error_count >= MAX_TRANSIENT_ERRORS:
                self.available = False
            return None

    def close(self):
        self.available = False
        self._deinit()

    def _deinit(self):
        if self.pdm is None:
            return
        try:
            if hasattr(self.pdm, "deinit"):
                self.pdm.deinit()
        except Exception as exc:
            self.last_error = exc
        self.pdm = None


class AudioOutput:
    def __init__(self, i2s_class=None, pin_factory=None):
        if i2s_class is None or pin_factory is None:
            loaded_i2s, loaded_pin = _load_i2s()
            i2s_class = i2s_class or loaded_i2s
            pin_factory = pin_factory or loaded_pin
        self.i2s = None
        self.available = False
        self._ready = False
        self.last_error = None
        self._error_count = 0
        if i2s_class is None or pin_factory is None:
            return
        try:
            self.i2s = i2s_class(
                1,
                sck=pin_factory(MAX98357A_PINS["bclk"]),
                ws=pin_factory(MAX98357A_PINS["lrc"]),
                sd=pin_factory(MAX98357A_PINS["din"]),
                mode=i2s_class.TX,
                bits=i2s_class.B16,
                format=i2s_class.MONO,
                rate=PCM_SAMPLE_RATE_HZ,
                ibuf=I2S_BUFFER_BYTES,
            )
            if not hasattr(self.i2s, "irq"):
                raise RuntimeError("I2S non-blocking readiness unavailable")
            self.i2s.irq(self._mark_ready)
            self.available = True
        except Exception as exc:
            self.last_error = exc
            self._deinit()

    def write(self, payload):
        if not self.available or self.i2s is None or not self._ready:
            return 0
        self._ready = False
        try:
            count = self.i2s.write(payload) or 0
            if count:
                self._error_count = 0
            return count
        except Exception as exc:
            self.last_error = exc
            self._error_count += 1
            if self._error_count >= MAX_TRANSIENT_ERRORS:
                self.available = False
            return 0

    def _mark_ready(self, _i2s):
        self._ready = True

    def close(self):
        self.available = False
        self._ready = False
        self._deinit()

    def _deinit(self):
        if self.i2s is None:
            return
        try:
            if hasattr(self.i2s, "irq"):
                self.i2s.irq(None)
            if hasattr(self.i2s, "deinit"):
                self.i2s.deinit()
        except Exception as exc:
            self.last_error = exc
        self.i2s = None
