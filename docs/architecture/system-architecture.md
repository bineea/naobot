# 系统架构

## 总览

naobot 分为宿主机、ESP32 固件、Dashboard、模拟器四部分。

```text
Dashboard <-> FastAPI Host <-> WebSocket <-> ESP32 Firmware
                         ^
                         |
                    CPython Simulator
```

控制层级：

```text
L3 Host/LLM 语义意图层：AgentScope Brain 决定 goal、文本、表情意图、技能请求和待确认记忆建议。
L2 行为/技能层：BehaviorRuntime 把语义 intent 确定性转成安全技能和兼容 actions。
L1 固件运动协调层：tick/cancel 动作，负责运动中断和姿态保护。
L0 固件反射安全层：急停、低电、跌倒、IMU fault，本地最高优先级。
```

## 宿主机

宿主机代码位于 `src/naobot/`，运行在 Python 3.11 环境。

职责：

- 提供 FastAPI Dashboard 和 WebSocket 服务。
- 接收机器人事件并维护状态。
- 通过 `agentscope==2.0.4` Agent 调用 OpenAI-compatible 模型；AgentScope `Toolkit` 为空。
- L3 仅接受和输出语义字段：`goal`、`text`、`expression`、`skills`、`memory_suggestion`。
- 未配置模型、推理超时、运行异常或非法输出时使用规则 fallback，并在状态中暴露 `mode` 和 `last_error`。
- L2 `BehaviorRuntime` 确定性编译语义 intent，忽略 LLM 自行返回的 `actions`，再执行技能、表情和兼容动作的 `PolicyGuard`。
- 对复杂请求最多启动 3 个专家 agent，并由产品负责人 agent 收敛；安全事件不启用组队。
- 使用容量 32 的有界优先级事件队列，高优先级优先，同优先级 FIFO；队列满时低优先级事件可被更高优先级事件驱逐。
- Host heartbeat 每 2 秒独立于大脑推理发送。
- 管理 Soul、Memory、Routine。
- 提供 CLI 和 CPython 模拟器。

宿主机不得直接暴露舵机角度、PWM、shell、文件、Python、MCP、硬件工具或任意底层硬件控制入口，也不得自主写入长期记忆。

默认运行参数：

- `NAOBOT_BRAIN_TIMEOUT_SECONDS=4.0`
- `NAOBOT_BRAIN_MAX_ITERS=4`
- `NAOBOT_EVENT_QUEUE_CAPACITY=32`
- `NAOBOT_HOST_HEARTBEAT_INTERVAL_MS=2000`

## ESP32 固件

固件代码位于 `firmware/esp32/`，运行在 MicroPython 环境。

职责：

- 连接 WiFi 和宿主机 WebSocket。
- 采集触摸、姿态、电池等事件。
- 接收宿主机下发的语义 intent 或兼容 actions；当 `skills`/`expression` 等语义字段存在时优先执行语义字段，避免再重复执行兼容 `actions`。
- 执行本地反射安全、可中断运动调度、失联降级、低电和跌倒保护。

固件不做 LLM、长期记忆、Dashboard、Blockly 或通用编程能力。

## Dashboard

Dashboard 是运维和调试界面，不是编程 IDE。

职责：

- 展示机器人状态、LLM 状态、日志和最近 intent。
- 提供白名单动作测试和全局急停。
- 管理 Soul、Memory、Routine。
- 辅助诊断 WebSocket 和 Agent 行为。

## 模拟器

模拟器运行在 CPython 上，用于无硬件开发。

职责：

- 连接 `/ws/kt2`。
- 发送 `touch_head`、`fall_detected`、`battery_low` 等事件。
- 接收 intent 并回传 ack 或 error。

模拟器用于软件闭环，不等同于真实硬件验证。

