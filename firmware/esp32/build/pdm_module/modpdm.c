#include <string.h>

#include "driver/i2s_pdm.h"
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "py/mperrno.h"
#include "py/mpthread.h"
#include "py/obj.h"
#include "py/objdict.h"
#include "py/runtime.h"

#define PDM_SAMPLE_RATE_HZ (16000)
#define PDM_BITS (16)
#define PDM_CHANNELS (1)
#define PDM_DMA_FRAME_NUM (256)

typedef struct {
    i2s_chan_handle_t channel;
    TaskHandle_t creator;
    uint8_t *buffer;
    size_t capacity;
    size_t head;
    size_t queued;
    uint64_t dropped_bytes;
    uint64_t read_bytes;
    uint64_t read_calls;
    uint64_t overruns;
    esp_err_t last_error;
    uint32_t rate_hz;
    bool enabled;
    bool initialized;
} pdm_state_t;

static pdm_state_t pdm_state;

static void pdm_owner_guard(void) {
    if (
        pdm_state.creator != NULL
        && pdm_state.creator != xTaskGetCurrentTaskHandle()
    ) {
        mp_raise_OSError(MP_EPERM);
    }
}

static esp_err_t pdm_release(void) {
    esp_err_t first_error = ESP_OK;
    if (pdm_state.channel != NULL) {
        if (pdm_state.enabled) {
            esp_err_t result = i2s_channel_disable(pdm_state.channel);
            if (result != ESP_OK) {
                first_error = result;
            }
        }
        esp_err_t result = i2s_del_channel(pdm_state.channel);
        if (first_error == ESP_OK && result != ESP_OK) {
            first_error = result;
        }
    }
    if (pdm_state.buffer != NULL) {
        heap_caps_free(pdm_state.buffer);
    }
    pdm_state.channel = NULL;
    pdm_state.buffer = NULL;
    pdm_state.capacity = 0;
    pdm_state.head = 0;
    pdm_state.queued = 0;
    pdm_state.rate_hz = 0;
    pdm_state.enabled = false;
    pdm_state.initialized = false;
    return first_error;
}

static esp_err_t pdm_pump(void) {
    uint8_t overflow_buffer[256];
    size_t free_bytes = pdm_state.capacity - pdm_state.queued;
    bool dropping = free_bytes < 2;
    uint8_t *destination;
    size_t requested;

    if (dropping) {
        destination = overflow_buffer;
        requested = sizeof(overflow_buffer);
    } else {
        size_t tail = (pdm_state.head + pdm_state.queued) % pdm_state.capacity;
        requested = pdm_state.capacity - tail;
        if (requested > free_bytes) {
            requested = free_bytes;
        }
        requested &= ~(size_t)1;
        destination = pdm_state.buffer + tail;
    }

    size_t received = 0;
    esp_err_t result;
    MP_THREAD_GIL_EXIT();
    result = i2s_channel_read(
        pdm_state.channel,
        destination,
        requested,
        &received,
        0
    );
    MP_THREAD_GIL_ENTER();

    if (result == ESP_ERR_TIMEOUT) {
        pdm_state.last_error = ESP_OK;
        return ESP_OK;
    }
    if (result != ESP_OK) {
        pdm_state.last_error = result;
        return result;
    }

    if (received & 1) {
        received--;
        pdm_state.dropped_bytes++;
        pdm_state.overruns++;
    }
    if (dropping) {
        if (received > 0) {
            pdm_state.dropped_bytes += received;
            pdm_state.overruns++;
        }
    } else {
        pdm_state.queued += received;
    }
    pdm_state.last_error = ESP_OK;
    return ESP_OK;
}

enum {
    ARG_clk_pin,
    ARG_data_pin,
    ARG_sample_rate_hz,
    ARG_bits,
    ARG_channels,
    ARG_buffer_bytes,
};

