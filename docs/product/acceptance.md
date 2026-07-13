# 验收标准

## 自动化验收

- `pytest` 全部通过。
- `ruff check .` 通过。
- 协议校验覆盖合法事件、非法消息、intent、ack、status、error。
- 安全回归覆盖未知动作、裸舵机字段、低电运动、过期 intent、未确认记忆。
- WebSocket 覆盖 `touch_head -> intent -> ack` 闭环。
- AgentScope Brain 覆盖流式语义输出解析、非法输出 fallback、超时 fallback、thinking 流忽略、空 Toolkit 和 4 轮上限。
- BehaviorRuntime 覆盖 L3 语义字段到兼容 actions 的确定性编译，并确认 LLM `actions` 被忽略。
- 有界优先级事件队列覆盖高优先级优先、同优先级 FIFO、容量 32 场景下低优先级驱逐或拒绝。
- 复杂请求覆盖最多 3 个专家 agent 加产品负责人收敛；安全事件覆盖禁用组队。

## 手动验收

- 运行 `naobot serve` 后 Dashboard 可访问。
- 运行 `naobot simulate --event touch_head` 后 Dashboard 显示机器人事件、Agent intent 和 ack。
- 修改 Soul 的名字或称呼后，下一次规则 fallback 回应随配置变化。
- Dashboard 急停生成 `stop` intent。
- 未配置 LLM 时系统仍可安全运行。
- 未配置模型、模型超时、运行异常或非法输出时，Dashboard/状态接口显示 fallback 模式和最近错误。
- Host heartbeat 每 2 秒独立发送，不因大脑推理阻塞机器人链路状态刷新。
- `firmware/esp32/README.md` 中的上传说明可作为 ESP32 bring-up 起点。
- 不把软件模拟器验收表述为真实硬件验收；真实 ESP32、舵机、传感器验证仍属于硬件 bring-up。

## 文档验收

- README 指向 PRD、架构、安全策略、agent 文档。
- `AGENTS.md` 说明 agent 必读顺序、目录职责、常用命令和安全红线。
- PRD、架构、安全策略中的不做项保持一致。
- AgentScope Brain、L2 BehaviorRuntime、PolicyGuard、协议兼容字段和固件执行优先级在产品、架构、安全、协议文档中保持一致。

