# 协议

## 控制 WebSocket `/ws/kt2`

控制链路只使用 JSON envelope，不承载二进制音视频。字段如下：

- `type`：`event`、`intent`、`ack`、`status`、`error`、`heartbeat`。
- `id`：消息 ID。
- `seq`：连接内序号。
- `ts_ms`：毫秒时间戳。
- `session_id`：会话 ID。
- `priority`：0-10，数值越高越紧急。
- `deadline_ms`：可选的 intent 有效窗口。
- `payload`：消息内容对象。

`intent.payload` 可包含 `goal`、`text`、`expression`、`skills`、`memory_suggestion` 和 L2 生成的兼容 `actions`。L3 不得输出 `actions`；L2 忽略任何 LLM `actions` 并确定性生成兼容字段。固件检测到 `skills` 或 `expression` 时不再执行兼容 `actions`。

```json
{
  "type": "intent",
  "id": "intent_xxx",
  "seq": 7,
  "ts_ms": 1770000000000,
  "session_id": "person_xxx",
  "priority": 4,
  "deadline_ms": 5000,
  "payload": {
    "goal": "友好回应",
    "text": "我在呢。",
    "expression": {"emotion": "happy", "valence": 0.8},
    "skills": [{"name": "wave", "args": {"level": 1}}],
    "actions": [{"name": "set_expression", "args": {"emotion": "happy", "valence": 0.8}}, {"name": "wave", "args": {"level": 1}}]
  }
}
```

机器人执行或拒绝 intent 后返回 `ack` 或 `error`。Host 事件队列拒绝新事件时返回 `EVENT_QUEUE_FULL`；较高优先级事件驱逐待处理事件时，对被驱逐事件返回 `EVENT_EVICTED`，两者都携带 `event_id`，不能当作 intent 执行结果。

## 媒体 WebSocket `/ws/media`

媒体 socket 建立后，设备的第一条消息必须是 JSON `media_hello`：

```json
{
  "kind": "media_hello",
  "device_id": "kt2-esp32-s3",
  "token": "device-token",
  "boot_id": "boot-1234",
  "capabilities": {
    "video": {"nominal_fps": 10, "event_fps": 15, "resolution": {"width": 320, "height": 240}},
    "audio": {"format": {"sample_rate_hz": 16000, "channels": 1, "encoding": "pcm16"}},
    "image": {"encoding": "jpeg"}
  }
}
```

配置 `NAOBOT_DEVICE_TOKEN` 时，`token` 必须恒定时间比较通过；hello 非对象、字段缺失、token 错误或 hello 非 JSON 时，Host 以 WebSocket policy violation `1008` 关闭。成功后 Host 返回 `{"kind":"media_ready","boot_id":"..."}`。当前每个 Host 只保留一个媒体设备连接。

## 二进制媒体帧

每条应用层媒体消息由固定 24 字节大端帧头和 payload 组成。Python struct 格式必须精确为 `>4sBBHIQI`：

| 顺序 | 字段 | struct | 字节 | 约束 |
| --- | --- | --- | --- | --- |
| 1 | `magic` | `4s` | 4 | 固定 `NABM` |
| 2 | `version` | `B` | 1 | 当前为 `1` |
| 3 | `kind` | `B` | 1 | 见下表 |
| 4 | `flags` | `H` | 2 | uint16 |
| 5 | `sequence` | `I` | 4 | uint32 |
| 6 | `timestamp_ms` | `Q` | 8 | uint64，设备相对时间可在重连后重置 |
| 7 | `payload_length` | `I` | 4 | uint32，且必须等于实际 payload 长度 |

kind 与 payload 上限：

| kind | 名称 | 方向 | 上限 |
| --- | --- | --- | --- |
| `1` | `AUDIO_PCM16` | 设备 -> Host | 64 KiB |
| `2` | `JPEG` | 设备 -> Host | 256 KiB |
| `3` | `TTS_PCM16` | Host -> 设备 | 256 KiB |

已定义 flags：`0x0001` 表示 speech，`0x0002` 表示 end-of-utterance，`0x0004` 表示 event boost。其余位当前保留，发送方应置 0；现有解码器只解释这三位，不应推断未知扩展语义。

magic、version、kind、长度、uint 范围或实际 payload 长度不合法时拒绝该媒体帧。媒体错误通过 `{"kind":"media_error","code":"...","message":"..."}` 返回，不转换为控制 envelope。

## TTS 与半双工

当前 Host 的一次 TTS 下行顺序是：

1. JSON `{"kind":"tts_start","text":"..."}`。
2. 一个 kind `3` 的 TTS PCM16 二进制媒体帧。
3. JSON `{"kind":"tts_end"}`。

固件在 `tts_start` 后清空旧 TTS 状态并暂停麦克风上传；摄像头仍按常态 10 FPS/事件窗口 15 FPS 继续上传。播放缓冲有上限和进度超时。只有收到 `tts_end` 且所有 PCM 已排空，固件才恢复麦克风；Host 的会话层在 TTS 完成后额外等待默认 200 ms 再恢复 listening。当前没有 AEC 和 barge-in。

## WebSocket 分片边界

固件媒体 client 能重组 Host 下行的标准 text/binary continuation 序列，允许分片之间穿插 ping，并回复 pong。重组后的单条消息上限为 256 KiB；非法 continuation 顺序、数据帧嵌套、RSV 位、服务端 masked 帧、超长消息或非法 control frame 会以 `1002`/`1009` 关闭。

TCP 每次最多发送 1 KiB 只是非阻塞发送切片，不等同于创建多个 WebSocket application message。当前实现不协商压缩、子协议或任意 RFC 扩展，不能据此声称完整支持所有 WebSocket 扩展。

## 媒体控制 JSON 与错误

设备可在媒体连接发送 `touch_head`、`enrollment_cancel` 和 `ping`；Host 分别处理注册/触摸、取消注册和返回 `pong`。非法 JSON、非对象或未知 kind 返回 `INVALID_CONTROL_JSON`/`INVALID_CONTROL_KIND`。

其他媒体错误包括 `INVALID_MEDIA_FRAME`、`MEDIA_QUEUE_FULL`、`INVALID_MEDIA_KIND`、`MEDIA_BACKEND_ERROR`、`MEDIA_WORKER_ERROR` 和 `TTS_ERROR`。坏媒体帧、媒体队列满或 provider 异常不得关闭控制 `/ws/kt2`，也不得改变固件反射控制权。

## status 与 heartbeat

Host heartbeat 默认每 2 秒独立发送，payload 包含 `source=host`、`host_ts_ms`、`agent_mode` 和 `last_intent_id`。固件只用它刷新大脑在线时间，不执行动作、不回 ack。

固件 status/heartbeat 至少包含：

- `source=firmware`、`uptime_ms`、`battery_pct`、`posture`、`agent_online`。
- `control_authority`、`reflex_state`、`motion_state`、`last_reflex`。
- `local_loop_ms`、`local_loop_interval_ms`、`local_loop_overrun_ms`。
- `camera_fps`、`audio_state`、`media_queue`、`media_dropped`、`psram_free`。

7 秒内没有 Host 任意消息或 heartbeat 时，固件标记离线、取消当前 Host skill 并保持本地反射。

## 禁止字段

外部控制协议不得出现 `raw`、`servo`、`angle`、`pwm`、`servo_id`、`current`、`torque`、`grip_force`、`pixels`、`framebuffer` 等裸硬件字段。新增动作必须同步安全策略、协议、L2 编译、固件执行器和测试。
