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

## 评审修复

- `StorageWorker` 改为独立 `_thread` 的唯一运行时所有者。主循环只调用 `start()`、`stop()` 与纯内存 `snapshot()`；不再调用会执行 SD I/O 的 `tick()`。
- 工作器将入队、结果 `poll()` 与快照都限制为锁保护的有界内存交换；物理挂载、`stat`、读写、JSON 序列化和轮转只发生在工作线程的 `_run_one()` 路径。
- `SDStorage.snapshot()` 改为纯内存读取。挂载和日志写入期间维护 `log_bytes` 缓存，日志使用 UTF-8 字节数计量，拒绝超过活动日志上限的单条记录，确保活动 `naobot.log` 不超过 128 KiB。
- 挂载和运行时 I/O 错误统一调用失效清理：尽力 `umount`、`deinit`，清除卡、挂载和可用状态，并以配置的指数退避限制重试；后续请求在退避到期后可重新挂载。
- 递归诊断校验增加最大深度和活动引用集合，明确拒绝二进制、循环引用及过深对象图。
- 新增阻塞存储 fake，证明安全主循环侧的 `snapshot()`、`tick()` 和 `poll()` 不会触发存储操作，实际阻塞操作仅在线程启动后发生。

## 评审修复 TDD 与验证

1. RED：新增回归测试后，旧实现在 UTF-8 字节限额、超大日志、循环引用、失效退避和线程接口上共失败 7 项。
2. GREEN：线程所有权、缓存快照和失效状态机实现后，存储测试恢复通过。
3. `./.venv/Scripts/python.exe -m pytest tests/test_firmware_storage.py tests/test_firmware_network.py -q`：54 passed。
4. `./.venv/Scripts/python.exe -m ruff check firmware/esp32/storage firmware/esp32/main.py firmware/esp32/config.py tests/test_firmware_storage.py`：通过。
5. `./.venv/Scripts/python.exe -m py_compile firmware/esp32/storage/__init__.py firmware/esp32/storage/sd_storage.py firmware/esp32/storage/storage_worker.py firmware/esp32/main.py firmware/esp32/config.py tests/test_firmware_storage.py`：通过。
6. `git diff --check`：通过。

## 并发发布复审修复

- 统一工作线程错误发布：日志失败、读取返回失败、读取异常和 shutdown `unmount()` 异常先保存为局部值，再由 `_publish_locked()` 在持锁状态下发布。
- `_publish_item_result()` 与 `_publish_storage_snapshot()` 在同一临界区内更新 storage snapshot、`last_error`、`runtime_state` 和读取请求结果，主线程只能观察到完整快照。
- 新增带所有权检测的锁替身与工作器子类，分别覆盖日志失败、读取失败、读取异常和 finally/unmount 异常；任何已初始化的 `_last_error` 未持锁写入都会使测试失败。

## 并发发布验证

1. RED：锁检测测试在旧实现中稳定捕获日志失败、读取异常与 finally/unmount 异常路径的未持锁 `_last_error` 写入。
2. GREEN：`./.venv/Scripts/python.exe -m pytest tests/test_firmware_storage.py -q`：22 passed。
3. `./.venv/Scripts/python.exe -m ruff check firmware/esp32/storage/storage_worker.py tests/test_firmware_storage.py`：通过。
4. `./.venv/Scripts/python.exe -m py_compile firmware/esp32/storage/storage_worker.py tests/test_firmware_storage.py`：通过。
5. `git diff --check`：通过。
