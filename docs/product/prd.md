# naobot PRD

## 背景

KT2 是一个口袋尺寸桌面机器人。naobot 的目标是在复刻 KT2 的基础上，引入 LLM 宿主机 Agent，让机器人从固定动作玩具升级为可配置、可观察、可安全扩展的智能机器人。

本 PRD 是项目唯一主 PRD。实现细节放在 `docs/architecture/`，开发流程放在 `docs/development.md`，agent 协作规则放在 `docs/agents/`。

## 目标用户

- 复刻 KT2 的个人开发者。
- 想用 LLM 扩展桌面机器人的 maker。
- 需要在真实硬件前先用模拟器验证行为的开发者。
- 使用 AI agent 协作推进功能开发、测试和评审的项目维护者。

## MVP 目标

- 宿主机运行 Agent 和 Dashboard，负责理解事件、调用 LLM 或规则 fallback、生成安全 intent。
- ESP32 固件负责事件采集、动作执行、本地安全守卫和失联降级。
- 模拟器可在没有真实硬件时完成 event -> intent -> ack 闭环。
- 动作、记忆、routine 均有安全边界和人工确认机制。
- 项目文档支持 AI-native 协作，agent 能根据统一入口继续拆解和实现功能。

## 核心能力

- Dashboard：查看状态、测试白名单动作、急停、配置 Soul、管理 Memory、管理 Routine、查看诊断日志。
- Host Agent：接收机器人事件，调用 OpenAI-compatible LLM；未配置 LLM 或调用失败时使用安全规则 fallback。
- 协议：支持 `event`、`intent`、`ack`、`status`、`error`、`heartbeat` envelope。
- 安全白名单：只允许 `set_face`、`blink`、`wave`、`small_step_forward`、`turn_left`、`turn_right`、`gentle_nudge`、`sit`、`chirp`、`sleep`、`stop`。
- Memory：长期记忆默认待确认，可查看、确认、删除。
- Routine：只允许白名单动作，默认待确认。
- Firmware：MicroPython 骨架包含硬件抽象、通信、本地降级、动作执行和安全守卫。

## 不做项

- 不做 Blockly/Blockley 或儿童积木编程。
- 不做 99 个游戏。
- 不做自主桌边移动。
- 不做 LLM 直接控制舵机角度、PWM、servo id 或裸硬件字段。
- 不做未经确认的长期记忆写入。
- 不把 Dashboard 做成通用编程 IDE。

## MVP 验收

- `pytest` 和 `ruff check .` 通过。
- `naobot serve` 可启动 Dashboard。
- `naobot simulate --event touch_head` 可完成 event -> intent -> ack。
- 未配置 LLM 时系统使用规则 fallback，并在 Dashboard 状态中可见。
- Dashboard 急停能生成并下发 `stop` intent。
- Memory 和 Routine 默认待确认。
- 固件目录包含可通过 `mpremote` 上传的 MicroPython 源码骨架和说明。

## 变更记录

- 2026-06-23：建立 AI-native 文档结构，确认 `docs/product/prd.md` 为唯一主 PRD；代码更新必须同步检查并暂存本文件。
- 2026-06-30：同步固件 demo 脚本（oled.py、scan_i2c.py、servo_180.py、servo_360.py）为示例/实验性实现，功能范围未改变，**无需需求变更**。
- 2026-06-30：新增 `firmware/esp32/demo/mpu6050_reader.py` 传感器读取示例（I2C/MPU6050 初始化与姿态数据获取），功能范围未改变，**无需需求变更**。
- 2026-06-30：主固件集成 SSD1306 OLED 显示和 MPU6050 姿态检测；MPU6050 缺失或读取失败按 `unknown` 姿态处理并禁止运动动作。
- 2026-07-08：主固件接入 WiFi 与明文 `ws://` WebSocket client，可上报事件、接收 intent、回传 ack/error；网络失败时继续本地 fallback。
- 2026-07-08：固件执行层补齐 Host 白名单动作与表情；动作执行失败返回 `error`，避免未执行动作被误认为成功。
- 2026-07-09：修复固件 MicroPython 兼容性问题，移除 `dataclasses` 依赖，并避免 WebSocket 随机数生成超过 `getrandbits(32)` 限制；功能范围未改变，**无需需求变更**。
