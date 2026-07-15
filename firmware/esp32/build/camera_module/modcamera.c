#include <string.h>

#include "esp_camera.h"
#include "esp_heap_caps.h"
#include "py/obj.h"
#include "py/objdict.h"
#include "py/mpthread.h"
#include "py/runtime.h"

static int camera_dict_get_int(mp_obj_dict_t *config, qstr key, int default_value) {
    mp_map_elem_t *element = mp_map_lookup(
        &config->map,
        MP_OBJ_NEW_QSTR(key),
        MP_MAP_LOOKUP
    );
    return element == NULL ? default_value : mp_obj_get_int(element->value);
}

static mp_obj_t camera_init(mp_obj_t config_in) {
    if (!mp_obj_is_type(config_in, &mp_type_dict)) {
        mp_raise_TypeError(MP_ERROR_TEXT("camera config must be a dict"));
    }
    mp_obj_dict_t *values = MP_OBJ_TO_PTR(config_in);
    camera_config_t config;
    memset(&config, 0, sizeof(config));
    bool reuse_sccb_i2c = camera_dict_get_int(values, MP_QSTR_reuse_sccb_i2c, 0);

    config.pin_pwdn = camera_dict_get_int(values, MP_QSTR_pin_pwdn, -1);
    config.pin_reset = camera_dict_get_int(values, MP_QSTR_pin_reset, -1);
    config.pin_xclk = camera_dict_get_int(values, MP_QSTR_pin_xclk, -1);
    config.pin_sccb_sda = reuse_sccb_i2c
        ? -1
        : camera_dict_get_int(values, MP_QSTR_pin_sccb_sda, -1);
    config.pin_sccb_scl = reuse_sccb_i2c
        ? -1
        : camera_dict_get_int(values, MP_QSTR_pin_sccb_scl, -1);
    config.pin_d7 = camera_dict_get_int(values, MP_QSTR_pin_d7, -1);
    config.pin_d6 = camera_dict_get_int(values, MP_QSTR_pin_d6, -1);
    config.pin_d5 = camera_dict_get_int(values, MP_QSTR_pin_d5, -1);
    config.pin_d4 = camera_dict_get_int(values, MP_QSTR_pin_d4, -1);
    config.pin_d3 = camera_dict_get_int(values, MP_QSTR_pin_d3, -1);
    config.pin_d2 = camera_dict_get_int(values, MP_QSTR_pin_d2, -1);
    config.pin_d1 = camera_dict_get_int(values, MP_QSTR_pin_d1, -1);
    config.pin_d0 = camera_dict_get_int(values, MP_QSTR_pin_d0, -1);
    config.pin_vsync = camera_dict_get_int(values, MP_QSTR_pin_vsync, -1);
    config.pin_href = camera_dict_get_int(values, MP_QSTR_pin_href, -1);
    config.pin_pclk = camera_dict_get_int(values, MP_QSTR_pin_pclk, -1);
    config.xclk_freq_hz = camera_dict_get_int(values, MP_QSTR_xclk_freq_hz, 20000000);
    config.ledc_timer = LEDC_TIMER_0;
    config.ledc_channel = LEDC_CHANNEL_0;
    config.pixel_format = camera_dict_get_int(values, MP_QSTR_pixel_format, PIXFORMAT_JPEG);
    config.frame_size = camera_dict_get_int(values, MP_QSTR_frame_size, FRAMESIZE_QVGA);
    config.jpeg_quality = camera_dict_get_int(values, MP_QSTR_jpeg_quality, 12);
    config.fb_count = camera_dict_get_int(values, MP_QSTR_fb_count, 2);
    config.fb_location = camera_dict_get_int(
        values,
        MP_QSTR_fb_location,
        CAMERA_FB_IN_PSRAM
    );
    config.grab_mode = camera_dict_get_int(values, MP_QSTR_grab_mode, CAMERA_GRAB_LATEST);
    config.sccb_i2c_port = camera_dict_get_int(values, MP_QSTR_sccb_i2c_port, -1);

    esp_err_t result = esp_camera_init(&config);
    if (result != ESP_OK) {
        mp_raise_msg_varg(&mp_type_OSError, MP_ERROR_TEXT("camera init failed: 0x%x"), result);
    }
    return mp_const_true;
}
static MP_DEFINE_CONST_FUN_OBJ_1(camera_init_obj, camera_init);

static mp_obj_t camera_deinit(void) {
    return mp_obj_new_bool(esp_camera_deinit() == ESP_OK);
}
static MP_DEFINE_CONST_FUN_OBJ_0(camera_deinit_obj, camera_deinit);

