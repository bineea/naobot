# ADR-0002：宿主机与固件分离

## 状态

Accepted

## 背景

`src/naobot/` 运行在 CPython Host，`firmware/esp32/` 运行在 ESP32 MicroPython。两端依赖、部署方式、实时性和安全职责不同；媒体流量不能阻塞控制与反射。

## 决策

- Host 负责 AgentScope、BehaviorRuntime、Dashboard、People/身份、会话、SQLite runtime、媒体 provider、协议、CLI 和模拟器。
- 固件负责事件采集、媒体设备、动作执行、连接 client、本地降级、运动协调与反射，不承担 LLM 或长期记忆。
- `/ws/kt2` 使用 JSON envelope 传输控制；`/ws/media` 使用 hello/token、媒体控制 JSON 和带 24 字节帧头的二进制音视频。
- 控制与媒体在固件中使用独立连接、队列、重连状态和 `_thread` connection worker；媒体异常不覆盖反射。
- 固件不进入 Host Python package，也不使用 Host `requirements.txt` 作为板上依赖。

## 影响

- Host 通过 `pip install -e .` 运行；本地媒体可选依赖使用 `pip install -e ".[media-local]"`。
- 固件源码通过 `mpremote` 上传；OV2640 需要定制 MicroPython `camera` 模块，generic bin 不满足该路径。
- 双 WebSocket 和二进制媒体取代“只通过 JSON envelope”的旧表述，但 L0 反射仍独立于两条网络链路。
