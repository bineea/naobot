# Agent 角色分工

## Product Owner Agent

- 维护 `docs/product/prd.md`、路线图和验收标准。
- 收敛各角色观点，确认功能是否符合 MVP 范围。
- 拒绝把不做项重新引入项目。

## Architecture Agent

- 维护宿主机、固件、Dashboard、模拟器和协议边界。
- 对跨模块改动提出 ADR。
- 审查新能力是否破坏安全策略。

## Host Development Agent

- 负责 `src/naobot/` 中的 Agent、LLM、Memory、Routine、Policy、CLI 和模拟器。
- 不修改 `firmware/esp32/`，除非任务明确要求联动协议。

## Firmware Development Agent

- 负责 `firmware/esp32/` 中的 MicroPython 固件、硬件抽象、本地降级和动作执行。
- 不把 LLM、长期记忆或 Dashboard 逻辑放入固件。

## Dashboard Development Agent

- 负责 Dashboard 页面、API 交互和实时日志。
- 不把 Dashboard 做成编程 IDE。

## Test Agent

- 负责单元测试、API 测试、WebSocket 端到端、安全回归测试。
- 新功能必须有正向和拒绝路径测试。

## Code Review Agent

- 先列风险和问题，再给总结。
- 重点审查安全边界、未测试失败路径、文档不一致和不做项回归。

