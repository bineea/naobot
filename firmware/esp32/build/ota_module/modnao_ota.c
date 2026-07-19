#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "mbedtls/ecp.h"
#include "mbedtls/pk.h"
#include "mbedtls/sha256.h"
#include "nvs.h"
#include "py/binary.h"
#include "py/obj.h"
#include "py/objdict.h"
#include "py/runtime.h"

#include "ota_public_key_selected.h"

#define NAOBOT_OTA_MAX_IMAGE_SIZE 0x280000
#define NAOBOT_OTA_MAX_CHUNK_SIZE 4096
#define NAOBOT_OTA_MAX_MANIFEST_SIZE 1024
#define NAOBOT_OTA_MAX_SIGNATURE_SIZE 128
#define NAOBOT_OTA_NVS_NAMESPACE "nao_ota"
#define NAOBOT_OTA_CURRENT_SEQUENCE_KEY "current_seq"
#define NAOBOT_OTA_PENDING_SEQUENCE_KEY "pending_seq"
#define NAOBOT_OTA_PHASE_KEY "phase"
#define NAOBOT_OTA_TARGET_ADDRESS_KEY "target_addr"

typedef enum {
    NAOBOT_OTA_PHASE_NONE = 0,
    NAOBOT_OTA_PHASE_PREPARED = 1,
    NAOBOT_OTA_PHASE_ACTIVATED = 2,
    NAOBOT_OTA_PHASE_CONFIRMING = 3,
    NAOBOT_OTA_PHASE_ROLLBACK = 4,
} naobot_ota_phase_t;

typedef struct {
    bool pending_found;
    uint32_t pending_sequence;
    bool phase_found;
    naobot_ota_phase_t phase;
    bool target_found;
    uint32_t target_address;
} naobot_ota_transaction_t;

typedef enum {
    NAOBOT_OTA_IDLE,
    NAOBOT_OTA_WRITING,
    NAOBOT_OTA_STAGED,
    NAOBOT_OTA_ACTIVATED,
    NAOBOT_OTA_ABORTED,
    NAOBOT_OTA_FAILED,
} naobot_ota_state_t;

static naobot_ota_state_t ota_state = NAOBOT_OTA_IDLE;
static esp_ota_handle_t ota_handle;
static const esp_partition_t *ota_partition;
static size_t ota_expected_size;
static size_t ota_written_size;
static uint32_t ota_sequence;
static uint8_t ota_expected_sha256[32];
static mbedtls_sha256_context ota_sha256;
static bool ota_active;
static bool ota_sha256_active;
static char ota_last_error[128];

