# 开发与验收说明

## 软件 MVP 验收

- `.\.venv\Scripts\python.exe -m pytest` 通过。
- `.\.venv\Scripts\python.exe -m ruff check .` 通过。
- `.\.venv\Scripts\naobot.exe serve` 可启动 Dashboard。
- `.\.venv\Scripts\naobot.exe simulate --event touch_head` 可完成 event -> intent -> ack。
- 未配置 LLM 时显示规则模拟状态。
- AgentScope Brain 状态可观察：`runtime=agentscope-2.0.4`，未配置、超时、异常或非法输出时进入 fallback。
- Dashboard 可编辑 Soul、查看 Memory、测试动作、执行急停。
- 固件目录包含可上传到 ESP32 的 MicroPython 骨架和 README。

## 安全边界

- Agent 输出必须经过 `PolicyGuard`。
- L3 AgentScope Brain 只能输出 `goal`、`text`、`expression`、`skills`、`memory_suggestion`；L2 `BehaviorRuntime` 确定性生成兼容 actions，并忽略 LLM `actions`。
- AgentScope `Toolkit` 必须为空，不允许 shell、文件、Python、MCP、硬件工具调用或自主长期记忆。
- 未配置模型、4 秒超时、运行异常、非法输出均必须 fallback，且 fallback 仍经过 `PolicyGuard`。
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
- 校验脚本为 `tools/check_prd_sync.py`，可手动运行：`.\.venv\Scripts\python.exe tools/check_prd_sync.py`。

## 手动联调

1. 启动服务：`.\.venv\Scripts\naobot.exe serve`。
2. 打开 Dashboard。
3. 运行：`.\.venv\Scripts\naobot.exe simulate --event touch_head`。
4. 在 Dashboard 查看日志和最后 intent。
5. 执行急停，确认日志出现 stop intent。

## AgentScope Brain 运行约束

- 依赖版本固定为 `agentscope==2.0.4`。
- OpenAI-compatible 配置仍使用 `NAOBOT_LLM_BASE_URL`、`NAOBOT_LLM_MODEL`、`NAOBOT_LLM_API_KEY`。
- 默认 `NAOBOT_BRAIN_TIMEOUT_SECONDS=4.0`，`NAOBOT_BRAIN_MAX_ITERS=4`。
- 复杂请求最多 3 个专家 agent 加产品负责人 agent 收敛；安全事件不组队。
- Host 事件队列默认容量 32，高优先级优先；Host heartbeat 默认每 2 秒发送，独立于推理。
- 软件模拟器验证不等同于真实硬件验证；真实 ESP32/舵机/传感器结果必须在硬件 bring-up 中另行记录。

## ESP32 烧录

项目已有 `data/ESP32_GENERIC_S3-20260406-v1.28.0.bin`。烧录前确认端口：

```powershell
esptool.py --chip esp32s3 --port COM3 erase_flash
esptool.py --chip esp32s3 --port COM3 --baud 460800 write_flash -z 0x0 data/ESP32_GENERIC_S3-20260406-v1.28.0.bin
```

上传固件源码见 `firmware/esp32/README.md`。