static mp_obj_t camera_capture(void) {
    MP_THREAD_GIL_EXIT();
    camera_fb_t *frame = esp_camera_fb_get();
    MP_THREAD_GIL_ENTER();
    if (frame == NULL) {
        return mp_const_none;
    }
    mp_obj_t payload = mp_obj_new_bytes(frame->buf, frame->len);
    esp_camera_fb_return(frame);
    return payload;
}
static MP_DEFINE_CONST_FUN_OBJ_0(camera_capture_obj, camera_capture);

static mp_obj_t camera_available_frames(void) {
    return mp_obj_new_bool(esp_camera_available_frames());
}
static MP_DEFINE_CONST_FUN_OBJ_0(camera_available_frames_obj, camera_available_frames);

static mp_obj_t camera_set_psram_dma(mp_obj_t enabled_in) {
    bool enabled = mp_obj_is_true(enabled_in);
    return mp_obj_new_bool(esp_camera_set_psram_mode(enabled) == ESP_OK);
}
static MP_DEFINE_CONST_FUN_OBJ_1(camera_set_psram_dma_obj, camera_set_psram_dma);

static mp_obj_t camera_psram_free(void) {
    return mp_obj_new_int_from_uint(heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
}
static MP_DEFINE_CONST_FUN_OBJ_0(camera_psram_free_obj, camera_psram_free);

static mp_obj_t camera_sensor_pid(void) {
    sensor_t *sensor = esp_camera_sensor_get();
    if (sensor == NULL) {
        return mp_const_none;
    }
    return mp_obj_new_int(sensor->id.PID);
}
static MP_DEFINE_CONST_FUN_OBJ_0(camera_sensor_pid_obj, camera_sensor_pid);

static const char *camera_sensor_name_from_pid(uint16_t pid) {
    switch (pid) {
        case OV2640_PID:
            return "OV2640";
        case OV3660_PID:
            return "OV3660";
        case OV5640_PID:
            return "OV5640";
        default:
            return "unknown";
    }
}

static mp_obj_t camera_sensor_name(void) {
    sensor_t *sensor = esp_camera_sensor_get();
    if (sensor == NULL) {
        return mp_const_none;
    }
    const char *name = camera_sensor_name_from_pid(sensor->id.PID);
    return mp_obj_new_str(name, strlen(name));
}
static MP_DEFINE_CONST_FUN_OBJ_0(camera_sensor_name_obj, camera_sensor_name);

static const mp_rom_map_elem_t camera_module_globals_table[] = {
    {MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_camera)},
    {MP_ROM_QSTR(MP_QSTR_init), MP_ROM_PTR(&camera_init_obj)},
    {MP_ROM_QSTR(MP_QSTR_deinit), MP_ROM_PTR(&camera_deinit_obj)},
    {MP_ROM_QSTR(MP_QSTR_capture), MP_ROM_PTR(&camera_capture_obj)},
    {MP_ROM_QSTR(MP_QSTR_available_frames), MP_ROM_PTR(&camera_available_frames_obj)},
    {MP_ROM_QSTR(MP_QSTR_set_psram_dma), MP_ROM_PTR(&camera_set_psram_dma_obj)},
    {MP_ROM_QSTR(MP_QSTR_psram_free), MP_ROM_PTR(&camera_psram_free_obj)},
    {MP_ROM_QSTR(MP_QSTR_sensor_pid), MP_ROM_PTR(&camera_sensor_pid_obj)},
    {MP_ROM_QSTR(MP_QSTR_sensor_name), MP_ROM_PTR(&camera_sensor_name_obj)},
    {MP_ROM_QSTR(MP_QSTR_FRAME_QVGA), MP_ROM_INT(FRAMESIZE_QVGA)},
    {MP_ROM_QSTR(MP_QSTR_PIXFORMAT_JPEG), MP_ROM_INT(PIXFORMAT_JPEG)},
    {MP_ROM_QSTR(MP_QSTR_CAMERA_FB_IN_PSRAM), MP_ROM_INT(CAMERA_FB_IN_PSRAM)},
    {MP_ROM_QSTR(MP_QSTR_CAMERA_GRAB_LATEST), MP_ROM_INT(CAMERA_GRAB_LATEST)},
};
static MP_DEFINE_CONST_DICT(camera_module_globals, camera_module_globals_table);

const mp_obj_module_t camera_user_cmodule = {
    .base = {&mp_type_module},
    .globals = (mp_obj_dict_t *)&camera_module_globals,
};

MP_REGISTER_MODULE(MP_QSTR_camera, camera_user_cmodule);