static const char *naobot_ota_state_name(void) {
    switch (ota_state) {
        case NAOBOT_OTA_WRITING:
            return "installing";
        case NAOBOT_OTA_STAGED:
            return "staged";
        case NAOBOT_OTA_ACTIVATED:
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

static esp_err_t naobot_nvs_get_optional_u32(const char *key, uint32_t *value, bool *found) {
    nvs_handle_t handle = 0;
    esp_err_t result = nvs_open(NAOBOT_OTA_NVS_NAMESPACE, NVS_READONLY, &handle);
    if (result == ESP_ERR_NVS_NOT_FOUND) {
        *found = false;
        return ESP_OK;
    }
    if (result != ESP_OK) {
        return result;
    }
    result = nvs_get_u32(handle, key, value);
    nvs_close(handle);
    if (result == ESP_ERR_NVS_NOT_FOUND) {
        *found = false;
        return ESP_OK;
    }
    if (result == ESP_OK) {
        *found = true;
    }
    return result;
}

static esp_err_t naobot_nvs_erase_optional(nvs_handle_t handle, const char *key) {
    esp_err_t result = nvs_erase_key(handle, key);
    return result == ESP_ERR_NVS_NOT_FOUND ? ESP_OK : result;
}

static esp_err_t naobot_nvs_read_transaction(naobot_ota_transaction_t *transaction) {
    memset(transaction, 0, sizeof(*transaction));
    uint32_t phase = NAOBOT_OTA_PHASE_NONE;
    esp_err_t result = naobot_nvs_get_optional_u32(
        NAOBOT_OTA_PENDING_SEQUENCE_KEY,
        &transaction->pending_sequence,
        &transaction->pending_found
    );
    if (result == ESP_OK) {
        result = naobot_nvs_get_optional_u32(
            NAOBOT_OTA_PHASE_KEY,
            &phase,
            &transaction->phase_found
        );
    }
    if (result == ESP_OK) {
        result = naobot_nvs_get_optional_u32(
            NAOBOT_OTA_TARGET_ADDRESS_KEY,
            &transaction->target_address,
            &transaction->target_found
        );
    }
    transaction->phase = (naobot_ota_phase_t)phase;
    return result;
}

static esp_err_t naobot_nvs_write_transaction(
    uint32_t pending_sequence,
    uint32_t target_address,
    naobot_ota_phase_t phase
) {
    nvs_handle_t handle = 0;
    esp_err_t result = nvs_open(NAOBOT_OTA_NVS_NAMESPACE, NVS_READWRITE, &handle);
    if (result == ESP_OK) {
        result = nvs_set_u32(handle, NAOBOT_OTA_PENDING_SEQUENCE_KEY, pending_sequence);
    }
    if (result == ESP_OK) {
        result = nvs_set_u32(handle, NAOBOT_OTA_TARGET_ADDRESS_KEY, target_address);
    }
    if (result == ESP_OK) {
        result = nvs_set_u32(handle, NAOBOT_OTA_PHASE_KEY, (uint32_t)phase);
    }
    if (result == ESP_OK) {
        result = nvs_commit(handle);
    }
    if (handle != 0) {
        nvs_close(handle);
    }
    return result;
}

static esp_err_t naobot_nvs_set_phase(naobot_ota_phase_t phase) {
    nvs_handle_t handle = 0;
    esp_err_t result = nvs_open(NAOBOT_OTA_NVS_NAMESPACE, NVS_READWRITE, &handle);
    if (result == ESP_OK) {
        result = nvs_set_u32(handle, NAOBOT_OTA_PHASE_KEY, (uint32_t)phase);
    }
    if (result == ESP_OK) {
        result = nvs_commit(handle);
    }
    if (handle != 0) {
        nvs_close(handle);
    }
    return result;
}

static esp_err_t naobot_nvs_begin_confirming(uint32_t pending_sequence) {
    nvs_handle_t handle = 0;
    esp_err_t result = nvs_open(NAOBOT_OTA_NVS_NAMESPACE, NVS_READWRITE, &handle);
    if (result == ESP_OK) {
        result = nvs_set_u32(handle, NAOBOT_OTA_CURRENT_SEQUENCE_KEY, pending_sequence);
    }
    if (result == ESP_OK) {
        result = nvs_set_u32(
            handle,
            NAOBOT_OTA_PHASE_KEY,
            (uint32_t)NAOBOT_OTA_PHASE_CONFIRMING
        );
    }
    if (result == ESP_OK) {
        result = nvs_commit(handle);
    }
    if (handle != 0) {
        nvs_close(handle);
    }
    return result;
}

static esp_err_t naobot_nvs_clear_transaction(void) {
    nvs_handle_t handle = 0;
    esp_err_t result = nvs_open(NAOBOT_OTA_NVS_NAMESPACE, NVS_READWRITE, &handle);
    if (result == ESP_OK) {
        result = naobot_nvs_erase_optional(handle, NAOBOT_OTA_PENDING_SEQUENCE_KEY);
    }
    if (result == ESP_OK) {
        result = naobot_nvs_erase_optional(handle, NAOBOT_OTA_PHASE_KEY);
    }
    if (result == ESP_OK) {
        result = naobot_nvs_erase_optional(handle, NAOBOT_OTA_TARGET_ADDRESS_KEY);
    }
    if (result == ESP_OK) {
        result = nvs_commit(handle);
    }
    if (handle != 0) {
        nvs_close(handle);
    }
    return result;
}

static const char *naobot_ota_phase_name(naobot_ota_phase_t phase) {
    switch (phase) {
        case NAOBOT_OTA_PHASE_PREPARED:
            return "prepared";
        case NAOBOT_OTA_PHASE_ACTIVATED:
            return "activated";
        case NAOBOT_OTA_PHASE_CONFIRMING:
            return "confirming";
        case NAOBOT_OTA_PHASE_ROLLBACK:
            return "rollback";
        default:
            return "none";
    }
}

static esp_err_t naobot_partition_state(
    const esp_partition_t *partition,
    esp_ota_img_states_t *state,
    bool *found
) {
    esp_err_t result = esp_ota_get_state_partition(partition, state);
    if (result == ESP_ERR_NOT_FOUND || result == ESP_ERR_NOT_SUPPORTED) {
        *found = false;
        return ESP_OK;
    }
    *found = result == ESP_OK;
    return result;
}

static esp_err_t naobot_ota_abort_active(void) {
    esp_err_t result = ESP_OK;
    if (ota_active) {
        result = esp_ota_abort(ota_handle);
        ota_active = false;
    }
    naobot_ota_free_sha256();
    ota_partition = NULL;
    return result;
}

static void naobot_ota_record_failure_after_abort(const char *message) {
    esp_err_t abort_result = naobot_ota_abort_active();
    if (abort_result == ESP_OK) {
        snprintf(ota_last_error, sizeof(ota_last_error), "%s", message);
    } else {
        snprintf(
            ota_last_error,
            sizeof(ota_last_error),
            "%s; esp_ota_abort failed: 0x%x",
            message,
            abort_result
        );
    }
    ota_state = NAOBOT_OTA_FAILED;
}

static esp_err_t naobot_ota_recover_transaction(void) {
    naobot_ota_transaction_t transaction;
    esp_err_t result = naobot_nvs_read_transaction(&transaction);
    if (result != ESP_OK) {
        return result;
    }
    if (!transaction.pending_found) {
        if (transaction.phase_found || transaction.target_found) {
            return naobot_nvs_clear_transaction();
        }
        return ESP_OK;
    }

    const esp_partition_t *running = esp_ota_get_running_partition();
    const esp_partition_t *boot = esp_ota_get_boot_partition();
    if (running == NULL || boot == NULL) {
        return ESP_ERR_NOT_FOUND;
    }
    esp_ota_img_states_t running_state = ESP_OTA_IMG_UNDEFINED;
    bool running_state_found = false;
    result = naobot_partition_state(running, &running_state, &running_state_found);
    if (result != ESP_OK) {
        return result;
    }

    if (!transaction.phase_found || !transaction.target_found) {
        if (running_state_found && running_state == ESP_OTA_IMG_PENDING_VERIFY) {
            return naobot_nvs_write_transaction(
                transaction.pending_sequence,
                running->address,
                NAOBOT_OTA_PHASE_ACTIVATED
            );
        }
        if (boot->address != running->address) {
            return naobot_nvs_write_transaction(
                transaction.pending_sequence,
                boot->address,
                NAOBOT_OTA_PHASE_ACTIVATED
            );
        }
        return naobot_nvs_clear_transaction();
    }

    bool running_is_target = running->address == transaction.target_address;
    bool boot_is_target = boot->address == transaction.target_address;
    switch (transaction.phase) {
        case NAOBOT_OTA_PHASE_PREPARED:
            if (running_is_target || boot_is_target) {
                return naobot_nvs_set_phase(NAOBOT_OTA_PHASE_ACTIVATED);
            }
            return naobot_nvs_clear_transaction();
        case NAOBOT_OTA_PHASE_ACTIVATED:
            if (running_is_target || boot_is_target) {
                return ESP_OK;
            }
            return naobot_nvs_set_phase(NAOBOT_OTA_PHASE_ROLLBACK);
        case NAOBOT_OTA_PHASE_CONFIRMING:
            if (!running_is_target) {
                return naobot_nvs_set_phase(NAOBOT_OTA_PHASE_ROLLBACK);
            }
            if (
                running_state_found
                && running_state == ESP_OTA_IMG_PENDING_VERIFY
            ) {
                result = esp_ota_mark_app_valid_cancel_rollback();
                if (result != ESP_OK) {
                    return result;
                }
            } else if (
                !running_state_found
                || running_state != ESP_OTA_IMG_VALID
            ) {
                return ESP_ERR_INVALID_STATE;
            }
            return naobot_nvs_clear_transaction();
        case NAOBOT_OTA_PHASE_ROLLBACK:
            return ESP_OK;
        default:
            return naobot_nvs_clear_transaction();
    }
}

static bool naobot_constant_time_equal(const uint8_t *left, const uint8_t *right, size_t size) {
    uint8_t difference = 0;
    for (size_t index = 0; index < size; ++index) {
        difference |= left[index] ^ right[index];
    }
    return difference == 0;
}

static bool naobot_get_uint32(mp_obj_t value_in, uint32_t *value) {
    if (!mp_obj_is_int(value_in)) {
        return false;
    }
    mp_int_t truncated = mp_obj_get_int_truncated(value_in);
    uint32_t candidate = (uint32_t)(mp_uint_t)truncated;
    if (!mp_obj_equal(value_in, mp_obj_new_int_from_uint(candidate))) {
        return false;
    }
    *value = candidate;
    return true;
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
    int result = -1;
    if (
        mbedtls_pk_parse_public_key(
            &public_key,
            pem,
            strlen(NAOBOT_OTA_PUBLIC_KEY_PEM) + 1
        ) == 0
        && mbedtls_pk_get_type(&public_key) == MBEDTLS_PK_ECKEY
    ) {
        mbedtls_ecp_keypair *ec_key = mbedtls_pk_ec(public_key);
        if (
            ec_key != NULL
            && ec_key->MBEDTLS_PRIVATE(grp).id == MBEDTLS_ECP_DP_SECP256R1
            && mbedtls_pk_can_do(&public_key, MBEDTLS_PK_ECDSA)
        ) {
            result = mbedtls_pk_verify(
                &public_key,
                MBEDTLS_MD_SHA256,
                digest,
                sizeof(digest),
                signature.buf,
                signature.len
            );
        }
    }
    mbedtls_pk_free(&public_key);
    return mp_obj_new_bool(result == 0);
}
static MP_DEFINE_CONST_FUN_OBJ_2(nao_ota_verify_manifest_obj, nao_ota_verify_manifest);

static mp_obj_t nao_ota_begin(
    mp_obj_t image_size_in,
    mp_obj_t expected_sha256_in,
    mp_obj_t sequence_in
) {
    mp_int_t image_size = mp_obj_get_int(image_size_in);
    uint32_t sequence;
    mp_buffer_info_t expected_sha256;
    mp_get_buffer_raise(expected_sha256_in, &expected_sha256, MP_BUFFER_READ);
    if (ota_active || ota_partition != NULL) {
        mp_raise_msg(&mp_type_RuntimeError, MP_ERROR_TEXT("OTA already active or staged"));
    }
    if (image_size <= 0 || image_size > NAOBOT_OTA_MAX_IMAGE_SIZE) {
        mp_raise_ValueError(MP_ERROR_TEXT("invalid OTA image size"));
    }
    if (!naobot_get_uint32(sequence_in, &sequence)) {
        mp_raise_ValueError(MP_ERROR_TEXT("OTA sequence must be uint32"));
    }
    if (expected_sha256.len != sizeof(ota_expected_sha256)) {
        mp_raise_ValueError(MP_ERROR_TEXT("expected sha256 must be 32 bytes"));
    }

    esp_err_t result = naobot_ota_recover_transaction();
    if (result != ESP_OK) {
        naobot_ota_set_esp_error("recover OTA transaction", result);
        mp_raise_msg_varg(
            &mp_type_OSError,
            MP_ERROR_TEXT("recover OTA transaction failed: 0x%x"),
            result
        );
    }
    uint32_t current_sequence = 0;
    uint32_t pending_sequence = 0;
    bool current_found = false;
    bool pending_found = false;
    result = naobot_nvs_get_optional_u32(
        NAOBOT_OTA_CURRENT_SEQUENCE_KEY,
        &current_sequence,
        &current_found
    );
    if (result != ESP_OK) {
        naobot_ota_set_esp_error("read current sequence", result);
        mp_raise_msg_varg(&mp_type_OSError, MP_ERROR_TEXT("read current sequence failed: 0x%x"), result);
    }
    result = naobot_nvs_get_optional_u32(
        NAOBOT_OTA_PENDING_SEQUENCE_KEY,
        &pending_sequence,
        &pending_found
    );
    if (result != ESP_OK) {
        naobot_ota_set_esp_error("read pending sequence", result);
        mp_raise_msg_varg(&mp_type_OSError, MP_ERROR_TEXT("read pending sequence failed: 0x%x"), result);
    }
    if (pending_found || sequence <= current_sequence) {
        mp_raise_ValueError(MP_ERROR_TEXT("OTA sequence is stale or pending"));
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

    result = esp_ota_begin(next, OTA_WITH_SEQUENTIAL_WRITES, &ota_handle);
    if (result != ESP_OK) {
        naobot_ota_set_esp_error("esp_ota_begin", result);
        mp_raise_msg_varg(&mp_type_OSError, MP_ERROR_TEXT("esp_ota_begin failed: 0x%x"), result);
    }
    ota_partition = next;
    ota_active = true;
    ota_expected_size = (size_t)image_size;
    ota_written_size = 0;
    ota_sequence = sequence;
    memcpy(ota_expected_sha256, expected_sha256.buf, sizeof(ota_expected_sha256));
    ota_last_error[0] = '\0';
    mbedtls_sha256_init(&ota_sha256);
    ota_sha256_active = true;
    if (mbedtls_sha256_starts(&ota_sha256, 0) != 0) {
        naobot_ota_record_failure_after_abort("sha256 initialization failed");
        mp_raise_msg(&mp_type_OSError, MP_ERROR_TEXT("sha256 initialization failed"));
    }
    ota_state = NAOBOT_OTA_WRITING;
    return mp_const_true;
}
static MP_DEFINE_CONST_FUN_OBJ_3(nao_ota_begin_obj, nao_ota_begin);

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
        naobot_ota_record_failure_after_abort("invalid OTA chunk size");
        mp_raise_ValueError(MP_ERROR_TEXT("invalid OTA chunk size"));
    }
    esp_err_t result = esp_ota_write(ota_handle, chunk.buf, chunk.len);
    if (result != ESP_OK) {
        char message[64];
        snprintf(message, sizeof(message), "esp_ota_write failed: 0x%x", result);
        naobot_ota_record_failure_after_abort(message);
        mp_raise_msg_varg(&mp_type_OSError, MP_ERROR_TEXT("esp_ota_write failed: 0x%x"), result);
    }
    if (mbedtls_sha256_update(&ota_sha256, chunk.buf, chunk.len) != 0) {
        naobot_ota_record_failure_after_abort("sha256 update failed");
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
        naobot_ota_record_failure_after_abort("firmware size mismatch");
        mp_raise_msg(&mp_type_OSError, MP_ERROR_TEXT("firmware size mismatch"));
    }
    uint8_t actual_sha256[32];
    if (mbedtls_sha256_finish(&ota_sha256, actual_sha256) != 0) {
        naobot_ota_record_failure_after_abort("sha256 finish failed");
        mp_raise_msg(&mp_type_OSError, MP_ERROR_TEXT("sha256 finish failed"));
    }
    naobot_ota_free_sha256();
    if (!naobot_constant_time_equal(actual_sha256, ota_expected_sha256, sizeof(actual_sha256))) {
        naobot_ota_record_failure_after_abort("firmware digest mismatch");
        mp_raise_msg(&mp_type_OSError, MP_ERROR_TEXT("firmware digest mismatch"));
    }

    esp_err_t result = esp_ota_end(ota_handle);
    ota_active = false;
    if (result != ESP_OK) {
        ota_partition = NULL;
        naobot_ota_set_esp_error("esp_ota_end", result);
        mp_raise_msg_varg(&mp_type_OSError, MP_ERROR_TEXT("esp_ota_end failed: 0x%x"), result);
    }
    ota_state = NAOBOT_OTA_STAGED;
    ota_last_error[0] = '\0';
    return mp_const_true;
}
static MP_DEFINE_CONST_FUN_OBJ_0(nao_ota_finish_obj, nao_ota_finish);

static mp_obj_t nao_ota_activate(void) {
    if (ota_state != NAOBOT_OTA_STAGED || ota_partition == NULL) {
        mp_raise_msg(&mp_type_RuntimeError, MP_ERROR_TEXT("OTA image is not staged"));
    }
    esp_err_t result = naobot_nvs_write_transaction(
        ota_sequence,
        ota_partition->address,
        NAOBOT_OTA_PHASE_PREPARED
    );
    if (result != ESP_OK) {
        naobot_ota_set_esp_error("prepare OTA activation", result);
        mp_raise_msg_varg(
            &mp_type_OSError,
            MP_ERROR_TEXT("prepare OTA activation failed: 0x%x"),
            result
        );
    }

    result = esp_ota_set_boot_partition(ota_partition);
    if (result != ESP_OK) {
        esp_err_t phase_result = naobot_nvs_set_phase(NAOBOT_OTA_PHASE_ROLLBACK);
        esp_err_t clear_result = naobot_nvs_clear_transaction();
        if (clear_result != ESP_OK) {
            snprintf(
                ota_last_error,
                sizeof(ota_last_error),
                "set boot failed: 0x%x; rollback phase: 0x%x; clear transaction: 0x%x",
                result,
                phase_result,
                clear_result
            );
            ota_state = NAOBOT_OTA_FAILED;
        } else {
            naobot_ota_set_esp_error("esp_ota_set_boot_partition", result);
        }
        mp_raise_msg_varg(
            &mp_type_OSError,
            MP_ERROR_TEXT("esp_ota_set_boot_partition failed: 0x%x"),
            result
        );
    }

    ota_state = NAOBOT_OTA_ACTIVATED;
    ota_partition = NULL;
    result = naobot_nvs_set_phase(NAOBOT_OTA_PHASE_ACTIVATED);
    if (result == ESP_OK) {
        ota_last_error[0] = '\0';
    } else {
        snprintf(
            ota_last_error,
            sizeof(ota_last_error),
            "activated; phase commit pending recovery: 0x%x",
            result
        );
    }
    return mp_const_true;
}
static MP_DEFINE_CONST_FUN_OBJ_0(nao_ota_activate_obj, nao_ota_activate);

static mp_obj_t nao_ota_abort(void) {
    if (ota_state != NAOBOT_OTA_WRITING && ota_state != NAOBOT_OTA_STAGED) {
        return mp_const_false;
    }
    esp_err_t abort_result = naobot_ota_abort_active();
    if (abort_result != ESP_OK) {
        naobot_ota_set_esp_error("esp_ota_abort", abort_result);
        mp_raise_msg_varg(
            &mp_type_OSError,
            MP_ERROR_TEXT("esp_ota_abort failed: 0x%x"),
            abort_result
        );
    }
    ota_state = NAOBOT_OTA_ABORTED;
    snprintf(ota_last_error, sizeof(ota_last_error), "OTA aborted");
    return mp_const_true;
}
static MP_DEFINE_CONST_FUN_OBJ_0(nao_ota_abort_obj, nao_ota_abort);

static void naobot_ota_dict_store(mp_obj_t dict, qstr key, mp_obj_t value) {
    mp_obj_dict_store(dict, MP_OBJ_NEW_QSTR(key), value);
}

static mp_obj_t nao_ota_status(void) {
    mp_obj_t status = mp_obj_new_dict(6);
    const char *state = naobot_ota_state_name();
    naobot_ota_dict_store(status, MP_QSTR_state, mp_obj_new_str(state, strlen(state)));
    naobot_ota_dict_store(status, MP_QSTR_bytes_written, mp_obj_new_int_from_uint(ota_written_size));
    naobot_ota_dict_store(status, MP_QSTR_image_size, mp_obj_new_int_from_uint(ota_expected_size));
    naobot_ota_dict_store(status, MP_QSTR_sequence, mp_obj_new_int_from_uint(ota_sequence));
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
    esp_err_t recovery_result = naobot_ota_recover_transaction();
    if (recovery_result != ESP_OK) {
        return mp_const_none;
    }
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

static mp_obj_t nao_ota_current_sequence(void) {
    esp_err_t recovery_result = naobot_ota_recover_transaction();
    if (recovery_result != ESP_OK) {
        mp_raise_msg_varg(
            &mp_type_OSError,
            MP_ERROR_TEXT("recover OTA transaction failed: 0x%x"),
            recovery_result
        );
    }
    uint32_t sequence = 0;
    bool found = false;
    esp_err_t result = naobot_nvs_get_optional_u32(
        NAOBOT_OTA_CURRENT_SEQUENCE_KEY,
        &sequence,
        &found
    );
    if (result != ESP_OK) {
        mp_raise_msg_varg(&mp_type_OSError, MP_ERROR_TEXT("read current sequence failed: 0x%x"), result);
    }
    return mp_obj_new_int_from_uint(found ? sequence : 0);
}
static MP_DEFINE_CONST_FUN_OBJ_0(nao_ota_current_sequence_obj, nao_ota_current_sequence);

static mp_obj_t nao_ota_pending_sequence(void) {
    esp_err_t recovery_result = naobot_ota_recover_transaction();
    if (recovery_result != ESP_OK) {
        mp_raise_msg_varg(
            &mp_type_OSError,
            MP_ERROR_TEXT("recover OTA transaction failed: 0x%x"),
            recovery_result
        );
    }
    uint32_t sequence = 0;
    bool found = false;
    esp_err_t result = naobot_nvs_get_optional_u32(
        NAOBOT_OTA_PENDING_SEQUENCE_KEY,
        &sequence,
        &found
    );
    if (result != ESP_OK) {
        mp_raise_msg_varg(&mp_type_OSError, MP_ERROR_TEXT("read pending sequence failed: 0x%x"), result);
    }
    return found ? mp_obj_new_int_from_uint(sequence) : mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(nao_ota_pending_sequence_obj, nao_ota_pending_sequence);

static mp_obj_t nao_ota_phase(void) {
    esp_err_t recovery_result = naobot_ota_recover_transaction();
    if (recovery_result != ESP_OK) {
        mp_raise_msg_varg(
            &mp_type_OSError,
            MP_ERROR_TEXT("recover OTA transaction failed: 0x%x"),
            recovery_result
        );
    }
    naobot_ota_transaction_t transaction;
    esp_err_t result = naobot_nvs_read_transaction(&transaction);
    if (result != ESP_OK) {
        mp_raise_msg_varg(&mp_type_OSError, MP_ERROR_TEXT("read OTA phase failed: 0x%x"), result);
    }
    if (!transaction.phase_found || transaction.phase == NAOBOT_OTA_PHASE_NONE) {
        return mp_const_none;
    }
    const char *phase = naobot_ota_phase_name(transaction.phase);
    return mp_obj_new_str(phase, strlen(phase));
}
static MP_DEFINE_CONST_FUN_OBJ_0(nao_ota_phase_obj, nao_ota_phase);

static mp_obj_t nao_ota_mark_healthy(void) {
    esp_err_t result = naobot_ota_recover_transaction();
    if (result != ESP_OK) {
        mp_raise_msg_varg(
            &mp_type_OSError,
            MP_ERROR_TEXT("recover OTA transaction failed: 0x%x"),
            result
        );
    }
    naobot_ota_transaction_t transaction;
    result = naobot_nvs_read_transaction(&transaction);
    if (result != ESP_OK) {
        mp_raise_msg_varg(&mp_type_OSError, MP_ERROR_TEXT("read OTA transaction failed: 0x%x"), result);
    }
    if (!transaction.pending_found) {
        return mp_const_false;
    }
    if (transaction.phase == NAOBOT_OTA_PHASE_ACTIVATED) {
        result = naobot_nvs_begin_confirming(transaction.pending_sequence);
        if (result != ESP_OK) {
            mp_raise_msg_varg(
                &mp_type_OSError,
                MP_ERROR_TEXT("begin confirming failed: 0x%x"),
                result
            );
        }
    } else if (transaction.phase != NAOBOT_OTA_PHASE_CONFIRMING) {
        mp_raise_msg(&mp_type_RuntimeError, MP_ERROR_TEXT("OTA transaction is not confirmable"));
    }

    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t running_state = ESP_OTA_IMG_UNDEFINED;
    bool running_state_found = false;
    if (running == NULL) {
        mp_raise_msg(&mp_type_OSError, MP_ERROR_TEXT("running partition unavailable"));
    }
    result = naobot_partition_state(running, &running_state, &running_state_found);
    if (result != ESP_OK || !running_state_found) {
        mp_raise_msg(&mp_type_OSError, MP_ERROR_TEXT("running image state unavailable"));
    }
    if (running_state == ESP_OTA_IMG_PENDING_VERIFY) {
        result = esp_ota_mark_app_valid_cancel_rollback();
        if (result != ESP_OK) {
            mp_raise_msg_varg(&mp_type_OSError, MP_ERROR_TEXT("mark healthy failed: 0x%x"), result);
        }
    } else if (running_state != ESP_OTA_IMG_VALID) {
        mp_raise_msg(&mp_type_OSError, MP_ERROR_TEXT("running image is not confirmable"));
    }

    result = naobot_nvs_clear_transaction();
    if (result != ESP_OK) {
        mp_raise_msg_varg(
            &mp_type_OSError,
            MP_ERROR_TEXT("finish confirming failed: 0x%x"),
            result
        );
    }
    return mp_const_true;
}
static MP_DEFINE_CONST_FUN_OBJ_0(nao_ota_mark_healthy_obj, nao_ota_mark_healthy);

static mp_obj_t nao_ota_rollback_and_reboot(void) {
    esp_err_t result = naobot_nvs_set_phase(NAOBOT_OTA_PHASE_ROLLBACK);
    if (result != ESP_OK) {
        mp_raise_msg_varg(&mp_type_OSError, MP_ERROR_TEXT("set rollback phase failed: 0x%x"), result);
    }
    result = naobot_nvs_clear_transaction();
    if (result != ESP_OK) {
        mp_raise_msg_varg(&mp_type_OSError, MP_ERROR_TEXT("clear OTA transaction failed: 0x%x"), result);
    }
    result = esp_ota_mark_app_invalid_rollback_and_reboot();
    mp_raise_msg_varg(&mp_type_OSError, MP_ERROR_TEXT("rollback failed: 0x%x"), result);
}
static MP_DEFINE_CONST_FUN_OBJ_0(nao_ota_rollback_and_reboot_obj, nao_ota_rollback_and_reboot);

static const mp_rom_map_elem_t nao_ota_module_globals_table[] = {
    {MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_nao_ota)},
    {MP_ROM_QSTR(MP_QSTR_verify_manifest), MP_ROM_PTR(&nao_ota_verify_manifest_obj)},
    {MP_ROM_QSTR(MP_QSTR_begin), MP_ROM_PTR(&nao_ota_begin_obj)},
    {MP_ROM_QSTR(MP_QSTR_write), MP_ROM_PTR(&nao_ota_write_obj)},
    {MP_ROM_QSTR(MP_QSTR_finish), MP_ROM_PTR(&nao_ota_finish_obj)},
    {MP_ROM_QSTR(MP_QSTR_activate), MP_ROM_PTR(&nao_ota_activate_obj)},
    {MP_ROM_QSTR(MP_QSTR_abort), MP_ROM_PTR(&nao_ota_abort_obj)},
    {MP_ROM_QSTR(MP_QSTR_status), MP_ROM_PTR(&nao_ota_status_obj)},
    {MP_ROM_QSTR(MP_QSTR_pending_verify), MP_ROM_PTR(&nao_ota_pending_verify_obj)},
    {MP_ROM_QSTR(MP_QSTR_current_sequence), MP_ROM_PTR(&nao_ota_current_sequence_obj)},
    {MP_ROM_QSTR(MP_QSTR_pending_sequence), MP_ROM_PTR(&nao_ota_pending_sequence_obj)},
    {MP_ROM_QSTR(MP_QSTR_phase), MP_ROM_PTR(&nao_ota_phase_obj)},
    {MP_ROM_QSTR(MP_QSTR_mark_healthy), MP_ROM_PTR(&nao_ota_mark_healthy_obj)},
    {MP_ROM_QSTR(MP_QSTR_rollback_and_reboot), MP_ROM_PTR(&nao_ota_rollback_and_reboot_obj)},
};
static MP_DEFINE_CONST_DICT(nao_ota_module_globals, nao_ota_module_globals_table);

const mp_obj_module_t nao_ota_user_cmodule = {
    .base = {&mp_type_module},
    .globals = (mp_obj_dict_t *)&nao_ota_module_globals,
};

MP_REGISTER_MODULE(MP_QSTR_nao_ota, nao_ota_user_cmodule);
