# 路线图

## Phase 0：软件 MVP

- Host Agent、FastAPI Dashboard、模拟器、协议模型、安全策略、Memory、Routine、固件骨架可用。
- 通过自动化测试验证核心安全边界。
- 不要求真实 ESP32 和舵机现场验证。

## Phase 1：硬件 bring-up

- 确认 ESP32 型号、引脚、屏幕、触摸、电源检测、IMU、舵机接线。
- 将固件 stub 替换为真实驱动。
- 完成安全姿态、急停、低电和跌倒降级实测。

## Phase 2：互动体验

- 扩展事件种类和 Soul 表达。
- 增加可解释的 routine 推荐和确认流程。
- 优化 Dashboard 的状态可视化与诊断日志。

## Phase 3：多 agent 工程化

- 使用 `docs/agents/` 模板让架构、开发、测试、评审 agent 并行协作。
- 对每个功能建立 PRD -> 任务 -> 测试 -> 评审 -> ADR 的闭环。
- 增加文档一致性检查和安全回归检查。

