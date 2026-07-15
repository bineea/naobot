#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "mbedtls/pk.h"
#include "mbedtls/sha256.h"
#include "py/binary.h"
#include "py/obj.h"
#include "py/objdict.h"
#include "py/runtime.h"

#include "ota_public_key_selected.h"

#define NAOBOT_OTA_MAX_IMAGE_SIZE 0x280000
#define NAOBOT_OTA_MAX_CHUNK_SIZE 4096
#define NAOBOT_OTA_MAX_MANIFEST_SIZE 1024
#define NAOBOT_OTA_MAX_SIGNATURE_SIZE 128

typedef enum {
    NAOBOT_OTA_IDLE,
    NAOBOT_OTA_WRITING,
    NAOBOT_OTA_READY,
    NAOBOT_OTA_ABORTED,
    NAOBOT_OTA_FAILED,
} naobot_ota_state_t;

static naobot_ota_state_t ota_state = NAOBOT_OTA_IDLE;
static esp_ota_handle_t ota_handle;
static const esp_partition_t *ota_partition;
static size_t ota_expected_size;
static size_t ota_written_size;
static uint8_t ota_expected_sha256[32];
static mbedtls_sha256_context ota_sha256;
static bool ota_active;
static bool ota_sha256_active;
static char ota_last_error[96];

static const char *naobot_ota_state_name(void) {
    switch (ota_state) {
        case NAOBOT_OTA_WRITING:
            return "installing";
        case NAOBOT_OTA_READY:
            return "ready_to_reboot";
        case NAOBOT_OTA_ABORTED:
            return "aborted";
        case NAOBOT_OTA_FAILED:
            return "failed";
        default:
            return "idle";
    }
}

static void naobot_ota_set_error(const char *message) {
    snprintf(ota_last_error, sizeof(ota_last_error), "%s", message);
    ota_state = NAOBOT_OTA_FAILED;
}

static void naobot_ota_set_esp_error(const char *operation, esp_err_t error) {
    snprintf(ota_last_error, sizeof(ota_last_error), "%s failed: 0x%x", operation, error);
    ota_state = NAOBOT_OTA_FAILED;
}

static void naobot_ota_free_sha256(void) {
    if (ota_sha256_active) {
        mbedtls_sha256_free(&ota_sha256);
        ota_sha256_active = false;
    }
}

static void naobot_ota_abort_active(void) {
    if (ota_active) {
        esp_ota_abort(ota_handle);
        ota_active = false;
    }
    naobot_ota_free_sha256();
    ota_partition = NULL;
}

static bool naobot_constant_time_equal(const uint8_t *left, const uint8_t *right, size_t size) {
    uint8_t difference = 0;
    for (size_t index = 0; index < size; ++index) {
        difference |= left[index] ^ right[index];
    }
    return difference == 0;
}

static mp_obj_t nao_ota_verify_manifest(mp_obj_t manifest_in, mp_obj_t signature_in) {
    mp_buffer_info_t manifest;
    mp_buffer_info_t signature;
    mp_get_buffer_raise(manifest_in, &manifest, MP_BUFFER_READ);
    mp_get_buffer_raise(signature_in, &signature, MP_BUFFER_READ);
    if (
        manifest.len == 0 || manifest.len > NAOBOT_OTA_MAX_MANIFEST_SIZE
        || signature.len == 0 || signature.len > NAOBOT_OTA_MAX_SIGNATURE_SIZE
    ) {
        return mp_const_false;
    }

    uint8_t digest[32];
    if (mbedtls_sha256(manifest.buf, manifest.len, digest, 0) != 0) {
        return mp_const_false;
    }
    mbedtls_pk_context public_key;
    mbedtls_pk_init(&public_key);
    const unsigned char *pem = (const unsigned char *)NAOBOT_OTA_PUBLIC_KEY_PEM;
    int result = mbedtls_pk_parse_public_key(
        &public_key,
        pem,
        strlen(NAOBOT_OTA_PUBLIC_KEY_PEM) + 1
    );
    if (result == 0 && mbedtls_pk_can_do(&public_key, MBEDTLS_PK_ECDSA)) {
        result = mbedtls_pk_verify(
            &public_key,
            MBEDTLS_MD_SHA256,
            digest,
            sizeof(digest),
            signature.buf,
            signature.len
        );
    }
    mbedtls_pk_free(&public_key);
    return mp_obj_new_bool(result == 0);
}
static MP_DEFINE_CONST_FUN_OBJ_2(nao_ota_verify_manifest_obj, nao_ota_verify_manifest);

