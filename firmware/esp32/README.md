# naobot ESP32 MicroPython 固件

本目录是 KT2 身体控制层固件骨架。固件只负责硬件驱动、事件采集、安全动作执行、本地降级和 WebSocket 连接，不负责 LLM、长期记忆、图形化编程或裸舵机角度执行。

## 主固件能力

- `hardware/display.py` 集成 SSD1306 I2C OLED，默认 `SDA=GPIO8`、`SCL=GPIO9`、地址 `0x3C`。
- `hardware/imu.py` 集成 MPU6050，读取加速度/陀螺仪并输出 `upright`、`fallen`、`unknown` 姿态。
- OLED 或 MPU6050 缺失时主循环不崩溃；显示屏退回 console 输出，IMU 姿态按 `unknown` 处理。
- `unknown` 或 `fallen` 姿态会触发安全故障，禁止运动动作。
- `main.py` 会尝试连接 WiFi 和 Host WebSocket；当前只支持明文 `ws://`，不支持 `wss://`。
- 网络不可用时，固件继续使用本地 fallback，不阻塞安全循环。
- `demo/` 目录只用于硬件 bring-up 验证，不参与主固件运行链路。

## 网络配置

在 `config.py` 中配置：

```python
WIFI_SSID = "YOUR_WIFI"
WIFI_PASSWORD = "YOUR_PASSWORD"
AGENT_WS_URL = "ws://192.168.1.100:8765/ws/kt2"
```

ESP32 和宿主机需要在同一局域网。先在宿主机运行 `naobot serve`，再启动固件。

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
- MPU6050 缺失或读取失败按 `unknown` 姿态处理，禁止运动动作。
- Host 下发的 intent 仍需通过固件 `SafetyGuard`，固件不会盲信网络指令。
- `stop` 是最高优先级动作。
- 不支持 LLM 下发裸舵机角度。
- Agent 离线时进入本地降级。
