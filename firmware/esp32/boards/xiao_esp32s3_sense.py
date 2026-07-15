BOARD_NAME = "Seeed XIAO ESP32S3 Sense"
FLASH_MB = 8
PSRAM_MB = 8

CAMERA_PINS = {
    "d0": 15,
    "d1": 17,
    "d2": 18,
    "d3": 16,
    "d4": 14,
    "d5": 12,
    "d6": 11,
    "d7": 48,
    "xclk": 10,
    "pclk": 13,
    "vsync": 38,
    "href": 47,
    "sccb_sda": 40,
    "sccb_scl": 39,
}

PDM_MIC_PINS = {"clk": 42, "data": 41}
SD_PINS = {"cs": 3, "sck": 7, "miso": 8, "mosi": 9}
EXTERNAL_I2C_PINS = {"sda": 5, "scl": 6}
PCA9685_OE_PIN = 1
MAX98357A_PINS = {"bclk": 43, "lrc": 44, "din": 4}

NATIVE_USB_ENABLED = True
CONSOLE_TRANSPORT = "USB_CDC"