static mp_obj_t nao_ota_begin(mp_obj_t image_size_in, mp_obj_t expected_sha256_in) {
    mp_int_t image_size = mp_obj_get_int(image_size_in);
    mp_buffer_info_t expected_sha256;
    mp_get_buffer_raise(expected_sha256_in, &expected_sha256, MP_BUFFER_READ);
    if (ota_active) {
        mp_raise_msg(&mp_type_RuntimeError, MP_ERROR_TEXT("OTA already active"));
    }
    if (image_size <= 0 || image_size > NAOBOT_OTA_MAX_IMAGE_SIZE) {
        mp_raise_ValueError(MP_ERROR_TEXT("invalid OTA image size"));
    }
    if (expected_sha256.len != sizeof(ota_expected_sha256)) {
        mp_raise_ValueError(MP_ERROR_TEXT("expected sha256 must be 32 bytes"));
    }

    const esp_partition_t *running = esp_ota_get_running_partition();
    const esp_partition_t *next = esp_ota_get_next_update_partition(NULL);
    if (running == NULL || next == NULL || next == running || next->address == running->address) {
        naobot_ota_set_error("safe OTA partition unavailable");
        mp_raise_msg(&mp_type_OSError, MP_ERROR_TEXT("safe OTA partition unavailable"));
    }
    if (next->size < (size_t)image_size) {
        naobot_ota_set_error("OTA image exceeds target partition");
        mp_raise_msg(&mp_type_OSError, MP_ERROR_TEXT("OTA image exceeds target partition"));
    }

    esp_err_t result = esp_ota_begin(next, OTA_WITH_SEQUENTIAL_WRITES, &ota_handle);
    if (result != ESP_OK) {
        naobot_ota_set_esp_error("esp_ota_begin", result);
        mp_raise_msg_varg(&mp_type_OSError, MP_ERROR_TEXT("esp_ota_begin failed: 0x%x"), result);
    }
    ota_partition = next;
    ota_active = true;
    ota_expected_size = (size_t)image_size;
    ota_written_size = 0;
    memcpy(ota_expected_sha256, expected_sha256.buf, sizeof(ota_expected_sha256));
    ota_last_error[0] = '\0';
    mbedtls_sha256_init(&ota_sha256);
    ota_sha256_active = true;
    if (mbedtls_sha256_starts(&ota_sha256, 0) != 0) {
        naobot_ota_abort_active();
        naobot_ota_set_error("sha256 initialization failed");
        mp_raise_msg(&mp_type_OSError, MP_ERROR_TEXT("sha256 initialization failed"));
    }
    ota_state = NAOBOT_OTA_WRITING;
    return mp_const_true;
}
static MP_DEFINE_CONST_FUN_OBJ_2(nao_ota_begin_obj, nao_ota_begin);

static mp_obj_t nao_ota_write(mp_obj_t chunk_in) {
    mp_buffer_info_t chunk;
    mp_get_buffer_raise(chunk_in, &chunk, MP_BUFFER_READ);
    if (!ota_active || ota_state != NAOBOT_OTA_WRITING) {
        mp_raise_msg(&mp_type_RuntimeError, MP_ERROR_TEXT("OTA is not active"));
    }
    if (
        chunk.len == 0 || chunk.len > NAOBOT_OTA_MAX_CHUNK_SIZE
        || chunk.len > ota_expected_size - ota_written_size
    ) {
        naobot_ota_abort_active();
        naobot_ota_set_error("invalid OTA chunk size");
        mp_raise_ValueError(MP_ERROR_TEXT("invalid OTA chunk size"));
    }
    esp_err_t result = esp_ota_write(ota_handle, chunk.buf, chunk.len);
    if (result != ESP_OK) {
        naobot_ota_abort_active();
        naobot_ota_set_esp_error("esp_ota_write", result);
        mp_raise_msg_varg(&mp_type_OSError, MP_ERROR_TEXT("esp_ota_write failed: 0x%x"), result);
    }
    if (mbedtls_sha256_update(&ota_sha256, chunk.buf, chunk.len) != 0) {
        naobot_ota_abort_active();
        naobot_ota_set_error("sha256 update failed");
        mp_raise_msg(&mp_type_OSError, MP_ERROR_TEXT("sha256 update failed"));
    }
    ota_written_size += chunk.len;
    return mp_obj_new_int_from_uint(chunk.len);
}
static MP_DEFINE_CONST_FUN_OBJ_1(nao_ota_write_obj, nao_ota_write);

