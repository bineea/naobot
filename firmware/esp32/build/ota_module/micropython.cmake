add_library(usermod_ota INTERFACE)

set(NAOBOT_OTA_PUBLIC_KEY_HEADER
    "${CMAKE_CURRENT_LIST_DIR}/ota_public_key_dev.h"
)
if(DEFINED ENV{NAOBOT_OTA_PUBLIC_KEY_HEADER} AND NOT "$ENV{NAOBOT_OTA_PUBLIC_KEY_HEADER}" STREQUAL "")
    set(NAOBOT_OTA_PUBLIC_KEY_HEADER "$ENV{NAOBOT_OTA_PUBLIC_KEY_HEADER}")
endif()
if(NOT EXISTS "${NAOBOT_OTA_PUBLIC_KEY_HEADER}")
    message(FATAL_ERROR "NAOBOT OTA public key header not found: ${NAOBOT_OTA_PUBLIC_KEY_HEADER}")
endif()
set_property(
    DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
    "${NAOBOT_OTA_PUBLIC_KEY_HEADER}"
)

configure_file(
    "${NAOBOT_OTA_PUBLIC_KEY_HEADER}"
    "${CMAKE_CURRENT_BINARY_DIR}/ota_public_key_selected.h"
    COPYONLY
)

target_sources(usermod_ota INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}/modnao_ota.c
)
target_include_directories(usermod_ota INTERFACE
    ${CMAKE_CURRENT_BINARY_DIR}
)
target_link_libraries(usermod INTERFACE usermod_ota)
