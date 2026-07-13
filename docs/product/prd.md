# naobot PRD

## 背景与事实源

KT2 是口袋尺寸桌面机器人。naobot 在复刻 KT2 的基础上加入宿主机 Agent，让固定动作设备成为可配置、可观察、可安全扩展的智能机器人。

本文件是唯一主 PRD。实现细节见 `docs/architecture/`，阶段状态见 `docs/product/roadmap.md` 与 `docs/product/acceptance.md`，开发配置见 `docs/development.md`。文档中的完成状态必须能由当前 HEAD 的代码、测试或实测记录证明。

## 目标用户

- 复刻 KT2 的个人开发者和 maker。
- 希望在真实硬件前用模拟器验证行为的开发者。
- 使用 AI agent 协作推进产品、架构、开发、测试和评审的维护者。

## 分层自治原则

- L3 Host AgentScope 负责人员/会话上下文、语义理解、对话、情绪表达和复杂请求判断，只输出 `goal`、`text`、`expression`、`skills`、`memory_suggestion`。
- L2 Host `BehaviorRuntime` 确定性编译 L3 语义字段，忽略 LLM 自带 `actions`，并让 `PolicyGuard` 校验白名单技能、参数化表情和兼容 actions。
- L1 固件运动/技能层负责动作 tick/cancel、姿态协调、限幅和中断。
- L0 固件反射层负责急停、跌倒、低电和 IMU fault，拥有最高控制权；Host、媒体和 TTS 都不能覆盖本地反射。

## MVP 核心能力

### AgentScope Brain

- 使用 `agentscope==2.0.4` Agent 和 OpenAI-compatible 模型，Toolkit 为空，不提供 shell、文件、Python、MCP、硬件工具或任意代码执行能力。
- 自动路由对多目标、冲突、身份/记忆、时序视觉、歧义和长文本计分；`score >= 4` 自动进入团队。
- 单 Agent 输出 `needs_team=true` 或 `confidence < 0.65` 时自升级到团队；安全事件始终走确定性 fallback，不组队。
- 团队固定由情绪、行为、安全三位专家并行给建议，再由产品负责人收敛为唯一决策。
- 单 Agent 默认超时 6 秒，团队从专家到负责人共用 15 秒预算，ReAct 最多 4 轮。未配置、超时、异常或非法输出统一进入规则 fallback，状态可观察。

### 人员、会话与 Runtime

- 会话支持唤醒词、短问候、摸头和持续目光等自然激活；会话激活前不调用 cloud provider 或 Agent。
- 已识别人员各 agent role 的 AgentScope runtime 写入 `runtime/naobot.db`，SQLite 使用 WAL；人员维度并发由锁串行化。
- visitor/guest runtime 只存在内存中，媒体连接结束时销毁，不写入已识别人员 runtime。
- 持久化 runtime 前移除原始 base64/URL 媒体，只保存文本摘要和 SHA-256。
- 注册仅接受未知单人，需要最近 5 张人脸帧、口头确认和摸头确认；没有 `NAOBOT_DATA_KEY` 时拒绝注册。
- 只有人脸 embedding 和注册使用的 5 张样本采用 Fernet 加密；SQLite 数据库整体未加密。
- People API 提供人员列表、runtime 重置、人员删除和注册取消；远程访问使用 bearer 或 `X-Naobot-Token`，未配置 token 时仅允许 loopback。

### 媒体与自然交互

- 控制使用 `/ws/kt2` JSON envelope；媒体使用独立 `/ws/media`，先发送带 `device_id/token/boot_id/capabilities` 的 `media_hello`，再交换媒体控制 JSON 和二进制帧。
- 视频 capability 为常态 10 FPS、本地事件窗口 15 FPS；Host 默认保留 10 秒视频和 15 秒音频 RAM 窗口，短期原始媒体不落盘。
- INMP441/MAX98357A 使用 PCM16、单声道、16 kHz；固件能量 VAD 标注 speech/end-of-utterance，Host 本地 VAD仅在固件未标注时兜底。
- 媒体入口队列有界；拥塞时优先淘汰最旧 JPEG，再淘汰非语音音频，新 JPEG 不得驱逐 speech/EOU；丢帧按 kind/reason 可观察。媒体坏帧或后端异常不能阻断控制 heartbeat，也不能覆盖固件反射。
- WebSocket 摄取、短本地观察与 ASR/Agent/TTS 分层执行；EOU 的音视频快照进入独立有界 turn queue，慢推理期间摄取与本地视频窗口继续更新。
- 已实现音频半双工：TTS 期间只暂停麦克风上传，摄像头继续按常态 10 FPS/事件窗口 15 FPS 上传；Host 将 PCM16 16 kHz TTS 拆为不超过 8192 bytes 的递增序号帧并按 32000 bytes/s 节流；固件在 `tts_end` 且播放缓冲排空后恢复麦克风，Host 在 TTS 完成后延迟 200 ms 恢复监听。
- 当前不支持 `wss://`、AEC 或 barge-in，也不声称支持任意 WebSocket RFC 扩展。

### 控制、Dashboard 与固件