static mp_obj_t nao_ota_finish(void) {
    if (!ota_active || ota_state != NAOBOT_OTA_WRITING) {
        mp_raise_msg(&mp_type_RuntimeError, MP_ERROR_TEXT("OTA is not active"));
    }
    if (ota_written_size != ota_expected_size) {
        naobot_ota_abort_active();
        naobot_ota_set_error("firmware size mismatch");
        mp_raise_msg(&mp_type_OSError, MP_ERROR_TEXT("firmware size mismatch"));
    }
    uint8_t actual_sha256[32];
    if (mbedtls_sha256_finish(&ota_sha256, actual_sha256) != 0) {
        naobot_ota_abort_active();
        naobot_ota_set_error("sha256 finish failed");
        mp_raise_msg(&mp_type_OSError, MP_ERROR_TEXT("sha256 finish failed"));
    }
    naobot_ota_free_sha256();
    if (!naobot_constant_time_equal(actual_sha256, ota_expected_sha256, sizeof(actual_sha256))) {
        naobot_ota_abort_active();
        naobot_ota_set_error("firmware digest mismatch");
        mp_raise_msg(&mp_type_OSError, MP_ERROR_TEXT("firmware digest mismatch"));
    }

    esp_err_t result = esp_ota_end(ota_handle);
    ota_active = false;
    if (result != ESP_OK) {
        ota_partition = NULL;
        naobot_ota_set_esp_error("esp_ota_end", result);
        mp_raise_msg_varg(&mp_type_OSError, MP_ERROR_TEXT("esp_ota_end failed: 0x%x"), result);
    }
    result = esp_ota_set_boot_partition(ota_partition);
    ota_partition = NULL;
    if (result != ESP_OK) {
        naobot_ota_set_esp_error("esp_ota_set_boot_partition", result);
        mp_raise_msg_varg(
            &mp_type_OSError,
            MP_ERROR_TEXT("esp_ota_set_boot_partition failed: 0x%x"),
            result
        );
    }
    ota_state = NAOBOT_OTA_READY;
    ota_last_error[0] = '\0';
    return mp_const_true;
}
static MP_DEFINE_CONST_FUN_OBJ_0(nao_ota_finish_obj, nao_ota_finish);

static mp_obj_t nao_ota_abort(void) {
    bool was_active = ota_active;
    naobot_ota_abort_active();
    ota_state = NAOBOT_OTA_ABORTED;
    if (ota_last_error[0] == '\0') {
        snprintf(ota_last_error, sizeof(ota_last_error), "OTA aborted");
    }
    return mp_obj_new_bool(was_active);
}
static MP_DEFINE_CONST_FUN_OBJ_0(nao_ota_abort_obj, nao_ota_abort);

static void naobot_ota_dict_store(mp_obj_t dict, qstr key, mp_obj_t value) {
    mp_obj_dict_store(dict, MP_OBJ_NEW_QSTR(key), value);
}

