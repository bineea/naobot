# 代码评审清单

## 安全边界

- 是否只允许白名单动作。
- 是否拒绝裸舵机角度、PWM、servo id 和危险关键词。
- 是否所有 LLM 输出都经过 `PolicyGuard`。
- 是否 L3 只输出 `goal/text/expression/skills/memory_suggestion`，且 LLM `actions` 被拒绝或忽略。
- 是否 AgentScope `Toolkit` 为空，没有 shell、文件、Python、MCP、硬件工具或自主长期记忆入口。
- 是否未配置、超时、异常、非法输出都 fallback，且 fallback 不绕过 `PolicyGuard`。
- 是否低电、fault、异常姿态下拒绝位移动作。
- 是否保持未确认 Memory 和 Routine 不自动生效。

## 不做项回归

- 是否引入 Blockly/Blockley 或儿童积木编程。
- 是否引入 99 个游戏。
- 是否引入自主桌边移动。
- 是否让 Dashboard 变成编程 IDE。
- 是否让固件承担 LLM 或长期记忆职责。

## 测试

- 是否覆盖正常路径和拒绝路径。
- 是否覆盖 WebSocket event -> intent -> ack。
- 是否覆盖 LLM fallback。
- 是否覆盖 AgentScope Brain 4 秒/4 轮、复杂请求三专家加产品负责人、安全事件禁用组队、有界优先级队列和 Host heartbeat 独立发送。
- 是否覆盖 API 错误响应。

## 文档

- README、PRD、架构、安全策略是否一致。
- 新增能力是否更新验收标准。
- 跨模块决策是否需要 ADR。
- 是否避免把模拟器或固件骨架测试表述为真实硬件已验证。

## 评审输出格式

1. 按严重程度列出问题。
2. 每个问题给出文件、风险、复现方式或依据、建议修复。
3. 没有发现问题时，说明测试缺口和评审范围限制。

