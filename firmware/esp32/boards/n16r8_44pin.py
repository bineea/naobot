BOARD_NAME = "ESP32-S3-N16R8-44PIN"
FLASH_MB = 16
PSRAM_MB = 8

CAMERA_PINS = {
    "d0": 4,
    "d1": 5,
    "d2": 10,
    "d3": 11,
    "d4": 12,
    "d5": 13,
    "d6": 14,
    "d7": 18,
    "xclk": 21,
    "pclk": 38,
    "vsync": 39,
    "href": 40,
    "sccb_sda": 8,
    "sccb_scl": 9,
}

INMP441_PINS = {"sck": 41, "ws": 42, "sd": 47}
MAX98357A_PINS = {"bclk": 19, "lrc": 20, "din": 45}
TOUCH_PINS = {"head": 1, "back": 2}
SERVO_PINS = {"lf": 6, "rf": 7, "lr": 15, "rr": 16}
I2C_PINS = {"sda": 8, "scl": 9}
BUZZER_PIN = 17

# GPIO8/9 由 OV2640 SCCB、OLED 和 MPU6050 共用，设备访问必须串行化。
SHARED_CAMERA_I2C = True
AVOID_PINS = (35, 36, 37, 48)
USB_UART_BRIDGE = "CH343"
NATIVE_USB_ENABLED = False
CONSOLE_TRANSPORT = "CH343_UART"


def used_pins():
    pins = []
    for mapping in (
        CAMERA_PINS,
        INMP441_PINS,
        MAX98357A_PINS,
        TOUCH_PINS,
        SERVO_PINS,
        I2C_PINS,
    ):
        pins.extend(mapping.values())
    pins.append(BUZZER_PIN)
    return tuple(pins)
