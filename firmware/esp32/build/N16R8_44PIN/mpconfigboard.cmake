set(IDF_TARGET esp32s3)

set(SDKCONFIG_DEFAULTS
    boards/sdkconfig.base
    boards/sdkconfig.ble
    boards/sdkconfig.spiram_sx
    boards/ESP32_GENERIC_S3/sdkconfig.board
    ${MICROPY_BOARD_DIR}/../sdkconfig.board
)

list(APPEND MICROPY_DEF_BOARD
    MICROPY_HW_BOARD_NAME="Naobot ESP32-S3 N16R8 44-pin"
    MICROPY_HW_ENABLE_USBDEV=0
    MICROPY_HW_ESP_USB_SERIAL_JTAG=0
    MICROPY_HW_ENABLE_UART_REPL=1
)
