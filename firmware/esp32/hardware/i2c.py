try:
    from machine import I2C, Pin
except (ImportError, RuntimeError):
    I2C = None
    Pin = None

from config import I2C_FREQ, I2C_ID, I2C_SCL, I2C_SDA


class SharedI2C:
    """外部 I2C0 的单例工厂；CPython 与缺失 machine 时返回 None。"""

    _instance = None
    _attempted = False

    @classmethod
    def get(cls):
        if cls._attempted:
            return cls._instance
        cls._attempted = True
        if I2C is None or Pin is None:
            return None
        try:
            cls._instance = I2C(
                I2C_ID,
                sda=Pin(I2C_SDA),
                scl=Pin(I2C_SCL),
                freq=I2C_FREQ,
            )
        except Exception as exc:
            cls._instance = None
            print("shared i2c fallback:", exc)
        return cls._instance
