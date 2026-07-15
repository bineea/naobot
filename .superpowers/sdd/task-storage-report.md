# XIAO Sense microSD 存储层报告

## 实现范围

- 新增 `firmware/esp32/storage/`：`SDStorage` 独占 `machine.SDCard(slot=2, ...)` 的创建、挂载、卸载和文件访问；挂载延迟执行且已挂载时幂等。
- 使用 XIAO Sense 已确认引脚：CS=GPIO3、SCK=GPIO7、MISO=GPIO8、MOSI=GPIO9；FAT 挂载点为 `/sd`。
- 缺卡、挂载、日志和更新读取错误均转换为状态错误，不向主安全循环抛出异常。
- 仅接受结构化诊断记录，写入紧凑 JSONL；递归拒绝 `bytes`、`bytearray` 和 `memoryview`，不保存 JPEG 或 PCM。
- 实现 128 KiB `naobot.log` 轮转和至多 7 个归档的确定性保留策略。
- 更新文件仅可从 `/sd/updates/<sequence>/` 读取；拒绝绝对路径、`..`、正反斜杠和目录逃逸，单次读取由配置的块大小限制。
- 新增有界协作式 `StorageWorker`；日志队列满时计数丢弃，更新请求返回明确的 `storage queue full` 拒绝。
- `main.py` 创建并轮询工作器，心跳和状态载荷包含 SD 可用性、挂载状态、队列深度、丢弃数和最近错误。未实现 OTA 安装。

## TDD 记录

1. 先新增 `tests/test_firmware_storage.py`。
2. RED：`pytest tests/test_firmware_storage.py -q` 在生产模块不存在时以 `ModuleNotFoundError: No module named 'storage'` 失败。
3. GREEN：新增存储实现与最小主程序集成后，聚焦测试通过。

## 验证

- `./.venv/Scripts/python.exe -m pytest tests/test_firmware_storage.py tests/test_firmware_network.py -q`：48 passed。
- `./.venv/Scripts/python.exe -m ruff check firmware/esp32/storage firmware/esp32/main.py firmware/esp32/config.py tests/test_firmware_storage.py`：通过。
- `git diff --check`：通过。

## 边界与顾虑

- 完成的是 CPython fake 存储行为验证；没有 microSD 实卡、XIAO 板或定制 MicroPython 固件实测，不将其表述为硬件验收。
- 未改动现有的 `tests/test_firmware_native_media_modules.py` 修改和其他协作者的未跟踪内容。
