# ADR-0003：AgentScope Brain 运行时

## 状态

Accepted

## 背景

Host 需要可配置的语义大脑、人员上下文和复杂请求协作，但可执行动作与硬件安全不能依赖 LLM 自律。自然交互还要求已识别人员可恢复上下文、访客不留持久状态，并让媒体与控制故障彼此隔离。

## 决策

- L3 使用 `agentscope==2.0.4` Agent 和 OpenAI-compatible 模型，只输出 `goal`、`text`、`expression`、`skills`、`memory_suggestion`、`confidence`、`needs_team`、`escalation_reason`。
- L3 不输出 `actions`、裸硬件字段、代码或工具调用；Toolkit 为空，`parallel_tool_calls=False`，ReAct 最多 4 轮。
- L2 `BehaviorRuntime` 忽略 LLM `actions`，确定性生成兼容动作并通过 `PolicyGuard`。
- 路由评分 `score >= 4` 自动进入团队；单 Agent 的 `needs_team=true` 或 `confidence < 0.65` 触发自升级。
- 团队固定为情绪、行为、安全三位专家并行建议，由产品负责人收敛；安全事件使用确定性 fallback，禁止组队。
- 单 Agent 默认 6 秒；团队从专家到负责人共享 15 秒总预算。未配置、超时、异常、非法 JSON 或 schema 不合法统一 fallback。
- 已识别人员按 person/agent role 使用 SQLite WAL 持久化 AgentScope state；访客 runtime 仅在内存，连接结束时销毁。
- 持久化前清洗原始媒体；人脸 embedding 与 5 张注册样本用 Fernet 加密，但数据库整体不加密。
- Host 事件队列容量 32；Host heartbeat 每 2 秒独立于推理。
- 媒体 `/ws/media` 与控制 `/ws/kt2` 分离，媒体错误不能改变控制权或覆盖固件反射。

## 影响

- 配置使用 `NAOBOT_BRAIN_SINGLE_TIMEOUT_SECONDS=6.0` 与 `NAOBOT_BRAIN_TEAM_TIMEOUT_SECONDS=15.0`；旧 `NAOBOT_BRAIN_TIMEOUT_SECONDS` 仅作为单 Agent 兼容回退。
- 新技能必须同步 PRD、安全、协议、L2、固件与测试；给 Agent 增加工具或自主记忆需要新 ADR 和安全评审。
- People 删除可级联清理 session、runtime、embedding 和样本；People API 必须鉴权。
- 本 ADR 只描述软件运行时，不代表 N16R8 C 编译、真实 bin、摄像头/I2S/PSRAM/CH343 或 30 分钟硬件验收。
