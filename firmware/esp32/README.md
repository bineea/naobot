# naobot ESP32 MicroPython 固件

本目录是 KT2 身体控制层固件骨架。固件只负责硬件驱动、事件采集、安全动作执行、本地降级和 WebSocket 连接，不负责 LLM、长期记忆、图形化编程或裸舵机角度执行。

## 上传

```powershell
mpremote connect COM3 fs mkdir :hardware
mpremote connect COM3 fs mkdir :motion
mpremote connect COM3 fs mkdir :safety
mpremote connect COM3 fs mkdir :interaction
mpremote connect COM3 fs mkdir :comm
mpremote connect COM3 fs cp firmware/esp32/config.py :
mpremote connect COM3 fs cp firmware/esp32/main.py :
mpremote connect COM3 fs cp -r firmware/esp32/hardware :
mpremote connect COM3 fs cp -r firmware/esp32/motion :
mpremote connect COM3 fs cp -r firmware/esp32/safety :
mpremote connect COM3 fs cp -r firmware/esp32/interaction :
mpremote connect COM3 fs cp -r firmware/esp32/comm :
mpremote connect COM3 soft-reset
```

建议先用 `mpremote connect COM3 mount firmware/esp32` 开发，稳定后再复制到 flash。

## 安全规则

- Agent 只能调用白名单动作。
- 低电量、姿态异常、跌倒后拒绝运动动作。
- `stop` 是最高优先级动作。
- 不支持 LLM 下发裸舵机角度。
- Agent 离线时进入本地降级。
