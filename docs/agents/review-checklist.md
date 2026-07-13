# 代码与文档评审清单

## 安全与分层

- L3 是否只输出语义字段，L2 是否忽略 LLM `actions`，L0 反射是否保持最高优先级。
- 是否拒绝未知动作、裸舵机/PWM/GPIO 字段、过期 intent、低电与异常姿态运动。
- Toolkit 是否为空；fallback 是否仍经过 `BehaviorRuntime` 和 `PolicyGuard`。
- 自动路由是否保持 `score >= 4`、`needs_team=true`/`confidence < 0.65` 升级、三专家加负责人和安全事件不组队。
- 超时是否写为单 Agent 6 秒、团队总预算 15 秒、最多 4 轮。

## Runtime、隐私与 People

- 已识别人员 runtime 是否使用 SQLite WAL，访客是否只在内存并在连接结束时销毁。
- 会话激活前是否避免 cloud/Agent 调用，RAM 音视频窗口是否不落盘，持久化 state 是否清洗媒体。
- 是否准确表述 Fernet 仅加密 embedding 与 5 张注册样本，而非整个数据库。
- 无 `NAOBOT_DATA_KEY` 是否拒绝注册；未知身份是否与已识别人员隔离。
- People API 是否鉴权，删除/reset/cancel 是否覆盖正常与拒绝路径。

## 协议与媒体

- `/ws/kt2` 控制 envelope 与 `/ws/media` hello/二进制媒体是否保持隔离。
- 24 字节 `>4sBBHIQI` 字段顺序、kind、flags 和 payload 上限是否两端一致。
- 是否覆盖 TTS start/binary/end、合法 continuation、非法分片、ping/pong、坏帧与媒体错误隔离。
- 是否保持 10/15 FPS、队列有界、先丢旧视频再丢非语音音频和 heartbeat 媒体/loop 指标。
- 音频半双工是否为 TTS 期间仅停麦克风、摄像头继续 10/15 FPS、固件排空恢复麦克风 + Host 200 ms；不得误写成 AEC、barge-in 或全双工音频已完成。

## 不做项与硬件事实

- 是否重新引入 Blockly、99 游戏、自主桌边移动、通用 IDE、LLM 裸硬件控制或未经确认的长期记忆。
- 是否误称全数据库加密、`wss://` 或任意 RFC 扩展已完成。
- 是否把 generic bin 写成含 `camera`，或把配方/CPython fake/语法检查写成真实 C 编译和硬件验收。
- 没有日志时，是否误称 N16R8、OV2640、I2S、PSRAM、CH343、舵机或 30 分钟指标已通过。

## 验证

- `pytest`、`ruff check .`、`tools/check_prd_sync.py`、陈旧表述 `rg`、Markdown 链接/围栏检查和 `git diff --check` 是否有最新输出。
- README、AGENTS、PRD、roadmap、acceptance、架构、协议、安全、development、固件 README 与 ADR 是否一致。
- 评审输出是否先列问题与风险，再说明测试缺口、硬件未执行项和修改摘要。