- 控制 envelope 支持 `event`、`intent`、`ack`、`status`、`error`、`heartbeat`；语义字段优先于兼容 `actions`，避免同一 intent 重复执行。
- 配置设备 token 时，控制 `/ws/kt2` Upgrade 必须携带 `X-Naobot-Token`；固件使用 Host heartbeat 建立短期时钟锚点，在 ACK 前拒绝过期 intent、无可靠时钟的运动 intent、活跃反射下的非 stop intent 和所有越界/裸硬件参数。
- Host 使用容量 32 的有界优先级事件队列；高优先级优先、同优先级 FIFO，队列满时驱逐较低优先级或拒绝新事件。
- Host heartbeat 默认每 2 秒发送，独立于 6/15 秒推理预算。
- Dashboard 提供状态、日志、Soul、Memory、Routine、People、白名单动作测试和急停，不提供编程 IDE 或任意代码执行。
- Memory 与 Routine 默认待确认；Routine 只允许白名单动作。
- 控制与媒体连接分别由独立 MicroPython `_thread` connection worker 完成 DNS/TCP/WebSocket 握手；固件 50 ms 本地循环和反射不把硬件对象交给连接线程。

## 不做项

- 不做 Blockly/Blockley 或儿童积木编程。
- 不做 99 个游戏。
- 不做自主桌边移动。
- 不做 LLM 直接控制舵机角度、PWM、servo id 或裸硬件字段。
- 不做未经确认的长期记忆写入。
- 不把 Dashboard 做成通用编程 IDE。
- 当前不做全数据库加密、`wss://`、AEC 或 barge-in。

## 验收边界

软件实现与自动化测试已覆盖 Agent/runtime、自动路由、加密范围、身份注册、People API、控制/媒体协议、队列、半双工、固件连接 worker 和反射隔离。具体清单见 `docs/product/acceptance.md`。

真实 N16R8 硬件仍未验收：仓库配方未在本机执行真实 C 编译，未生成项目定制真实 bin，也未实测 OV2640、I2S、PSRAM、CH343、OLED、MPU6050、触摸、舵机或 30 分钟稳定性指标。仓库 generic bin 不含项目定制 `camera` 模块，不能作为摄像头验收镜像。

## 变更记录

- 2026-06-23：建立 AI-native 文档结构，确认本文件为唯一主 PRD；代码更新必须同步检查并暂存本文件。
- 2026-06-30：增加固件 demo、SSD1306 OLED 与 MPU6050 集成；MPU6050 缺失或失败按 `unknown` 并禁止运动。
- 2026-07-08：固件接入 WiFi 与明文 `ws://` 控制 WebSocket；补齐 Host 白名单动作，执行失败返回 `error`。
- 2026-07-09：修复 MicroPython 兼容性并迭代 OLED 眼睛动画；产品与安全边界不变。
- 2026-07-10：引入 L3/L2/L1/L0 分层自治、双向 heartbeat、固件反射与可中断运动控制。
- 2026-07-13：引入 AgentScope Brain、L2 确定性编译、空 Toolkit、fallback、有界优先级队列与语义字段优先策略。
- 2026-07-13：按 HEAD `d485ac7` 同步实际实现：自动路由 `score >= 4` 与 `needs_team`/`confidence < 0.65` 升级；情绪/行为/安全三专家加负责人；6 秒单 Agent/15 秒团队；SQLite WAL 人员 runtime 与访客内存 runtime；People 注册/API；控制/媒体双 WebSocket、10/15 FPS、VAD、半双工、TTS 与分片测试；Fernet 仅覆盖 embedding 和 5 张样本。明确 N16R8 C 编译、真实 bin、摄像头/I2S/PSRAM/CH343 和 30 分钟指标均未验收。
- 2026-07-13：修正半双工媒体行为：TTS 播放期间继续上传 10/15 FPS 摄像头帧，仅暂停麦克风上行，避免自然交互时出现视觉断层。
- 2026-07-13：修复活跃会话人物切换的 runtime 隔离。
- 2026-07-13：修复 Dashboard/REST 管理 API 鉴权缺口；配置 `NAOBOT_DEVICE_TOKEN` 时全部 HTTP `/api/*` 必须使用 Bearer 或 `X-Naobot-Token`，未配置时仅允许 loopback；根 HTML 保持开放，WebSocket 鉴权不在本次变更范围内。
- 2026-07-13：修复未认证媒体 WebSocket 占用唯一连接槽；`media_hello` 默认 5 秒超时，超时、非法 hello 或 token 使用 1008 关闭，认证成功后在连接锁内原子检查并登记唯一连接。
- 2026-07-13：runtime 生命周期锁细化为同一人物同一 role 串行、不同 role 并行，避免团队专家调用被人物级长锁串行；SQLite schema 升级到 v2，People、session、embedding 与 agent runtime 均按 `robot_id` 隔离，并原地保留迁移 v1 数据。
- 2026-07-13：修复 Host 与固件 heartbeat 时钟域混用；机器人在线状态和 Last heartbeat 使用 Host 接收时间，固件 heartbeat 时间戳与 uptime 单独可观察；Host heartbeat 序号在单 Agent 实例内按 uint32 递增。
- 2026-07-13：修复 Host 媒体入口背压、推理耦合与 TTS 整段发送：入口队列按 JPEG、非语音音频顺序淘汰并保护 speech/EOU；frame/turn 双 worker 让慢 Agent 不阻塞摄取；TTS 使用不超过 8192 bytes 的递增帧并按 PCM16 播放速率节流，断线统一取消 worker、清空队列与访客 runtime。
- 2026-07-13：加固固件控制链路：控制 WS 复用增量分片解析，Upgrade 支持 `X-Naobot-Token`；以 Host heartbeat 锚定 deadline；stop、反射、低电和无可靠时钟在 ACK 前完成拒绝/抢占；固件递归拒绝裸硬件字段和越界/未知参数；反射恢复后允许同类跌倒或低电再次触发，同时保留 `last_reflex` 历史。
