# naobot

naobot 是 KT2 LLM 桌面智能机器人软件 MVP。它实现宿主机 Agent、FastAPI Dashboard、ESP32 MicroPython 固件骨架、CPython 机器人模拟器、协议模型、安全白名单、记忆/routine 和自动化测试。

## 文档入口

- 产品事实源：`docs/product/prd.md`
- 阶段验收：`docs/product/acceptance.md`
- 系统架构：`docs/architecture/system-architecture.md`
- 协议说明：`docs/architecture/protocol.md`
- 安全策略：`docs/architecture/safety-policy.md`
- AI agent 入口：`AGENTS.md`
- Agent 分工与模板：`docs/agents/`

## 不做项

- 不做 Blockly/Blockley 或儿童积木编程：本项目聚焦 KT2 智能机器人能力，不把 Dashboard 做成儿童编程 IDE。
- 不做 99 个游戏：游戏属于后续内容/技能扩展，MVP 先验证事件、Agent、协议、安全动作和 Dashboard 闭环。
- 不做自主桌边移动：桌面机器人存在跌落风险，在没有真实硬件传感器和桌边检测闭环验证前，不开放自主移动能力。
- 不做 LLM 直接控制舵机角度：LLM 只能生成白名单动作 intent，不能输出裸舵机角度、PWM 或 servo id。
- 不做未经确认的长期记忆写入：Memory 默认待确认，避免误记、敏感信息或临时偏好被自动保存。

## 快速开始

```powershell
python -m pip install -r requirements.txt
python -m pip install -e .
naobot serve
```

打开 `http://127.0.0.1:8765` 查看 Dashboard。

模拟机器人事件：

```powershell
naobot simulate --event touch_head
naobot send-event --event battery_low
```

## LLM 配置

默认不需要真实模型，系统会启用安全规则模拟。若要使用 OpenAI-compatible API：

```powershell
$env:NAOBOT_LLM_BASE_URL="http://127.0.0.1:1234/v1"
$env:NAOBOT_LLM_MODEL="local-model"
$env:NAOBOT_LLM_API_KEY="optional"
```

## 测试

```powershell
pytest
ruff check .
```

## 目录

```text
src/naobot/          # 宿主机 Agent、Dashboard、协议、CLI、模拟器
firmware/esp32/      # MicroPython 固件骨架
tests/               # 自动化测试
docs/product/        # PRD、路线图、验收标准
docs/architecture/   # 架构、协议、安全策略
docs/agents/         # AI agent 分工、模板、评审清单
docs/decisions/      # ADR 架构决策记录
docs/development.md  # 开发与验收说明
data/                # MicroPython 固件 bin 等资料
```
