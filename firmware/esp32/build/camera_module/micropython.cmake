add_library(usermod_camera INTERFACE)

target_sources(usermod_camera INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}/modcamera.c
)

# ESP-IDF component target 负责传递 esp_camera.h 及转换头文件路径。
target_link_libraries(usermod_camera INTERFACE __idf_esp32-camera)
target_link_libraries(usermod INTERFACE usermod_camera)

include(${CMAKE_CURRENT_LIST_DIR}/../pdm_module/micropython.cmake)
include(${CMAKE_CURRENT_LIST_DIR}/../ota_module/micropython.cmake)
