from __future__ import annotations

import importlib
import sys
from csv import reader
from pathlib import Path

FIRMWARE_ROOT = Path(__file__).resolve().parents[1] / "firmware" / "esp32"
BUILD_ROOT = FIRMWARE_ROOT / "build"
if str(FIRMWARE_ROOT) not in sys.path:
    sys.path.insert(0, str(FIRMWARE_ROOT))


def read_xiao_partitions() -> list[list[str]]:
    source = (BUILD_ROOT / "XIAO_ESP32S3_SENSE" / "partitions.csv").read_text(
        encoding="utf-8"
    )
    return [
        [field.strip() for field in row]
        for row in reader(line for line in source.splitlines() if line and not line.startswith("#"))
    ]


def test_xiao_esp32s3_sense_profile_has_the_fixed_wiring() -> None:
    board = importlib.import_module("boards.xiao_esp32s3_sense")

    assert board.BOARD_NAME == "Seeed XIAO ESP32S3 Sense"
    assert board.FLASH_MB == 8
    assert board.PSRAM_MB == 8
    assert board.CAMERA_PINS == {
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
    assert board.PDM_MIC_PINS == {"clk": 42, "data": 41}
    assert board.SD_PINS == {"cs": 3, "sck": 7, "miso": 8, "mosi": 9}
    assert board.EXTERNAL_I2C_PINS == {"sda": 5, "scl": 6}
    assert board.PCA9685_OE_PIN == 1
    assert board.MAX98357A_PINS == {"bclk": 43, "lrc": 44, "din": 4}
    assert board.NATIVE_USB_ENABLED is True
    assert board.CONSOLE_TRANSPORT == "USB_CDC"
    assert not hasattr(board, "SERVO_PINS")
    assert not hasattr(board, "BUZZER_PIN")
    assert not hasattr(board, "INMP441_PINS")
    assert not hasattr(board, "USB_UART_BRIDGE")


def test_config_uses_xiao_profile_and_shared_peripheral_constants() -> None:
    config = importlib.import_module("config")

    assert config.I2C_SDA == 5
    assert config.I2C_SCL == 6
    assert config.OLED_ADDR == 0x3C
    assert config.PCA9685_ADDR == 0x40
    assert config.BQ27441_ADDR == 0x55
    assert config.MPR121_ADDR == 0x5A
    assert config.MPU6050_ADDR == 0x68
    assert config.BQ25895_ADDR == 0x6A
    assert config.BATTERY_WARN_PCT == 20
    assert config.LOW_BATTERY_PCT == 15
    assert config.BATTERY_CRITICAL_PCT == 8


def test_xiao_build_recipe_pins_versions_memory_usb_and_partitions() -> None:
    script = (BUILD_ROOT / "build.ps1").read_text(encoding="utf-8")
    sdkconfig = (BUILD_ROOT / "sdkconfig.board").read_text(encoding="utf-8")
    board_cmake = (BUILD_ROOT / "XIAO_ESP32S3_SENSE" / "mpconfigboard.cmake").read_text(
        encoding="utf-8"
    )
    variant_cmake = (
        BUILD_ROOT / "XIAO_ESP32S3_SENSE" / "mpconfigvariant_SPIRAM_OCT.cmake"
    ).read_text(encoding="utf-8")

    assert "v1.28.0" in script
    assert "2b0015629f67fd186f980079b2e696ad0bc7343c" in script
    assert "v2.1.6" in script
    assert "2ac69a6f1749694804f5196e63fa1f79800b74bf" in script
    assert '"XIAO_ESP32S3_SENSE"' in script
    assert "build-XIAO_ESP32S3_SENSE-SPIRAM_OCT" in script
    assert "CONFIG_ESPTOOLPY_FLASHSIZE_8MB=y" in sdkconfig
    assert 'CONFIG_ESPTOOLPY_FLASHSIZE="8MB"' in sdkconfig
    assert "CONFIG_SPIRAM_MODE_OCT=y" in sdkconfig
    assert "CONFIG_CAMERA_PSRAM_DMA=y" in sdkconfig
    assert "CONFIG_ESP_CONSOLE_USB_CDC=y" in sdkconfig
    assert "CONFIG_TINYUSB_ENABLED=y" in sdkconfig
    assert "CONFIG_PARTITION_TABLE_CUSTOM=y" in sdkconfig
    assert "CONFIG_PARTITION_TABLE_CUSTOM_FILENAME=\"partitions.csv\"" in sdkconfig
    assert "MICROPY_HW_ENABLE_USBDEV=1" in board_cmake
    assert "MICROPY_HW_ESP_USB_SERIAL_JTAG=0" in board_cmake
    assert "MICROPY_HW_ENABLE_UART_REPL=0" in board_cmake
    assert "boards/sdkconfig.spiram_oct" in variant_cmake
    partition_rows = {row[0]: row for row in read_xiao_partitions()}
    assert partition_rows["nvs"][1:5] == ["data", "nvs", "0x9000", "0x6000"]
    assert partition_rows["otadata"][1:5] == ["data", "ota", "0xF000", "0x2000"]
    assert partition_rows["phy_init"][1:5] == ["data", "phy", "0x11000", "0x1000"]
    assert partition_rows["ota_0"][1:5] == ["app", "ota_0", "0x20000", "0x280000"]
    assert partition_rows["ota_1"][1:5] == ["app", "ota_1", "0x2A0000", "0x280000"]
    assert partition_rows["vfs"][1:5] == ["data", "spiffs", "0x520000", "0x2A0000"]
    assert partition_rows["coredump"][1:5] == ["data", "coredump", "0x7C0000", "0x40000"]


def test_xiao_partitions_are_non_overlapping_aligned_and_fit_flash() -> None:
    intervals = []
    for name, partition_type, _subtype, offset, size, *_flags in read_xiao_partitions():
        start = int(offset, 0)
        end = start + int(size, 0)
        assert start >= 0
        assert end <= 8 * 1024 * 1024
        if partition_type == "app":
            assert start % 0x10000 == 0
        intervals.append((start, end, name))

    intervals.sort()
    for (_start, end, name), (next_start, _next_end, next_name) in zip(
        intervals, intervals[1:], strict=False
    ):
        assert end <= next_start, f"{name} overlaps {next_name}"


def test_camera_recipe_keeps_qvga_jpeg_psram_dma_and_sensor_diagnostics() -> None:
    source = (BUILD_ROOT / "camera_module" / "modcamera.c").read_text(encoding="utf-8")

    assert "FRAMESIZE_QVGA" in source
    assert "PIXFORMAT_JPEG" in source
    assert "CAMERA_FB_IN_PSRAM" in source
    assert "esp_camera_set_psram_mode" in source
    assert "esp_camera_sensor_get" in source
    assert "MP_QSTR_sensor_pid" in source
    assert "MP_QSTR_sensor_name" in source


def test_deprecated_targets_are_absent_from_formal_runtime_code() -> None:
    assert not (FIRMWARE_ROOT / "boards" / "n16r8_44pin.py").exists()
    assert not (BUILD_ROOT / "N16R8_44PIN").exists()

    runtime_sources = [
        path
        for path in FIRMWARE_ROOT.rglob("*")
        if path.suffix in {".c", ".py"} and "__pycache__" not in path.parts
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in runtime_sources)
    for deprecated in ("N16R8", "16MB", "CH343"):
        assert deprecated not in text
