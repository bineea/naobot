# naobot

naobot 是 KT2 LLM 桌面智能机器人软件 MVP。当前软件包含 Host AgentScope Brain、FastAPI Dashboard、控制/媒体双 WebSocket、自然交互与身份注册、SQLite runtime、ESP32 MicroPython 固件、CPython 模拟器和自动化测试。

系统采用 L3/L2/L1/L0 分层自治：AgentScope 只输出语义字段，`BehaviorRuntime` 确定性编译并忽略 LLM `actions`，固件运动层与反射层保留最终控制权。Toolkit 为空，模型未配置、超时、异常或非法输出时进入安全 fallback。

## 当前事实

- 自动路由 `score >= 4` 进入团队；单 Agent 输出 `needs_team=true` 或 `confidence < 0.65` 时升级。
- 团队为情绪、行为、安全三专家加产品负责人；单 Agent 默认 6 秒，团队总预算 15 秒，最多 4 轮；安全事件不组队。
- 已识别人员 Agent runtime 使用 SQLite WAL，访客 runtime 仅在内存；Fernet 只加密人脸 embedding 和注册的 5 张样本，不是全数据库加密。
- `/ws/kt2` 承载控制 envelope，`/ws/media` 承载 hello/token、音视频与 TTS；视频常态 10 FPS、事件窗口 15 FPS。
- 半双工已实现：TTS 时仅暂停麦克风上行，摄像头继续按 10/15 FPS 上传；固件排空音频后恢复麦克风，Host 再延迟 200 ms 恢复监听。`wss://`、AEC 和 barge-in 未实现。
- 软件与协议测试已覆盖，但真实 N16R8、C 编译、项目定制 bin、摄像头/I2S/PSRAM/CH343 和 30 分钟指标均未验收。

## 文档入口

- AI agent 入口：`AGENTS.md`
- 产品事实源：`docs/product/prd.md`
- 路线图与硬件门槛：`docs/product/roadmap.md`
- 软件/硬件验收：`docs/product/acceptance.md`
- 系统架构：`docs/architecture/system-architecture.md`
- 协议：`docs/architecture/protocol.md`
- 安全与隐私：`docs/architecture/safety-policy.md`
- 开发、配置和烧录：`docs/development.md`

## 快速开始

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\naobot.exe serve
```

打开 `http://127.0.0.1:8765`。无硬件模拟：

```powershell
.\.venv\Scripts\naobot.exe simulate --event touch_head
.\.venv\Scripts\naobot.exe send-event --event battery_low
```

OpenAI-compatible 模型为可选配置：

```powershell
$env:NAOBOT_LLM_BASE_URL="http://127.0.0.1:1234/v1"
$env:NAOBOT_LLM_MODEL="local-model"
$env:NAOBOT_LLM_API_KEY="optional"
```

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
git diff --check
```

仓库 `data/ESP32_GENERIC_S3-20260406-v1.28.0.bin` 是 generic MicroPython 镜像，不含项目定制 `camera` 模块。摄像头路径需要按 `firmware/esp32/README.md` 构建定制镜像；当前配方尚未在本机真实执行 C 编译。

## 不做项

不做 Blockly/儿童积木编程、99 个游戏、自主桌边移动、LLM 裸舵机控制、未经确认的长期记忆、通用编程 IDE、全数据库加密承诺、`wss://`/AEC/barge-in 完成声明或无实测依据的硬件完成声明。
