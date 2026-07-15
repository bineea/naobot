# Native Media Review Fix Report

## Status

DONE_WITH_CONCERNS

本次整改仅修改 XIAO 原生媒体构建、PDM/Camera C 模块和对应静态约束测试。未修改
`storage`、`main.py` 或 `docs`。

## Fixed Findings

1. 将 MicroPython `v1.28.0` annotated tag object
   `2b0015629f67fd186f980079b2e696ad0bc7343c` 与实际 commit
   `e0e9fbb17ed6fd06bb76e266ae554784c9c80804` 分开固定和校验；camera commit 保持
   `2ac69a6f1749694804f5196e63fa1f79800b74bf`。
2. 将 PDM 单次 pump 限制为 256 个偶数字节，并使用 1 FreeRTOS tick 的有界等待；
   `ESP_ERR_TIMEOUT` 与 `ESP_OK` 共用 received 处理路径，因此 partial bytes 不再丢失。
3. PDM 临时 heap 和 Camera framebuffer 均使用 MicroPython NLR 边界保护；
   `mp_obj_new_bytes` 抛异常时先释放或归还资源，再重新抛出原异常。
4. PDM 成功 deinit 后清空 creator，允许后续 worker task 重新 init。
5. PDM release 仅在 `i2s_del_channel` 成功后清 handle 和释放 buffer；disable/delete
   失败保留可重试状态。init failure 也检查 cleanup 结果，cleanup 失败时保留 owner 和资源。

## TDD Evidence

- RED：`tests/test_firmware_native_media_modules.py` 新增约束后出现 6 个预期失败。
- GREEN：同一测试文件修复后 24 项通过。
- 聚焦回归：原生模块、XIAO 构建与 Camera/PDM 媒体用例共 38 项通过。

## Verification

```text
.\.venv\Scripts\python.exe -m pytest tests/test_firmware_native_media_modules.py tests/test_xiao_board_build.py tests/test_firmware_media.py -k "native_media_modules or xiao_board_build or camera or pdm" -q
38 passed

.\.venv\Scripts\python.exe -m ruff check tests/test_firmware_native_media_modules.py
All checks passed!

PowerShell parser: firmware/esp32/build/build.ps1
PowerShell parse OK

git diff --check
exit 0
```

## Remaining Concern

本机未提供 `idf.py` 和 `make`，因此未运行真实 MicroPython/ESP-IDF C 编译，也未验证固件产物
或 XIAO 硬件行为。当前 C 结论来自源码审查和静态约束测试，不能替代真实编译与板上验证。
