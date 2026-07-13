# 协议

## Envelope

宿主机、机器人和模拟器统一使用 JSON envelope。

字段：

- `type`：消息类型，允许 `event`、`intent`、`ack`、`status`、`error`、`heartbeat`。
- `id`：消息 ID。
- `seq`：连接内序号。
- `ts_ms`：毫秒时间戳。
- `session_id`：会话 ID。
- `priority`：0 到 10，数值越高越紧急。
- `deadline_ms`：可选，intent 过期时间窗口。
- `payload`：消息内容对象。

## event

机器人或模拟器上报外部事件。

示例：

```json
{
  "type": "event",
  "payload": {
    "name": "touch_head",
    "battery_pct": 80,
    "posture": "upright"
  }
}
```

## intent

宿主机下发可执行意图。兼容旧版 `payload.actions`，并支持新版语义字段：

- `goal`：高层目标描述，用于日志和可解释性。
- `expression`：参数化表情意图，由固件 renderer 限幅后绘制。
- `skills`：可请求的技能动作，仍需 Host `PolicyGuard` 和固件安全层双重校验。
- `actions`：兼容旧固件和 Dashboard 的白名单动作列表。

优先级规则：

- L3 AgentScope Brain 只允许输出 `goal`、`text`、`expression`、`skills`、`memory_suggestion`，不得输出 `actions`。
- L2 `BehaviorRuntime` 根据 `expression` 和 `skills` 确定性生成兼容 `actions`，并忽略任何 LLM 提供的 `actions`。
- 固件解析 intent 时，语义字段优先于兼容 `actions`；当同一 intent 同时包含 `skills`/`expression` 和 `actions` 时，不得重复执行兼容 `actions`。
- 现有 envelope 类型保持兼容，仍使用 `event`、`intent`、`ack`、`status`、`error`、`heartbeat`。

示例：

```json
{
  "type": "intent",
  "priority": 4,
  "payload": {
    "text": "我在呢，老板。",
    "goal": "开心地打招呼",
    "expression": {
      "emotion": "happy",
      "valence": 0.8,
      "arousal": 0.4,
      "eye_open": 0.75,
      "pupil_offset_x": 0.0,
      "blink_rate": 0.2,
      "duration_ms": 1200
    },
    "skills": [
      {"name": "wave", "args": {"level": 1}}
    ],
    "actions": [
      {"name": "set_face", "args": {"face": "happy"}},
      {"name": "wave", "args": {"level": 1}}
    ]
  }
}
```

## ack / error

机器人执行或拒绝 intent 后返回 ack；协议错误或本地安全拒绝返回 error。

Host 事件队列满时返回 `EVENT_QUEUE_FULL`；更高优先级事件替换待处理事件时，针对被替换事件返回 `EVENT_EVICTED`。两种错误都携带 `event_id`，固件不得把它们当作 intent 执行结果。

```json
{
  "type": "ack",
  "payload": {
    "intent_id": "manual_xxx",
    "status": "accepted"
  }
}
```

## status / heartbeat

`status` 用于上报机器人当前状态，`heartbeat` 用于保持连接和检测失联。Host 和固件都可以发送 `heartbeat`，通过 `payload.source` 区分。

固件状态应包含身体自治字段：

- `source`：固件心跳为 `firmware`，Host 心跳为 `host`。
- `uptime_ms`：固件启动后的毫秒计时。
- `control_authority`：`idle/host/skill/cerebellum/reflex/emergency`
- `reflex_state`：`none/fall_detected/recovering/recovered/low_battery/emergency_stop/fault`
- `motion_state`：当前运动调度状态。
- `last_reflex`：最近一次本地反射动作。
- `local_loop_ms`：固件主循环最近一次耗时。

Host 心跳 payload 包含 `source=host`、`host_ts_ms`、`agent_mode` 和 `last_intent_id`。固件收到 Host 心跳只刷新大脑在线时间，不执行动作、不回 ack。

Host heartbeat 默认每 2 秒发送，独立于 L3 推理和事件队列。事件处理使用容量 32 的有界优先级队列，高优先级事件优先处理；队列满时更高优先级事件可驱逐最低优先级事件，新的低优先级事件会被拒绝并返回错误。

## 禁止字段

外部协议不得出现裸硬件控制字段，包括但不限于 `raw`、`servo`、`angle`、`pwm`、`servo_id`、`current`、`torque`、`grip_force`、`pixels`、`framebuffer`。如需新增动作，必须先更新安全策略、测试和固件执行器。

