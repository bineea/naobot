# ADR-0001：项目边界

## 状态

Accepted

## 背景

naobot 需要在复刻 KT2 的同时引入 LLM 能力，但不能让 LLM 或 Dashboard 直接触碰危险硬件控制，也不能把 MVP 扩展成儿童编程平台或通用 IDE。

## 决策

- MVP 聚焦宿主机 Agent、Dashboard、模拟器、协议、安全策略、Memory、Routine 和 ESP32 固件骨架。
- 明确不做 Blockly、99 游戏、自主桌边移动、LLM 直接舵机角度。
- Dashboard 是机器人运维和配置界面，不是编程 IDE。

## 影响

- 新功能必须先对照 PRD 和安全策略。
- 任何越过边界的功能提议都需要新的 PRD 版本和 ADR。

