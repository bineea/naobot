# naobot ESP32 MicroPython 固件

本目录是 KT2 身体控制层固件骨架。固件只负责硬件驱动、事件采集、安全动作执行、本地降级和 WebSocket 连接，不负责 LLM、长期记忆、图形化编程或裸舵机角度执行。

## 主固件能力

- `hardware/display.py` 集成 0.96 寸 128x64 SSD1306 I2C OLED，默认 `SDA=GPIO8`、`SCL=GPIO9`、地址 `0x3C`，使用圆圆护目镜风格的眼睛驱动表情，不画大号 emoji、嘴巴或复杂文字。
- OLED 表情支持短动画帧：`idle` 左右看、`happy` 开心眯眼、`alert` 警觉抖动、`sleepy` 缓慢闭眼，`blink` 快速闭眼后恢复当前表情。
- `hardware/imu.py` 集成 MPU6050，读取加速度/陀螺仪并输出 `upright`、`fallen`、`unknown` 姿态。
- OLED 或 MPU6050 缺失时主循环不崩溃；显示屏退回 console 输出，IMU 姿态按 `unknown` 处理。
- `unknown` 或 `fallen` 姿态会触发安全故障，禁止运动动作。
- `main.py` 会尝试连接 WiFi 和 Host WebSocket；当前只支持明文 `ws://`，不支持 `wss://`。
- 网络不可用时，固件继续使用本地 fallback，不阻塞安全循环。
- `reflex/` 提供本地反射安全层，低电、跌倒、IMU fault 和急停优先于 Host intent。
- `control/motion_controller.py` 提供可中断运动调度，运动 skill 可被 stop、低电或姿态异常抢占。
- `motion/action_player.py` 已实现 Host 白名单动作：`set_face`、`set_expression`、`blink`、`wave`、`small_step_forward`、`turn_left`、`turn_right`、`gentle_nudge`、`sit`、`chirp`、`sleep`、`stop`。
- 当前动作假设四个 180 度关节舵机按 `lf/rf/lr/rr` 表示左前、右前、左后、右后；动作序列以明显可见和可调参为目标。
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
mpremote connect COM3 fs mkdir :control
mpremote connect COM3 fs mkdir :reflex
mpremote connect COM3 fs cp firmware/esp32/config.py :
mpremote connect COM3 fs cp firmware/esp32/main.py :
mpremote connect COM3 fs cp -r firmware/esp32/hardware :
mpremote connect COM3 fs cp -r firmware/esp32/motion :
mpremote connect COM3 fs cp -r firmware/esp32/safety :
mpremote connect COM3 fs cp -r firmware/esp32/interaction :
mpremote connect COM3 fs cp -r firmware/esp32/comm :
mpremote connect COM3 fs cp -r firmware/esp32/control :
mpremote connect COM3 fs cp -r firmware/esp32/reflex :
mpremote connect COM3 soft-reset
```

建议先用 `mpremote connect COM3 mount firmware/esp32` 开发，稳定后再复制到 flash。

## 安全规则

- Agent 只能调用白名单动作。
- 低电量、姿态异常、跌倒后拒绝运动动作。
- MPU6050 缺失或读取失败按 `unknown` 姿态处理，禁止运动动作。
- Host 下发的 intent 仍需通过固件 `SafetyGuard`，固件不会盲信网络指令。
- 固件本地反射层拥有最高控制权，Host/LLM intent 不能覆盖急停、低电、跌倒或 IMU fault。
- 固件动作执行失败时返回 `error`，不会对未执行动作回假 `ack`。
- `stop` 是最高优先级动作。
- 不支持 LLM 下发裸舵机角度。
- Agent 离线时进入本地降级。
