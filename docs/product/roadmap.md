# 路线图

## 当前基线：软件实现已完成

以下能力已在当前 HEAD 实现，并有自动化测试覆盖；这里的“完成”仅指 CPython、MicroPython fake、协议和静态构建配方层面，不代表真实 N16R8 硬件验收：

- AgentScope Brain 使用 `agentscope==2.0.4`、OpenAI-compatible 模型和空 Toolkit；单 Agent 默认 `6s`、团队默认 `15s`、ReAct 最多 4 轮，失败统一进入可观察 fallback。
- 自动路由按多目标、冲突、身份/记忆、时序视觉、歧义和长文本累计评分，`score >= 4` 进入团队；单 Agent 返回 `needs_team=true` 或 `confidence < 0.65` 时升级。
- 团队固定为情绪、行为、安全三位专家并行给建议，由产品负责人收敛；安全事件走确定性 fallback，不启动团队。
- 已识别人员的 AgentScope runtime 以 SQLite WAL 持久化；访客 runtime 只在内存中存在并在媒体连接结束时销毁。
- 控制 `/ws/kt2` 与媒体 `/ws/media` 分离；媒体有 hello/token、24 字节二进制帧、限长、队列淘汰、TTS 下行、WebSocket 分片接收和错误隔离测试。
- 自然交互支持唤醒词、短问候、触摸和持续目光激活；视频常态 10 FPS、事件窗口 15 FPS，音频由固件能量 VAD 标志和 Host 本地 VAD 兜底。
- 音频半双工已实现：TTS 期间仅暂停麦克风上传，摄像头继续保持 10/15 FPS；固件排空 TTS 后恢复麦克风，Host 在 TTS 完成后再等待 200 ms 恢复监听。AEC 与 barge-in 未实现。
- People 注册使用未知单人、最近 5 张人脸、口头确认和摸头确认；People API 支持列表、runtime 重置、删除和取消注册，并执行 token/本机鉴权。
- `runtime/naobot.db` 使用 SQLite WAL；只有人脸 embedding 和注册时 5 张样本用 Fernet 加密，未实现全数据库加密。
- Host 事件队列容量默认 32，高优先级优先且同优先级 FIFO；Host heartbeat 每 2 秒独立发送。
- 控制/媒体协议、runtime、身份注册、加密范围、People API、优先级队列、半双工和固件连接 worker 均有自动化测试。

## 下一阶段：N16R8 硬件验收待办

以下项目尚未执行，不得从软件测试或构建配方推断为已通过：

- 在真实 ESP32-S3 N16R8 44 针板上构建并烧录 MicroPython `v1.28.0` + `esp32-camera v2.1.6` 定制固件。
- 验证 CH343 下载/日志/REPL、16 MB Flash、8 MB Octal PSRAM、GPIO8/9 共线和电源稳定性。
- 验证 OV2640 QVGA JPEG、INMP441 PCM16 16 kHz 输入、MAX98357A TTS 输出、OLED、MPU6050、触摸和四路舵机。
- 实测反射优先级、急停、低电、跌倒、IMU fault、失联降级、动作中断和语义字段优先于兼容 `actions`。
- 实测控制与媒体各自 `_thread` 连接 worker 不破坏 50 ms 本地安全循环。

## 30 分钟硬件稳定性门槛（未执行）

一次验收必须在同一台真实 N16R8 上连续运行 30 分钟并保留日志，目标如下：

- 0 次崩溃、watchdog reset、非计划重启、PSRAM 分配失败和 CH343 串口中断。
- 控制 heartbeat 间隔保持 2 秒，不能出现超过 7 秒且未触发离线降级的空窗；控制与媒体异常互不拖垮。
- `local_loop_interval_ms` 的 P99 不超过 75 ms，最大值不超过 100 ms；记录所有 `local_loop_overrun_ms > 0` 样本。
- 常态视频平均 9-11 FPS；触摸等本地事件后的 boost 窗口平均 13-17 FPS。
- 完成至少 30 轮语音输入/TTS 输出；TTS 期间无麦克风上行但摄像头继续 10/15 FPS，排空后固件恢复麦克风，Host 在 200 ms 后恢复监听；无语音帧乱序或不可恢复的 TTS 卡死。
- 完成至少 10 次断网/重连或服务重启注入；本地反射始终可用，重连后控制与媒体状态恢复。

## 后续体验与工程化

- 扩展事件种类、Soul 表达和可解释 routine 推荐，但继续保持人工确认与动作白名单。
- 优化 Dashboard 状态、People 管理和媒体诊断，不把 Dashboard 变成编程 IDE。
- 对每个跨模块能力保持 PRD、ADR、协议、测试和硬件验收记录同步。
