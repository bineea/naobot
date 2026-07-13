# 路线图

## Phase 0：软件 MVP

- Host Agent、FastAPI Dashboard、模拟器、协议模型、安全策略、Memory、Routine、固件骨架可用。
- AgentScope Brain 运行时可用：`agentscope==2.0.4`、OpenAI-compatible 模型、空 Toolkit、4 秒超时、4 轮上限、fallback 可观察。
- L2 BehaviorRuntime 可用：确定性编译 `goal/text/expression/skills/memory_suggestion`，忽略 LLM `actions`，通过 `PolicyGuard` 生成兼容 actions。
- WebSocket 接入有界优先级事件队列，默认容量 32；Host heartbeat 每 2 秒独立于推理发送。
- 通过自动化测试验证核心安全边界。
- 不要求真实 ESP32 和舵机现场验证。

## Phase 1：硬件 bring-up

- 确认 ESP32 型号、引脚、屏幕、触摸、电源检测、IMU、舵机接线。
- 将固件 stub 替换为真实驱动。
- 完成安全姿态、急停、低电和跌倒降级实测。
- 验证语义字段优先于兼容 actions 的固件执行策略，避免同一 intent 在真实硬件上重复执行。

## Phase 2：互动体验

- 扩展事件种类和 Soul 表达。
- 增加可解释的 routine 推荐和确认流程。
- 优化 Dashboard 的状态可视化与诊断日志。

## Phase 1A-1D：分层自治控制

- Phase 1A：反射安全层，固件本地处理跌倒、低电、急停和 IMU fault。
- Phase 1B：可中断运动控制，运动动作支持 tick/cancel 和安全抢占。
- Phase 1C：参数化表情，Host 输出 expression，固件 renderer 负责限幅绘制。
- Phase 1D：Host 语义行为层，LLM 输出 goal + expression + skills，并保留 actions 兼容。

## Phase 3：多 agent 工程化

- 使用 `docs/agents/` 模板让架构、开发、测试、评审 agent 并行协作。
- 对每个功能建立 PRD -> 任务 -> 测试 -> 评审 -> ADR 的闭环。
- 增加文档一致性检查和安全回归检查。

