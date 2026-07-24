# naobot ESP32 MicroPython 固件

本目录是 KT2 身体控制层固件骨架。固件只负责硬件驱动、事件采集、安全动作执行、本地降级和 WebSocket 连接，不负责 LLM、长期记忆、图形化编程或裸舵机角度执行。

## 主固件能力

- `hardware/display.py` 集成 0.96 寸 128x64 SSD1306 I2C OLED，默认 `SDA=GPIO8`、`SCL=GPIO9`、地址 `0x3C`，使用圆圆护目镜风格的眼睛驱动表情，不画大号 emoji、嘴巴或复杂文字。
- OLED 表情支持短动画帧：`idle` 左右看、`happy` 开心眯眼、`alert` 警觉抖动、`sleepy` 缓慢闭眼，`blink` 快速闭眼后恢复当前表情。
- `hardware/imu.py` 集成 MPU6050，读取加速度/陀螺仪并输出 `upright`、`fallen`、`unknown` 姿态。
- OLED 或 MPU6050 缺失时主循环不崩溃；显示屏退回 console 输出，IMU 姿态按 `unknown` 处理。
- `unknown` 或 `fallen` 姿态会触发安全故障，禁止运动动作。
- `main.py` 会尝试连接 WiFi 和 Host WebSocket；当前只支持明文 `ws://`，不支持 `wss://`。
- `media/` 使用独立 `/ws/media` WebSocket、独立队列和独立重连状态；控制 WebSocket 不承载二进制媒体。
- OV2640 默认输出 QVGA JPEG（quality 12），使用两个 PSRAM framebuffer、PSRAM DMA 和 `GRAB_LATEST`。
- INMP441 与 MAX98357A 均使用 PCM16、单声道、16 kHz I2S；固件能量 VAD 设置 speech/end-of-utterance flags，Host 只在固件未标注时用本地 VAD 兜底。
- 音频是半双工：TTS 播放期间只暂停麦克风上传，摄像头继续按常态 10 FPS/事件窗口 15 FPS 上传；固件收到 `tts_end` 且排空播放缓冲后立即恢复麦克风，Host 会在 TTS 完成后再等待默认 200 ms 才恢复 listening。当前没有 AEC 或 barge-in。
- `camera`、`machine.I2S` 或相应硬件缺失时，媒体设备标记为 unavailable，50 ms 本地安全循环继续运行。
- 摄像头先检查可用帧再取 framebuffer；I2S RX 只在 IRQ readiness 后读取；控制接收和媒体 connect/recv/send 使用不超过 10 ms 的单次超时，媒体大帧每次最多发送 1 KiB。
- 网络不可用时，固件继续使用本地 fallback，不阻塞安全循环。
- `reflex/` 提供本地反射安全层，低电、跌倒、IMU fault 和急停优先于 Host intent。
- `control/motion_controller.py` 提供可中断运动调度，运动 skill 可被 stop、低电或姿态异常抢占。
- `motion/action_player.py` 已实现 Host 白名单动作：`set_face`、`set_expression`、`blink`、`wave`、`small_step_forward`、`turn_left`、`turn_right`、`gentle_nudge`、`sit`、`chirp`、`sleep`、`stop`。
- 当前动作假设四个 180 度关节舵机按 `lf/rf/lr/rr` 表示左前、右前、左后、右后；动作序列以明显可见和可调参为目标。
- `demo/` 目录只用于硬件 bring-up 验证，不参与主固件运行链路。

## Seeed XIAO ESP32S3 Sense

固定 profile 位于 `boards/xiao_esp32s3_sense.py`，目标为 Seeed XIAO ESP32S3 Sense（8 MB Flash、8 MB Octal PSRAM）：

| 设备 | 固定 GPIO |
| --- | --- |
| OV2640 D0-D7 | 15, 17, 18, 16, 14, 12, 11, 48 |
| OV2640 XCLK/PCLK/VSYNC/HREF | 10, 13, 38, 47 |
| OV2640 SCCB SDA/SCL | 40, 39 |
| PDM 麦克风 CLK/DATA | 42, 41 |
| MAX98357A BCLK/LRC/DIN | 43, 44, 4 |
| microSD CS/SCK/MISO/MOSI | 3, 7, 8, 9 |
| OLED + MPU6050 SDA/SCL | 5, 6 |
| PCA9685 OE | 1 |

