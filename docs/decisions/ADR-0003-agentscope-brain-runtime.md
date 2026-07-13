# ADR-0003：AgentScope Brain 运行时

## 状态

Accepted

## 背景

naobot 需要让宿主机具备可配置的语义大脑，但机器人安全边界不能依赖 LLM 自律。L3 大脑适合生成目标、对话、表情和低风险技能建议；可执行动作、协议兼容字段和硬件保护必须由确定性代码、`PolicyGuard` 和固件安全层共同约束。

项目已引入 `agentscope==2.0.4`，并通过 AgentScope Agent 连接 OpenAI-compatible 模型。

## 决策

- L3 使用 AgentScope Brain Runtime，模型接口为 OpenAI-compatible。
- L3 输出契约仅允许 `goal`、`text`、`expression`、`skills`、`memory_suggestion`。
- L3 不允许输出 `actions`、裸硬件字段、代码、shell、文件、Python、MCP 或硬件工具调用。
- AgentScope `Toolkit` 保持为空，`parallel_tool_calls=False`，ReAct 默认最多 4 轮。
- 单次大脑推理默认 4 秒超时。
- 未配置模型、推理超时、运行异常、非法 JSON 或不符合 `LLMDecision` schema 时，统一降级到规则 fallback；通过 schema 但违反白名单或参数策略的语义由 L2/`PolicyGuard` 拒绝。
- fallback 只产生安全语义决策，仍必须经过 L2 和 `PolicyGuard`。
- L2 `BehaviorRuntime` 负责把语义字段确定性编译为兼容 `actions`，并忽略任何 LLM `actions`。
- 复杂请求最多启用 3 个专家 agent，并由产品负责人 agent 收敛为一个最终 JSON 决策。
- 安全事件禁用多 agent 组队，优先降低延迟、冲突和不可控输出。
- Host 使用容量 32 的有界优先级事件队列；Host heartbeat 每 2 秒独立于推理发送。
- 固件执行 intent 时语义字段优先于兼容 `actions`，避免同一行为重复执行。

## 影响

- 协议 envelope 类型保持兼容，继续使用 `event`、`intent`、`ack`、`status`、`error`、`heartbeat`。
- 新固件应优先解析 `skills` 和 `expression`，旧固件仍可消费 L2 生成的兼容 `actions`。
- 新增技能必须同时更新 PRD、安全策略、协议、L2 编译规则、固件执行器和测试。
- 任何给 AgentScope Brain 增加工具、长期自主记忆或裸硬件访问的提议，都需要新的 ADR 和安全评审。
- 当前决策只说明软件运行时边界，不宣称真实 ESP32、舵机或传感器已验证。
