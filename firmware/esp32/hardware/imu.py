try:
    from machine import I2C, Pin
except ImportError:
    I2C = None
    Pin = None

try:
    import utime as time
except ImportError:
    import time

from config import (
    I2C_FREQ,
    I2C_ID,
    I2C_SCL,
    I2C_SDA,
    MPU6050_ADDR,
    MPU6050_CALIBRATION_SAMPLES,
    POSTURE_FALLEN_AXIS_MIN,
    POSTURE_FALLEN_Z_MAX,
    POSTURE_UPRIGHT_Z_MIN,
)

PWR_MGMT_1 = 0x6B
CONFIG = 0x1A
GYRO_CONFIG = 0x1B
ACCEL_CONFIG = 0x1C
ACCEL_XOUT_H = 0x3B
TEMP_OUT_H = 0x41
GYRO_XOUT_H = 0x43


class IMU:
    def __init__(self, i2c=None, calibrate=True):
        self.available = False
        self.posture = "unknown"
        self.i2c = i2c
        self.offsets = {
            "ax": 0,
            "ay": 0,
            "az": 0,
            "gx": 0,
            "gy": 0,
            "gz": 0,
        }

        try:
            self.i2c = self.i2c or self._create_i2c()
            if not self.i2c:
                raise RuntimeError("i2c unavailable")
            if not self._device_present():
                raise RuntimeError("mpu6050 not found")
            self._init_device()
            if calibrate:
                self.calibrate(MPU6050_CALIBRATION_SAMPLES)
            self.available = True
            self.posture = self.read_posture()
        except Exception as exc:
            self.available = False
            self.posture = "unknown"
            print("imu fallback:", exc)

    def _create_i2c(self):
        if not I2C or not Pin:
            return None
        return I2C(I2C_ID, scl=Pin(I2C_SCL), sda=Pin(I2C_SDA), freq=I2C_FREQ)

    def _device_present(self):
        if not hasattr(self.i2c, "scan"):
            return True
        return MPU6050_ADDR in self.i2c.scan()

    def _write_reg(self, reg, value):
        self.i2c.writeto_mem(MPU6050_ADDR, reg, bytes([value]))

    def _read_word(self, reg):
        data = self.i2c.readfrom_mem(MPU6050_ADDR, reg, 2)
        value = (data[0] << 8) | data[1]
        if value >= 0x8000:
            value -= 65536
        return value

    def _init_device(self):
        self._write_reg(PWR_MGMT_1, 0x00)
        self._write_reg(CONFIG, 0x03)
        self._write_reg(ACCEL_CONFIG, 0x00)
        self._write_reg(GYRO_CONFIG, 0x00)
        time.sleep(0.05)

    def _read_raw(self):
        return {
            "ax": self._read_word(ACCEL_XOUT_H),
            "ay": self._read_word(ACCEL_XOUT_H + 2),
            "az": self._read_word(ACCEL_XOUT_H + 4),
            "temp": self._read_word(TEMP_OUT_H),
            "gx": self._read_word(GYRO_XOUT_H),
            "gy": self._read_word(GYRO_XOUT_H + 2),
            "gz": self._read_word(GYRO_XOUT_H + 4),
        }

    def calibrate(self, samples):
        if samples <= 0:
            return
        sums = {"ax": 0, "ay": 0, "az": 0, "gx": 0, "gy": 0, "gz": 0}
        for _ in range(samples):
            raw = self._read_raw()
            for key in sums:
                sums[key] += raw[key]
            time.sleep(0.005)

        self.offsets["ax"] = sums["ax"] / samples
        self.offsets["ay"] = sums["ay"] / samples
        self.offsets["az"] = (sums["az"] / samples) - 16384
        self.offsets["gx"] = sums["gx"] / samples
        self.offsets["gy"] = sums["gy"] / samples
        self.offsets["gz"] = sums["gz"] / samples

    def read_motion(self):
        if not self.available or not self.i2c:
            self.available = False
            return None
        try:
            raw = self._read_raw()
            ax_raw = raw["ax"] - self.offsets["ax"]
            ay_raw = raw["ay"] - self.offsets["ay"]
            az_raw = raw["az"] - self.offsets["az"]
            gx_raw = raw["gx"] - self.offsets["gx"]
            gy_raw = raw["gy"] - self.offsets["gy"]
            gz_raw = raw["gz"] - self.offsets["gz"]
            return {
                "ax": ax_raw / 16384,
                "ay": ay_raw / 16384,
                "az": az_raw / 16384,
                "gx": gx_raw / 131,
                "gy": gy_raw / 131,
                "gz": gz_raw / 131,
                "temp": raw["temp"] / 340 + 36.53,
            }
        except Exception as exc:
            self.available = False
            self.posture = "unknown"
            print("imu read failed:", exc)
            return None

    def read_posture(self):
        motion = self.read_motion()
        if not motion:
            self.posture = "unknown"
            return self.posture

        ax = motion["ax"]
        ay = motion["ay"]
        az = motion["az"]
        if az >= POSTURE_UPRIGHT_Z_MIN:
            self.posture = "upright"
        elif abs(az) <= POSTURE_FALLEN_Z_MAX and (
            abs(ax) >= POSTURE_FALLEN_AXIS_MIN or abs(ay) >= POSTURE_FALLEN_AXIS_MIN
        ):
            self.posture = "fallen"
        else:
            self.posture = "unknown"
        return self.posture

    def is_fault(self):
        return self.read_posture() not in ("upright", "sitting")
