# 验收标准

## 软件已完成项

当前 HEAD 的软件验收范围包括：

- `pytest` 覆盖 AgentScope 流式输出、非法输出/异常/超时 fallback、4 轮上限、自动路由 `score >= 4`、`needs_team`/低置信度升级、三专家加负责人和安全事件不组队。
- Runtime 测试覆盖 SQLite WAL、已识别人员持久化、访客内存隔离与销毁、并发锁、取消时保存、媒体内容清洗和 People 级删除/重置。
- 身份测试覆盖未知单人注册、5 张样本、口头确认、摸头确认、10 秒窗口、无 `NAOBOT_DATA_KEY` 拒绝、Fernet 密文和匹配缓存刷新。
- 控制协议覆盖 `event/intent/ack/status/error/heartbeat` envelope、语义字段优先、非法字段和 event -> intent -> ack。
- 媒体协议覆盖 `/ws/media` hello/token、24 字节 `>4sBBHIQI` 帧头、kind/flags/限长、TTS、合法 WebSocket continuation、非法分片、ping/pong 和 close/error。
- 媒体服务覆盖控制/媒体隔离、10/15 FPS capability、有界队列、先丢旧视频再丢非语音音频、会话前不调用 cloud/Agent、RAM 时序摘要不落盘和半双工状态。
- People API 覆盖鉴权、列表、runtime 重置、人员删除和注册取消。
- 固件 fake 覆盖控制与媒体独立 `_thread` 连接 worker、固件 VAD、TTS 排空恢复、heartbeat 媒体/loop 指标和媒体异常不影响反射路径。

软件验收命令：

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
```

## 软件手动检查

- `naobot serve` 后 Dashboard 与 `/health` 可访问。
- `naobot simulate --event touch_head` 完成 event -> intent -> ack。
- 未配置模型、模型超时、运行异常或非法输出时，状态接口显示 fallback 和最近错误。
- Host heartbeat 每 2 秒独立发送，不因 6 秒单 Agent 或 15 秒团队预算阻塞链路刷新。
- 使用本机或有效 bearer/device token 调试 People API；未授权远程请求返回 403。
- 没有活跃会话时，媒体输入不会调用 cloud provider 或 Agent；TTS 后 Host 延迟 200 ms 恢复监听。

## N16R8 硬件待验收

以下项目当前均为“未执行”：

- MicroPython `v1.28.0` + `esp32-camera v2.1.6` 配方的真实 C 编译与产物检查。
- 定制镜像烧录、CH343、OV2640、INMP441、MAX98357A、OLED、MPU6050、触摸、舵机和 PSRAM 实测。
- 控制/媒体双 WebSocket 在真实 WiFi、断网、重连和大帧压力下的板上行为。
- 急停、低电、跌倒、IMU fault、失联降级和反射优先级的物理动作验收。
- 30 分钟稳定性指标。具体门槛见 `docs/product/roadmap.md`；未形成带时间戳日志前不得标记通过。

仓库中的 generic MicroPython bin 不含项目定制 `camera` 模块，不能用于完成摄像头验收。模拟器、CPython fake、协议测试和静态构建配方都不能替代上述实机记录。

## 文档验收

- README、AGENTS、PRD、路线图、验收、架构、协议、安全、开发和 ADR 中的 6/15 秒、双 WebSocket、runtime、People、加密范围与硬件状态一致。
- 文档不声称真实 bin 已生成，不声称摄像头/I2S/PSRAM/CH343 或 30 分钟指标已通过。
- 文档不声称全数据库加密、`wss://`、AEC、barge-in 或任意 RFC WebSocket 扩展已完成。
