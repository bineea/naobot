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

宿主机下发可执行意图。`payload.actions` 必须只包含安全白名单动作。

示例：

```json
{
  "type": "intent",
  "priority": 4,
  "payload": {
    "text": "我在呢，老板。",
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

`status` 用于上报机器人当前状态，`heartbeat` 用于保持连接和检测失联。

## 禁止字段

外部协议不得出现裸硬件控制字段，包括但不限于 `raw`、`servo`、`angle`、`pwm`、`servo_id`。如需新增动作，必须先更新安全策略、测试和固件执行器。