static mp_obj_t nao_ota_status(void) {
    mp_obj_t status = mp_obj_new_dict(5);
    const char *state = naobot_ota_state_name();
    naobot_ota_dict_store(status, MP_QSTR_state, mp_obj_new_str(state, strlen(state)));
    naobot_ota_dict_store(status, MP_QSTR_bytes_written, mp_obj_new_int_from_uint(ota_written_size));
    naobot_ota_dict_store(status, MP_QSTR_image_size, mp_obj_new_int_from_uint(ota_expected_size));
    naobot_ota_dict_store(
        status,
        MP_QSTR_progress_pct,
        mp_obj_new_int(ota_expected_size == 0 ? 0 : (ota_written_size * 100) / ota_expected_size)
    );
    naobot_ota_dict_store(
        status,
        MP_QSTR_error,
        ota_last_error[0] == '\0'
            ? mp_const_none
            : mp_obj_new_str(ota_last_error, strlen(ota_last_error))
    );
    return status;
}
static MP_DEFINE_CONST_FUN_OBJ_0(nao_ota_status_obj, nao_ota_status);

static mp_obj_t nao_ota_pending_verify(void) {
    const esp_partition_t *running = esp_ota_get_running_partition();
    if (running == NULL) {
        return mp_const_none;
    }
    esp_ota_img_states_t state;
    esp_err_t result = esp_ota_get_state_partition(running, &state);
    if (result == ESP_ERR_NOT_SUPPORTED || result == ESP_ERR_NOT_FOUND) {
        return mp_const_false;
    }
    if (result != ESP_OK) {
        return mp_const_none;
    }
    return mp_obj_new_bool(state == ESP_OTA_IMG_PENDING_VERIFY);
}
static MP_DEFINE_CONST_FUN_OBJ_0(nao_ota_pending_verify_obj, nao_ota_pending_verify);

static mp_obj_t nao_ota_mark_healthy(void) {
    mp_obj_t pending = nao_ota_pending_verify();
    if (pending != mp_const_true) {
        return mp_const_false;
    }
    esp_err_t result = esp_ota_mark_app_valid_cancel_rollback();
    if (result != ESP_OK) {
        mp_raise_msg_varg(&mp_type_OSError, MP_ERROR_TEXT("mark healthy failed: 0x%x"), result);
    }
    return mp_const_true;
}
static MP_DEFINE_CONST_FUN_OBJ_0(nao_ota_mark_healthy_obj, nao_ota_mark_healthy);

static mp_obj_t nao_ota_rollback_and_reboot(void) {
    esp_err_t result = esp_ota_mark_app_invalid_rollback_and_reboot();
    mp_raise_msg_varg(&mp_type_OSError, MP_ERROR_TEXT("rollback failed: 0x%x"), result);
}
static MP_DEFINE_CONST_FUN_OBJ_0(nao_ota_rollback_and_reboot_obj, nao_ota_rollback_and_reboot);

static const mp_rom_map_elem_t nao_ota_module_globals_table[] = {
    {MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_nao_ota)},
    {MP_ROM_QSTR(MP_QSTR_verify_manifest), MP_ROM_PTR(&nao_ota_verify_manifest_obj)},
    {MP_ROM_QSTR(MP_QSTR_begin), MP_ROM_PTR(&nao_ota_begin_obj)},
    {MP_ROM_QSTR(MP_QSTR_write), MP_ROM_PTR(&nao_ota_write_obj)},
    {MP_ROM_QSTR(MP_QSTR_finish), MP_ROM_PTR(&nao_ota_finish_obj)},
    {MP_ROM_QSTR(MP_QSTR_abort), MP_ROM_PTR(&nao_ota_abort_obj)},
    {MP_ROM_QSTR(MP_QSTR_status), MP_ROM_PTR(&nao_ota_status_obj)},
    {MP_ROM_QSTR(MP_QSTR_pending_verify), MP_ROM_PTR(&nao_ota_pending_verify_obj)},
    {MP_ROM_QSTR(MP_QSTR_mark_healthy), MP_ROM_PTR(&nao_ota_mark_healthy_obj)},
    {MP_ROM_QSTR(MP_QSTR_rollback_and_reboot), MP_ROM_PTR(&nao_ota_rollback_and_reboot_obj)},
};
static MP_DEFINE_CONST_DICT(nao_ota_module_globals, nao_ota_module_globals_table);

const mp_obj_module_t nao_ota_user_cmodule = {
    .base = {&mp_type_module},
    .globals = (mp_obj_dict_t *)&nao_ota_module_globals,
};

MP_REGISTER_MODULE(MP_QSTR_nao_ota, nao_ota_user_cmodule);
