# ADR-0002：宿主机与固件分离

## 状态

Accepted

## 背景

`src/naobot/` 运行在 CPython 宿主机环境，`firmware/esp32/` 运行在 ESP32 MicroPython 环境。两者依赖、部署方式和安全职责不同。

## 决策

- `src/naobot/` 只放宿主机 Python package，包括 Agent、Dashboard、协议、CLI、模拟器、Memory 和 Routine。
- `firmware/esp32/` 只放 ESP32 MicroPython 固件代码，包括事件采集、动作执行、本地安全和 WebSocket client。
- 固件不进入 Python package 打包，也不使用宿主机 `requirements.txt` 作为运行时依赖。

## 影响

- 宿主机通过 `pip install -e .` 安装和运行。
- 固件通过 `mpremote` 上传。
- 两端只通过 JSON envelope 协议协作。