定制 `camera` 模块管理摄像头 SCCB；外部 OLED 与 MPU6050 使用 I2C0 GPIO5/6。下载、日志和 REPL 使用原生 USB CDC。

## 网络配置

在 `config.py` 中配置：

```python
WIFI_SSID = "YOUR_WIFI"
WIFI_PASSWORD = "YOUR_PASSWORD"
AGENT_WS_URL = "ws://192.168.1.100:8765/ws/kt2"
MEDIA_WS_URL = "ws://192.168.1.100:8765/ws/media"
DEVICE_ID = "kt2-esp32-s3"
DEVICE_TOKEN = ""
```

ESP32 和宿主机需要在同一局域网。先在宿主机运行 `naobot serve`，再启动固件。

固件默认每 2 秒发送一次 `heartbeat`。如果 7 秒内没有收到 Host 的任意消息或 Host heartbeat，固件会标记 `agent offline`，取消当前 Host skill，并继续保留本地 fallback 和反射安全。

控制 socket 一旦断开会立即取消当前动作和待执行队列；7 秒超时用于连接仍存在但 Host 无消息的 stale 场景。控制发送采用有界分块 flush，临时 EAGAIN 不会在 50 ms 安全循环中无限自旋。TCP/WebSocket connect 保留配置的 2 秒超时，建立连接后的单次 I/O 仍限制为 10 ms。

媒体 socket 连接后首先发送 `media_hello`，其中包含 `device_id`、`token`、`boot_id` 和 QVGA/PCM16 capability。二进制帧头固定为 `>4sBBHIQI`，字段顺序为 `magic, version, kind, flags, sequence, timestamp_ms, payload_length`；kind 1/2/3 分别表示 PCM16 上行、JPEG 上行和 TTS PCM16 下行。视频常态 10 FPS，本地事件后的短窗口为 15 FPS；队列拥塞时先淘汰旧视频，再淘汰非语音音频，不让媒体异常进入控制 socket。

heartbeat 额外报告 `camera_fps`、`audio_state`、`media_queue`、`media_dropped` 和 `psram_free`。

Camera/I2S 单次瞬态异常会记录但允许后续采集恢复；连续 3 次驱动异常后才标记 unavailable，并保持控制和反射路径运行。

50 ms 安全循环按 deadline 补偿睡眠，并额外记录实际调度间隔和 overrun。控制 client 只把 DNS/TCP/WebSocket 握手交给 `ConnectionWorker`；媒体链路则由独立 `MediaRuntimeWorker` 线程从头到尾独占 Camera、I2S 和媒体 WebSocket，主循环只发送事件加速截止时间并读取标量快照。`_thread` 不可用时媒体直接标记为 disabled，不会退化到安全循环中同步执行。媒体连接失败也不能改变反射控制权。当前实现仍不提供 FreeRTOS 高优先级隔离保证；真实硬件驱动、GC、线程调度或底层网络栈仍可能引入抖动，必须通过板上 stall probe 验证。

## OTA 原生 API 契约

Stage 1 的原生写入入口是
`begin(image_size, expected_sha256_bytes, sequence)`。第三个 `sequence` 是有意增加的
安全参数，必须来自已验签 manifest，并由原生 NVS anti-rollback 状态再次校验。
不提供两参数兼容入口；旧调用方必须显式传入已验签且严格递增的 uint32 sequence，
不能用默认值绕过防降级检查。

`finish()` 和 `activate()` 可能触发 ESP-IDF 整镜像同步验证，只能由专属
`OtaWorker` 串行执行。50 ms 主循环只 submit/poll，并在 worker pending、异常或
超时时保持 OE disabled。后台操作完成前，协调器不得并发调用同一 OTA session 的
`begin()`、`write()` 或 `abort()`。

## 定制 MicroPython 镜像

`build/` 提供固定上游版本的可复现配方：

