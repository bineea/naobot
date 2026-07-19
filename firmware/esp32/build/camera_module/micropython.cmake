add_library(usermod_camera INTERFACE)

target_sources(usermod_camera INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}/modcamera.c
)

# MicroPython 的 usermod QSTR 扫描只读取接口 include，需显式暴露 Camera 公共头文件。
target_include_directories(usermod_camera INTERFACE
    ${MICROPY_PORT_DIR}/components/esp32-camera/driver/include
    ${MICROPY_PORT_DIR}/components/esp32-camera/conversions/include
    ${MICROPY_PORT_DIR}/managed_components/espressif__esp_jpeg/include
)

target_link_libraries(usermod_camera INTERFACE __idf_esp32-camera)
target_link_libraries(usermod INTERFACE usermod_camera)

include(${CMAKE_CURRENT_LIST_DIR}/../pdm_module/micropython.cmake)
include(${CMAKE_CURRENT_LIST_DIR}/../ota_module/micropython.cmake)
