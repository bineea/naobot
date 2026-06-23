# 系统架构

## 总览

naobot 分为宿主机、ESP32 固件、Dashboard、模拟器四部分。

```text
Dashboard <-> FastAPI Host <-> WebSocket <-> ESP32 Firmware
                         ^
                         |
                    CPython Simulator
```

## 宿主机

宿主机代码位于 `src/naobot/`，运行在 Python 3.11 环境。

职责：

- 提供 FastAPI Dashboard 和 WebSocket 服务。
- 接收机器人事件并维护状态。
- 调用 OpenAI-compatible LLM；未配置或失败时使用规则 fallback。
- 执行动作白名单和 `PolicyGuard`。
- 管理 Soul、Memory、Routine。
- 提供 CLI 和 CPython 模拟器。

宿主机不得直接暴露舵机角度、PWM 或任意硬件控制入口。

## ESP32 固件

固件代码位于 `firmware/esp32/`，运行在 MicroPython 环境。

职责：

- 连接 WiFi 和宿主机 WebSocket。
- 采集触摸、姿态、电池等事件。
- 接收并执行宿主机下发的白名单动作。
- 执行本地安全守卫、失联降级、低电和跌倒保护。

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

