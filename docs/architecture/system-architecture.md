# 系统架构

## 分层自治

naobot 把实时安全留在身体侧，把语义理解留在 Host 侧。高层输出不能越过低层控制权：

```text
L3 Host AgentScope：理解人员、会话、文本和媒体，只输出 goal/text/expression/skills/memory_suggestion
L2 Host BehaviorRuntime：确定性编译语义 intent，忽略 LLM actions，并经过 PolicyGuard
L1 Firmware motion/skill：动作 tick/cancel、姿态协调、限幅和 Host skill 中断
L0 Firmware reflex：急停、跌倒、低电、IMU fault，本地最高优先级
```

控制优先级为 `急停 > 反射安全 > 小脑姿态控制 > 固件技能 > Host/LLM intent > Routine/idle`。Host 失联、媒体异常或推理超时都不能覆盖 L0/L1。

## 连接与组件

```text
Dashboard / People API
          |
          v
FastAPI Host
  |-- /ws/kt2   JSON envelope 控制连接 ------ ESP32 control client
  |-- /ws/media hello + JSON/二进制媒体 ----- ESP32 media client
  |-- AgentScope Runtime / BehaviorRuntime
  |-- MediaService / natural interaction
  `-- SQLite WAL identity/session/runtime

CPython Simulator -------- /ws/kt2
```

控制和媒体是两条独立 WebSocket：控制链路承载 envelope、intent、ack 和 heartbeat；媒体链路承载 `media_hello`、TTS 控制 JSON 与带 24 字节帧头的二进制音视频。媒体拥塞或坏帧只产生媒体错误，不进入控制 socket。

## Host

Host 位于 `src/naobot/`，运行在 Python 3.11：

- AgentScope Runtime 使用 `agentscope==2.0.4`、OpenAI-compatible 模型、空 Toolkit 和最多 4 轮 ReAct。
- 路由器对多目标、冲突、身份/记忆、时序视觉、歧义和长文本计分；`score >= 4` 直接组队，单 Agent 的 `needs_team=true` 或 `confidence < 0.65` 触发升级。
- 团队固定并行运行情绪、行为、安全三位专家，再由产品负责人输出唯一决策；安全事件走确定性 fallback。
- 单 Agent 默认预算 6 秒，团队从开始到负责人收敛共用 15 秒预算。未配置、超时、异常或非法输出进入 fallback。
- `BehaviorRuntime` 只根据语义字段生成兼容 `actions`，并由 `PolicyGuard` 校验；LLM 自带 `actions` 不参与执行。
- `MediaService` 负责媒体连接、短期窗口、自然激活、VAD/ASR/视觉/身份/TTS 编排、注册和 People 管理。
- 会话可由唤醒词、短问候、触摸或持续目光激活；会话激活前不调用 cloud provider 或 Agent。
- 视频 capability 为常态 10 FPS、事件窗口 15 FPS；Host 默认保留 10 秒视频和 15 秒音频 RAM 窗口。
- TTS 使用半双工：Host 标记 speaking，TTS 完成后等待 200 ms 再恢复 listening；未实现 AEC 和 barge-in。
- Host 使用容量 32 的有界优先级事件队列；heartbeat 每 2 秒独立于推理发送。

默认大脑参数为 `NAOBOT_BRAIN_SINGLE_TIMEOUT_SECONDS=6.0`、`NAOBOT_BRAIN_TEAM_TIMEOUT_SECONDS=15.0`、`NAOBOT_BRAIN_MAX_ITERS=4`。兼容变量 `NAOBOT_BRAIN_TIMEOUT_SECONDS` 仅在未设置新的单 Agent 变量时作为单 Agent超时来源。

## 身份、会话与 Runtime

- `RuntimePersistence` 使用 `runtime/naobot.db` 和 SQLite WAL，保存 people、conversation sessions、已识别人员各 agent role 的 AgentScope state、embedding 和注册样本。
- `RuntimeRegistry` 按人员加异步锁；已识别人员 runtime 可从 SQLite 恢复，访客/visitor/guest runtime 只在内存缓存，媒体断开时销毁。
- 持久化 AgentScope state 会清除原始 base64/URL 媒体，只保留摘要和 SHA-256。
- 未知身份保持 visitor 隔离，不能读取或写入已识别人员 runtime。
- 注册只接受未知单人，要求最近 5 张帧、口头“确认”和摸头确认；embedding 与这 5 张样本用 Fernet 加密。
- SQLite 文件并非全库加密；people 元数据、会话和 Agent runtime state 仍是普通 SQLite 内容。

## ESP32 Firmware

固件位于 `firmware/esp32/`，运行在 MicroPython：

- 控制 client 与媒体 client 分别拥有独立的 `ConnectionWorker`，DNS/TCP/WebSocket 握手在各自 `_thread` 中执行，完成后才把 transport 交回 `uasyncio`。
- 主 50 ms 循环只执行硬件、动作、状态和反射；硬件对象不跨连接线程。
- 固件 heartbeat 上报控制权、反射、运动、媒体和本地 loop 指标；7 秒未收到 Host 消息时取消 Host skill 并进入本地自治。
- 语义 `skills/expression` 优先于兼容 `actions`，避免重复执行。
- 固件能量 VAD设置 speech/end-of-utterance flags；Host 本地 VAD只在固件未标注时补充。
- TTS 期间只停止麦克风上传，摄像头继续按 10/15 FPS 上传；收到 `tts_end` 且音频缓冲排空后立即恢复麦克风。Host 仍单独执行 200 ms 恢复延迟。
- 反射优先于网络、媒体、TTS 和 Host intent。媒体设备 unavailable 或媒体连接失败时，控制和反射路径继续运行。

固件当前只支持明文 `ws://`。连接 worker 降低握手阻塞风险，但不等于 FreeRTOS 高优先级隔离，也没有真实板上时序保证。

## Dashboard 与模拟器

Dashboard 是运维、状态、Soul/Memory/Routine、People、白名单动作和急停界面，不是编程 IDE。People API 的远程访问必须通过 device token 鉴权。

模拟器只连接 `/ws/kt2`，用于无硬件 event -> intent -> ack 软件闭环。它不验证媒体硬件、摄像头、I2S、PSRAM、CH343、舵机或 30 分钟板上稳定性。
