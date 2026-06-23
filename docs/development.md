# 开发与验收说明

## 软件 MVP 验收

- `pytest` 通过。
- `ruff check .` 通过。
- `naobot serve` 可启动 Dashboard。
- `naobot simulate --event touch_head` 可完成 event -> intent -> ack。
- 未配置 LLM 时显示规则模拟状态。
- Dashboard 可编辑 Soul、查看 Memory、测试动作、执行急停。
- 固件目录包含可上传到 ESP32 的 MicroPython 骨架和 README。

## 安全边界

- Agent 输出必须经过 `PolicyGuard`。
- 未知动作、裸舵机角度、低电运动、过期 intent 均拒绝。
- 长期记忆默认待确认。
- Routine 默认待确认，且只允许白名单动作。
- Dashboard 不是编程 IDE，不暴露任意代码执行入口。

## Git hooks

项目提供可追踪的本地 Git hook，要求代码更新时同步更新并暂存 `docs/product/prd.md`。

启用：

```powershell
git config core.hooksPath .githooks
```

规则：

- 暂存 `src/`、`firmware/`、`tests/`、`pyproject.toml`、`requirements.txt` 时，必须同时暂存 `docs/product/prd.md`。
- 如果代码变更不影响产品需求，也需要在 PRD 的变更记录中说明“无需需求变更”的原因。
- 校验脚本为 `tools/check_prd_sync.py`，可手动运行：`python tools/check_prd_sync.py`。

## 手动联调

1. 启动服务：`naobot serve`。
2. 打开 Dashboard。
3. 运行：`naobot simulate --event touch_head`。
4. 在 Dashboard 查看日志和最后 intent。
5. 执行急停，确认日志出现 stop intent。

## ESP32 烧录

项目已有 `data/ESP32_GENERIC_S3-20260406-v1.28.0.bin`。烧录前确认端口：

```powershell
esptool.py --chip esp32s3 --port COM3 erase_flash
esptool.py --chip esp32s3 --port COM3 --baud 460800 write_flash -z 0x0 data/ESP32_GENERIC_S3-20260406-v1.28.0.bin
```

上传固件源码见 `firmware/esp32/README.md`。
