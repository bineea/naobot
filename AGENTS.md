# naobot AI Agent 工作入口

本文件是所有 AI agent 进入 naobot 项目前必须先读的入口文档。目标是让架构、开发、测试、评审、产品负责人等 agent 使用同一套事实源、边界和交付格式。

## 项目目标

naobot 是 KT2 LLM 桌面智能机器人软件 MVP。当前目标是让 KT2 具备宿主机 Agent、FastAPI Dashboard、ESP32 MicroPython 固件骨架、机器人模拟器、协议模型、安全白名单、记忆/routine 和自动化测试。

产品事实源见 `docs/product/prd.md`。

## 必读顺序

1. `docs/product/prd.md`：产品目标、用户价值、MVP 验收。
2. `docs/architecture/system-architecture.md`：宿主机、固件、Dashboard、模拟器边界。
3. `docs/architecture/safety-policy.md`：安全白名单、不做项、拒绝策略。
4. `docs/agents/roles.md`：不同 agent 的职责边界。
5. `docs/development.md`：本地启动、测试、固件上传说明。

## 目录职责

```text
src/naobot/          # 宿主机 Agent、Dashboard、协议、CLI、模拟器
firmware/esp32/      # ESP32 MicroPython 固件源码
tests/               # 自动化测试
docs/product/        # PRD、路线图、验收标准
docs/architecture/   # 架构、协议、安全策略
docs/agents/         # agent 分工、任务模板、交接模板、评审清单
docs/decisions/      # ADR 架构决策记录
data/                # MicroPython 固件 bin 等资料
```

## 常用命令

```powershell
python -m pip install -r requirements.txt
python -m pip install -e .
git config core.hooksPath .githooks
naobot serve
naobot simulate --event touch_head
pytest
ruff check .
```

## 安全红线

- 不实现 Blockly/Blockley 或儿童积木编程。
- 不实现 99 个游戏。
- 不实现自主桌边移动。
- 不允许 LLM 直接控制舵机角度、PWM、servo id 或裸硬件字段。
- 不允许未经确认写入长期记忆。
- 不允许 Dashboard 变成通用编程 IDE 或任意代码执行入口。
- 所有动作必须来自白名单，并经过 `PolicyGuard`。

## Agent 工作协议

- 接任务前使用 `docs/agents/task-template.md` 明确目标、范围、输入、输出和验收。
- 修改完成后按 `docs/agents/handoff-template.md` 写交接说明。
- 评审时按 `docs/agents/review-checklist.md` 优先检查安全边界、测试和文档一致性。
- 如果 PRD、架构和代码冲突，以 PRD 表达产品意图，以安全策略表达不可突破边界，再更新对应文档消除冲突。
- 代码变更必须同步暂存 `docs/product/prd.md`。本地 Git hook 由 `git config core.hooksPath .githooks` 启用。