- MicroPython `v1.28.0`，commit `2b0015629f67fd186f980079b2e696ad0bc7343c`
- Espressif `esp32-camera v2.1.6`，commit `2ac69a6f1749694804f5196e63fa1f79800b74bf`
- 外置 `XIAO_ESP32S3_SENSE` board profile + `SPIRAM_OCT`，8 MB Flash，`CONFIG_CAMERA_PSRAM_DMA=y`
- `camera_module/` 提供 `camera` MicroPython C 模块；`manifest.py` 冻结 board、media 和 config

在已安装 Git、GNU Make、ESP-IDF 及其工具链的 PowerShell 环境运行：

```powershell
firmware/esp32/build/build.ps1 -Clean
```

生产构建必须通过 `-OtaPublicKeyHeader <path>` 提供生产 P-256 公钥头；未指定时仅使用仓库开发公钥，不得发布。脚本会把源码放在 `firmware/esp32/build/_work/`，校验两个固定 commit 后调用 MicroPython ESP32 构建。输出目录为 `_work/micropython/ports/esp32/build-XIAO_ESP32S3_SENSE-SPIRAM_OCT/`。仓库不提交构建产物；每次构建仍需检查输出并在目标板上验证。

## 上传

```powershell
mpremote connect COM3 fs mkdir :hardware
mpremote connect COM3 fs mkdir :motion
mpremote connect COM3 fs mkdir :safety
mpremote connect COM3 fs mkdir :interaction
mpremote connect COM3 fs mkdir :comm
mpremote connect COM3 fs mkdir :control
mpremote connect COM3 fs mkdir :reflex
mpremote connect COM3 fs mkdir :boards
mpremote connect COM3 fs mkdir :media
mpremote connect COM3 fs cp firmware/esp32/config.py :
mpremote connect COM3 fs cp firmware/esp32/main.py :
mpremote connect COM3 fs cp -r firmware/esp32/hardware :
mpremote connect COM3 fs cp -r firmware/esp32/motion :
mpremote connect COM3 fs cp -r firmware/esp32/safety :
mpremote connect COM3 fs cp -r firmware/esp32/interaction :
mpremote connect COM3 fs cp -r firmware/esp32/comm :
mpremote connect COM3 fs cp -r firmware/esp32/control :
mpremote connect COM3 fs cp -r firmware/esp32/reflex :
mpremote connect COM3 fs cp -r firmware/esp32/boards :
mpremote connect COM3 fs cp -r firmware/esp32/media :
mpremote connect COM3 soft-reset
```

建议先用 `mpremote connect COM3 mount firmware/esp32` 开发，稳定后再复制到 flash。

## 安全规则

- Agent 只能调用白名单动作。
- 低电量、姿态异常、跌倒后拒绝运动动作。
- MPU6050 缺失或读取失败按 `unknown` 姿态处理，禁止运动动作。
- Host 下发的 intent 仍需通过固件 `SafetyGuard`，固件不会盲信网络指令。
- 固件本地反射层拥有最高控制权，Host/LLM intent 不能覆盖急停、低电、跌倒或 IMU fault。
- Host 心跳超时不会关闭本地反射安全；固件会进入本地自治并取消当前 Host 运动。
- 固件动作执行失败时返回 `error`，不会对未执行动作回假 `ack`。
- `stop` 是最高优先级动作。
- 不支持 LLM 下发裸舵机角度。
- Agent 离线时进入本地降级。

## 硬件验证状态

2026-07-24 已在 HEAD `240b4e9` 使用固定上游版本完成项目定制 C 编译、链接和分区尺寸检查，生成 1,840,304 字节的 OTA 应用镜像 `micropython.bin`。这不等于硬件验收；仓库 generic bin 也仍不含定制 `camera` 模块。真实 XIAO ESP32S3 Sense 上的烧录、签名 OTA、失败回滚、OV2640、PDM/I2S、PSRAM、USB CDC、OLED、MPU6050、触摸、舵机和 30 分钟稳定性均未验收。首次 bring-up 必须检查摄像头 10/15 FPS、音频声道/幅值、PSRAM 余量、半双工排空恢复、TTS 连续播放、控制连接重连、媒体 worker 重连以及媒体拥塞时 50 ms 安全循环抖动；指标门槛见 `docs/product/roadmap.md`。
