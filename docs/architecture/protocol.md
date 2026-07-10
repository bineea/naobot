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

## 禁止字段

外部协议不得出现裸硬件控制字段，包括但不限于 `raw`、`servo`、`angle`、`pwm`、`servo_id`、`current`、`torque`、`grip_force`、`pixels`、`framebuffer`。如需新增动作，必须先更新安全策略、测试和固件执行器。