static mp_obj_t pdm_init(size_t n_args, const mp_obj_t *pos_args, mp_map_t *kw_args) {
    static const mp_arg_t allowed_args[] = {
        {MP_QSTR_clk_pin, MP_ARG_REQUIRED | MP_ARG_KW_ONLY | MP_ARG_INT, {.u_int = -1}},
        {MP_QSTR_data_pin, MP_ARG_REQUIRED | MP_ARG_KW_ONLY | MP_ARG_INT, {.u_int = -1}},
        {MP_QSTR_sample_rate_hz, MP_ARG_KW_ONLY | MP_ARG_INT, {.u_int = PDM_SAMPLE_RATE_HZ}},
        {MP_QSTR_bits, MP_ARG_KW_ONLY | MP_ARG_INT, {.u_int = PDM_BITS}},
        {MP_QSTR_channels, MP_ARG_KW_ONLY | MP_ARG_INT, {.u_int = PDM_CHANNELS}},
        {MP_QSTR_buffer_bytes, MP_ARG_KW_ONLY | MP_ARG_INT, {.u_int = 8000}},
    };
    mp_arg_val_t args[MP_ARRAY_SIZE(allowed_args)];

    pdm_owner_guard();
    mp_arg_parse_all(
        n_args,
        pos_args,
        kw_args,
        MP_ARRAY_SIZE(allowed_args),
        allowed_args,
        args
    );
    if (
        args[ARG_sample_rate_hz].u_int != PDM_SAMPLE_RATE_HZ
        || args[ARG_bits].u_int != PDM_BITS
        || args[ARG_channels].u_int != PDM_CHANNELS
    ) {
        mp_raise_ValueError(MP_ERROR_TEXT("pdm requires 16000 Hz, 16-bit mono"));
    }
    mp_int_t buffer_bytes = args[ARG_buffer_bytes].u_int;
    if (buffer_bytes <= 0 || (buffer_bytes & 1)) {
        mp_raise_ValueError(MP_ERROR_TEXT("buffer_bytes must be positive and even"));
    }

    if (pdm_state.creator == NULL) {
        pdm_state.creator = xTaskGetCurrentTaskHandle();
    }
    esp_err_t result = pdm_release();
    if (result != ESP_OK) {
        pdm_state.last_error = result;
        mp_raise_OSError(result);
    }

    pdm_state.dropped_bytes = 0;
    pdm_state.read_bytes = 0;
    pdm_state.read_calls = 0;
    pdm_state.overruns = 0;
    pdm_state.last_error = ESP_OK;
    pdm_state.capacity = (size_t)buffer_bytes;
    pdm_state.buffer = heap_caps_malloc(pdm_state.capacity, MALLOC_CAP_8BIT);
    if (pdm_state.buffer == NULL) {
        pdm_state.capacity = 0;
        pdm_state.last_error = ESP_ERR_NO_MEM;
        mp_raise_OSError(MP_ENOMEM);
    }

    i2s_chan_config_t channel_config = I2S_CHANNEL_DEFAULT_CONFIG(
        I2S_NUM_0,
        I2S_ROLE_MASTER
    );
    channel_config.dma_frame_num = PDM_DMA_FRAME_NUM;
    channel_config.dma_desc_num = (buffer_bytes + 1023) / 1024;
    if (channel_config.dma_desc_num < 2) {
        channel_config.dma_desc_num = 2;
    }
    result = i2s_new_channel(&channel_config, NULL, &pdm_state.channel);
    if (result != ESP_OK) {
        goto init_failed;
    }

    i2s_pdm_rx_config_t pdm_config = {
        .clk_cfg = I2S_PDM_RX_CLK_DEFAULT_CONFIG(PDM_SAMPLE_RATE_HZ),
        .slot_cfg = I2S_PDM_RX_SLOT_PCM_FMT_DEFAULT_CONFIG(
            I2S_DATA_BIT_WIDTH_16BIT,
            I2S_SLOT_MODE_MONO
        ),
        .gpio_cfg = {
            .clk = args[ARG_clk_pin].u_int,
            .din = args[ARG_data_pin].u_int,
            .invert_flags = {
                .clk_inv = false,
            },
        },
    };
    result = i2s_channel_init_pdm_rx_mode(pdm_state.channel, &pdm_config);
    if (result != ESP_OK) {
        goto init_failed;
    }
    result = i2s_channel_enable(pdm_state.channel);
    if (result != ESP_OK) {
        goto init_failed;
    }

    pdm_state.enabled = true;
    pdm_state.initialized = true;
    pdm_state.rate_hz = PDM_SAMPLE_RATE_HZ;
    return mp_const_none;

init_failed:
    pdm_state.last_error = result;
    pdm_release();
    pdm_state.last_error = result;
    mp_raise_OSError(result);
}
static MP_DEFINE_CONST_FUN_OBJ_KW(pdm_init_obj, 0, pdm_init);

