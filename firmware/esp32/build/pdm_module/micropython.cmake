add_library(usermod_pdm INTERFACE)

target_sources(usermod_pdm INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}/modpdm.c
)

target_link_libraries(usermod_pdm INTERFACE __idf_esp_driver_i2s)
target_link_libraries(usermod INTERFACE usermod_pdm)