static mp_obj_t pdm_read(mp_obj_t max_bytes_in) {
    pdm_owner_guard();
    mp_int_t max_bytes = mp_obj_get_int(max_bytes_in);
    if (max_bytes <= 0 || (max_bytes & 1)) {
        mp_raise_ValueError(MP_ERROR_TEXT("max_bytes must be positive and even"));
    }
    pdm_state.read_calls++;
    if (!pdm_state.initialized) {
        return mp_const_none;
    }
    if (pdm_state.queued == 0) {
        esp_err_t result = pdm_pump();
        if (result != ESP_OK) {
            mp_raise_OSError(result);
        }
    }
    if (pdm_state.queued == 0) {
        return mp_const_none;
    }

    size_t count = (size_t)max_bytes;
    if (count > pdm_state.queued) {
        count = pdm_state.queued;
    }
    uint8_t *payload = heap_caps_malloc(count, MALLOC_CAP_8BIT);
    if (payload == NULL) {
        pdm_state.last_error = ESP_ERR_NO_MEM;
        mp_raise_OSError(MP_ENOMEM);
    }
    size_t first = pdm_state.capacity - pdm_state.head;
    if (first > count) {
        first = count;
    }
    memcpy(payload, pdm_state.buffer + pdm_state.head, first);
    memcpy(payload + first, pdm_state.buffer, count - first);
    pdm_state.head = (pdm_state.head + count) % pdm_state.capacity;
    pdm_state.queued -= count;
    pdm_state.read_bytes += count;
    mp_obj_t result = mp_obj_new_bytes(payload, count);
    heap_caps_free(payload);
    return result;
}
static MP_DEFINE_CONST_FUN_OBJ_1(pdm_read_obj, pdm_read);

static mp_obj_t pdm_available(void) {
    pdm_owner_guard();
    if (!pdm_state.initialized) {
        return mp_const_false;
    }
    esp_err_t result = pdm_pump();
    if (result != ESP_OK) {
        mp_raise_OSError(result);
    }
    return mp_obj_new_bool(pdm_state.queued > 0);
}
static MP_DEFINE_CONST_FUN_OBJ_0(pdm_available_obj, pdm_available);

static mp_obj_t pdm_deinit(void) {
    pdm_owner_guard();
    esp_err_t result = pdm_release();
    if (result != ESP_OK) {
        pdm_state.last_error = result;
        mp_raise_OSError(result);
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(pdm_deinit_obj, pdm_deinit);

static void pdm_stats_store(mp_obj_t stats, qstr key, mp_obj_t value) {
    mp_obj_dict_store(stats, MP_OBJ_NEW_QSTR(key), value);
}

static mp_obj_t pdm_stats(void) {
    pdm_owner_guard();
    mp_obj_t stats = mp_obj_new_dict(9);
    pdm_stats_store(stats, MP_QSTR_initialized, mp_obj_new_bool(pdm_state.initialized));
    pdm_stats_store(stats, MP_QSTR_rate_hz, mp_obj_new_int_from_uint(pdm_state.rate_hz));
    pdm_stats_store(stats, MP_QSTR_queued_bytes, mp_obj_new_int_from_uint(pdm_state.queued));
    pdm_stats_store(stats, MP_QSTR_dropped_bytes, mp_obj_new_int_from_ull(pdm_state.dropped_bytes));
    pdm_stats_store(stats, MP_QSTR_read_bytes, mp_obj_new_int_from_ull(pdm_state.read_bytes));
    pdm_stats_store(stats, MP_QSTR_read_calls, mp_obj_new_int_from_ull(pdm_state.read_calls));
    pdm_stats_store(stats, MP_QSTR_overruns, mp_obj_new_int_from_ull(pdm_state.overruns));
    pdm_stats_store(stats, MP_QSTR_last_error, mp_obj_new_int(pdm_state.last_error));
    return stats;
}
static MP_DEFINE_CONST_FUN_OBJ_0(pdm_stats_obj, pdm_stats);

static const mp_rom_map_elem_t pdm_module_globals_table[] = {
    {MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_pdm)},
    {MP_ROM_QSTR(MP_QSTR_init), MP_ROM_PTR(&pdm_init_obj)},
    {MP_ROM_QSTR(MP_QSTR_read), MP_ROM_PTR(&pdm_read_obj)},
    {MP_ROM_QSTR(MP_QSTR_available), MP_ROM_PTR(&pdm_available_obj)},
    {MP_ROM_QSTR(MP_QSTR_deinit), MP_ROM_PTR(&pdm_deinit_obj)},
    {MP_ROM_QSTR(MP_QSTR_stats), MP_ROM_PTR(&pdm_stats_obj)},
};
static MP_DEFINE_CONST_DICT(pdm_module_globals, pdm_module_globals_table);

const mp_obj_module_t pdm_user_cmodule = {
    .base = {&mp_type_module},
    .globals = (mp_obj_dict_t *)&pdm_module_globals,
};

MP_REGISTER_MODULE(MP_QSTR_pdm, pdm_user_cmodule);
