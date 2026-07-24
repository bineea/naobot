# naobot PRD

## 背景与事实源

KT2 是口袋尺寸桌面机器人。naobot 在复刻 KT2 的基础上加入宿主机 Agent，目标是成为面向家庭多人、默认儿童安全、高度主动、功能完善且长期有趣的桌面陪伴机器人，而不是停留在命令式玩具或软件 MVP。

本文件是唯一主 PRD。实现细节见 `docs/architecture/`，阶段状态见 `docs/product/roadmap.md` 与 `docs/product/acceptance.md`，开发配置见 `docs/development.md`。文档中的完成状态必须能由当前 HEAD 的代码、测试或实测记录证明。

## 产品愿景

- naobot 能识别并尊重不同家庭成员，用对话、圆眼表情、声音、姿态和动作形成连续的陪伴关系。
- naobot 具有受边界约束的主动性：能够观察合适时机、主动问候和发起互动，同时遵守勿扰、频率、隐私、儿童安全和固件反射规则。
- 趣味性由安全技能系统、人格与关系成长、LLM 即兴创作共同提供；LLM 只能生成语义意图、表情参数和技能编排，不能直接控制硬件。
- 产品联网优先；断网时保留安全反射、触摸、基础表情动作和固定互动，联网后提供开放式对话、视觉理解和生成式玩法。
- 受限桌面探索是正式目标，但只有在专用边缘/距离感知、区域约束和真实硬件安全验收完成后才能启用。

## 目标用户

- 第一目标用户是家庭中的成人与儿童；机器人必须支持多人身份、个人与家庭共享边界、监护人控制和访客模式。
- 儿童可以直接互动，系统默认执行内容分级、最小化记忆和严格的主动行为边界。
- 个人开发者、maker 和 AI agent 维护者是第二目标用户，可扩展安全技能和验证软硬件，但不能绕过产品安全边界。

## 产品能力

1. 陪伴与关系：多人身份、个人/家庭记忆、人格成长、关系连续性和可解释遗忘。
2. 自然交流：唤醒、触摸、对视、语音、视觉、连续会话和自然打断恢复。
3. 生命感与主动性：情绪、精力、兴趣、主动行为候选、打扰预算、勿扰和反馈学习。
4. 身体表达：参数化圆眼、声音、姿态、动作编排、可中断技能和本地反射。
5. 趣味系统：安全技能包、故事、模仿、互动玩法、日常仪式和 LLM 动态创作。
6. 受限桌面探索：边缘检测、测距、区域约束、避障、定位和安全回退。
7. 家庭与儿童安全：监护人权限、内容分级、陌生人模式、隐私退出和数据删除。
8. 可靠性与产品化：配网、升级、恢复、校准、设备健康和长期稳定运行。
9. AI 与扩展生态：AgentScope runtime、技能注册、能力声明、版本兼容和可重复评测。

## 分层自治原则

- L3 Host AgentScope 负责人员/会话上下文、语义理解、对话、情绪表达和复杂请求判断，只输出 `goal`、`text`、`expression`、`skills`、`memory_suggestion`。
- L2 Host `BehaviorRuntime` 确定性编译 L3 语义字段，忽略 LLM 自带 `actions`，并让 `PolicyGuard` 校验白名单技能、参数化表情和兼容 actions。
- L1 固件运动/技能层负责动作 tick/cancel、姿态协调、限幅和中断。
- L0 固件反射层负责急停、跌倒、低电和 IMU fault，拥有最高控制权；Host、媒体和 TTS 都不能覆盖本地反射。

## 当前架构基线

### AgentScope Brain

- 使用 `agentscope==2.0.4` Agent 和 OpenAI-compatible 模型，Toolkit 为空，不提供 shell、文件、Python、MCP、硬件工具或任意代码执行能力。
- 自动路由对多目标、冲突、身份/记忆、时序视觉、歧义和长文本计分；`score >= 4` 自动进入团队。
- 单 Agent 输出 `needs_team=true` 或 `confidence < 0.65` 时自升级到团队；安全事件始终走确定性 fallback，不组队。
- 团队固定由情绪、行为、安全三位专家并行给建议，再由产品负责人收敛为唯一决策。
- 单 Agent 默认超时 6 秒，团队从专家到负责人共用 15 秒预算，ReAct 最多 4 轮。未配置、超时、异常或非法输出统一进入规则 fallback，状态可观察。

### 人员、会话与 Runtime

- 会话支持唤醒词、短问候、摸头和持续目光等自然激活；会话激活前不调用 cloud provider 或 Agent。
- 已识别人员各 agent role 的 AgentScope runtime 写入 `runtime/naobot.db`，SQLite 使用 WAL；同一 role 串行、不同 role 可并行，reset/delete 通过人物 activity gate 与 generation/tombstone 等待在途调用并使旧 session save 失效。
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
- WebSocket 摄取、短本地观察与 ASR/Agent/TTS 分层执行；EOU 的音视频快照连同当时的 `session_id/person_id/trigger` 进入独立有界 turn queue，人物切换后旧 turn 不得写入新人物 runtime；慢推理期间摄取与本地视频窗口继续更新。
- 已实现音频半双工：TTS 期间只暂停麦克风上传，摄像头继续按常态 10 FPS/事件窗口 15 FPS 上传；Host 将 PCM16 16 kHz TTS 拆为不超过 8192 bytes 的递增序号帧并按 32000 bytes/s 节流；固件在 `tts_end` 且播放缓冲排空后恢复麦克风，Host 在 TTS 完成后延迟 200 ms 恢复监听。
- 当前不支持 `wss://`、AEC 或 barge-in，也不声称支持任意 WebSocket RFC 扩展。

### 控制、Dashboard 与固件

- 控制 envelope 支持 `event`、`intent`、`ack`、`status`、`error`、`heartbeat`；语义字段优先于兼容 `actions`，避免同一 intent 重复执行。`ack.status` 三态：`accepted`（已入队，非终态）、`completed`（动作自然播完，终态）、`failed`（被 reflex 或中断取消，携带 `reason`，终态）；`error` 用于入队前拒绝，不与 `ack failed` 混用。固件对 `intent_id` 做有界 LRU 去重，MotionController 动作队列有界（默认 8），Host 侧 `IntentTracker` 回收超过 `deadline_ms` 未收到终态的 intent。
- 配置设备 token 时，控制 `/ws/kt2` Upgrade 必须携带 `X-Naobot-Token`；Host 同时只接受一个 owner 绑定的控制连接。Dashboard WebSocket 配置 token 时使用会话 token 鉴权，未配置时仅允许 loopback。固件使用 Host heartbeat 建立短期时钟锚点，在 ACK 前拒绝过期 intent、无可靠时钟的运动 intent、活跃反射下的非 stop intent 和所有越界/裸硬件参数。
- Host 使用容量 32 的有界优先级事件队列；高优先级优先、同优先级 FIFO，队列满时驱逐较低优先级或拒绝新事件。
- Host heartbeat 默认每 2 秒发送，独立于 6/15 秒推理预算。
- Dashboard 提供状态、日志、Soul、Memory、Routine、People、白名单动作测试和急停，不提供编程 IDE 或任意代码执行。
- Memory 与 Routine 默认待确认；Routine 只允许白名单动作。
- 控制连接由 MicroPython `_thread` connection worker 隔离握手；Camera、I2S 与媒体 WebSocket 的创建、采集、收发、重连和关闭由独立媒体 runtime 线程完整独占。固件 50 ms 本地循环只交换事件加速指令和媒体健康快照，`_thread` 不可用时媒体禁用。
- 固件支持 ECDSA P-256 签名、SHA-256 完整性校验、ESP32-S3 应用镜像校验和双 OTA app 分区切换；激活、启动健康确认与回滚使用 NVS 事务阶段恢复，并校验当前运行分区确为持久化目标槽。整镜像 finalize/activate 由 OTA 专属 worker 串行执行，50 ms 主循环只 submit/poll。运动 inhibit 使用 owner 语义：OTA coordinator 从请求受理到安全 abort/fail 解锁前持有 install owner，重启后的 BootHealthMonitor 在 pending unknown、健康观察、确认或回滚失败期间持有独立 boot-health owner，双方不能释放对方的锁；只有 `mark_healthy()` 成功或明确确认没有 pending OTA 事务时才释放 boot-health owner。锁定期间网络 intent 与动作 tick 均不能启动非 stop 动作，舵机 OE 保持 disabled；stop 与 L0 反射仍保留最高安全语义。finalize/activate timeout、已切换启动槽但重启失败时同样 fail-closed。构建脚本把已校验公钥复制到 workspace 固定头文件，并将来源路径和内容 SHA-256 纳入 CMake 依赖，避免复用 workspace 时嵌入旧 key。OTA 只能写入非运行分区，固件本地安全循环和最终拒绝权不受升级流程覆盖。

## 不做项

- 不做 Blockly/Blockley 或儿童积木编程。
- 不做 99 个游戏。
- 在边缘检测、测距、区域约束和实机安全验收完成前，不启用自主桌面探索或靠近桌边的移动。
- 不做 LLM 直接控制舵机角度、PWM、servo id 或裸硬件字段。
- 不做未经确认的长期记忆写入。
- 不把 Dashboard 做成通用编程 IDE。
- 当前不做全数据库加密、`wss://`、AEC 或 barge-in。

## 验收边界

当前代码与自动化测试覆盖 Agent/runtime、自动路由、加密范围、身份注册、People API、控制/媒体协议、队列、半双工、固件连接 worker 和反射隔离。这些证据只代表对应能力达到原型或自动化验证阶段，不代表完整产品已经完成。成熟度和缺口见 `docs/reviews/2026-07-14-product-capability-audit.md`，完成定义见 `docs/product/acceptance.md`。

真实 XIAO ESP32-S3 Sense 硬件仍未验收。2026-07-24 已在 HEAD `891e60a` 使用 MicroPython v1.28.0、ESP-IDF v5.5.1 和 esp32-camera v2.1.6 完成项目定制镜像的真实 C 编译：OTA 应用镜像 `micropython.bin` 为 1,840,272 字节，小于 `0x280000` app 分区，SHA-256 为 `a37a04eca9fd9b5da258aff3bad56b4a4db3a8619fa4122ba22342674d858ebe`；首次烧录合并镜像 `firmware.bin` 为 1,971,344 字节，SHA-256 为 `dc262af5230a141538b683c530a4192c93b97cf9b712829760c9126434e3363c`。该证据仅证明构建、链接和分区尺寸通过，尚未证明真实烧录、签名 OTA、失败回滚、OV2640、PDM/I2S、PSRAM、USB CDC、OLED、MPU6050、触摸、舵机或 30 分钟稳定性。

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
- 2026-07-13：将 Camera、I2S 和媒体 WebSocket 的完整生命周期移入单一 `MediaRuntimeWorker` 线程；安全主循环仅通过 mailbox/快照交换标量状态，媒体线程不可用时安全禁用，不再只隔离握手。
- 2026-07-13：Dashboard 使用内联 favicon，避免诊断页面产生无意义的 `/favicon.ico` 404 控制台错误；产品行为不变。
- 2026-07-13：最终安全评审修复控制发送背压自旋、断链动作残留、重复控制连接劫持、Dashboard WS 鉴权、控制连接超时和媒体设备瞬态恢复；媒体 turn 冻结入队人物，断线取消 touch 推理并报告 `MEDIA_QUEUE_FULL`；runtime reset/delete 使用 activity gate 与 generation/tombstone 防止旧 session 复活数据。
- 2026-07-14：产品目标从“软件 MVP”调整为面向家庭多人、默认儿童安全、高度主动、联网优先的完整桌面陪伴机器人；引入九大产品能力域和 L0-L5 成熟度，受限桌面探索在边缘/距离感知和实机安全验收前保持禁用。
- 2026-07-14：Phase 1 软件基础——OLED/蜂鸣器动作改为 tick/cancel 化 `DisplaySkill`/`BuzzerSkill`（与 `PoseSkill` 同构），解除动作队列对安全循环的阻塞；intent 回执扩展为 `accepted`/`completed`/`failed` 三态，固件 MotionController 增加有界队列（默认 8）、`intent_id` LRU 去重和 skill 完成回调，Host 侧新增 `IntentTracker` 状态机与 deadline 超时回收；控制 WS 触摸事件经 `MediaService.route_touch_event` 桥接到媒体注册流程，`awaiting_touch` 时摸头确认完成注册并跳过重复 intent。本次仅到 L2 自动化验证，未做目标硬件实机验收。
- 2026-07-19：Stage 1 OTA 软件基础——新增 ECDSA P-256 签名、SHA-256/芯片/应用镜像校验、双槽写入与切换、NVS 事务恢复、启动健康确认和失败回滚；修复 MicroPython v1.28 usermod 头文件、Camera QSTR include、冻结模块名和 OTA 应用镜像尺寸检查，并明确 OTA 打包输入为 `micropython.bin` 而非首次烧录合并镜像。真实 C 编译已生成 1,833,040 字节的 `micropython.bin`，但烧录、签名升级、断电恢复和回滚仍待 XIAO ESP32-S3 Sense 实机验收。
- 2026-07-19：Stage 1 OTA 最终安全收口——健康确认增加 running partition 与持久化 target address 一致性校验；整镜像 finalize/activate 移入专属 worker，原生长操作释放 MicroPython GIL，安全主循环只进行 submit/poll；构建脚本对每个 make fail-fast 并删除旧应用产物。`begin(image_size, expected_sha256_bytes, sequence)` 的三参数形式是防降级安全契约，不提供不带 sequence 的兼容入口。
- 2026-07-24：在 HEAD `f7517ba` 重新完成 Stage 1 OTA 真实 C 编译、链接和分区尺寸检查；`micropython.bin` 为 1,837,072 字节，SHA-256 为 `e7e34b3084f564bd4dae44f7490ae74369a9b30f39e7a621793e25e49f8f2f10`，并以临时 P-256 私钥完成真实镜像的 Host 打包自验。真实烧录、设备端签名升级、断电恢复、回滚和 30 分钟稳定性仍未验收。
- 2026-07-24：修复 Stage 1 OTA 最终评审 Important——新增 `MotionController` 与 OTA coordinator 共享的运动 inhibit，受理升级后立即取消当前/队列并拒绝非 stop intent，只有 native session 安全 abort 且 OE 禁用确认后才解锁；timeout、activate 后等待重启和重启失败保持 fail-closed。OTA 公钥改为携带来源路径/内容指纹的 workspace 固定构建输入，并显式加入 CMake configure 依赖；已使用两个临时 P-256 公钥在同一 WSL workspace 连续真实构建，CMake 与 `modnao_ota.c` 均重新执行，两个镜像哈希不同且各自只包含对应公钥标记。设备端换 key 验签仍未实测。
- 2026-07-24：修复 Stage 1 OTA abort 幂等与失败重试契约——write/SHA/digest 等失败路径已自行安全清理时，协调器的二次 native abort 可幂等确认；`esp_ota_abort` 失败时保留活动 handle/目标分区供后续重试，只有 native cleanup、OE 禁用和 motion cleanup 均确认后才释放运动 inhibit。该修复仅有自动化源码契约与协调器测试证据，真实设备 abort 失败注入、签名升级、断电恢复、回滚和 30 分钟稳定性仍未验收。
- 2026-07-24：在 HEAD `ce8a77e` 对 abort 幂等修复后的最终源码重新执行真实 C 编译；`micropython.bin` 为 1,838,064 字节，SHA-256 为 `c522a2af4b1031179db2d5629ad63dd3eee58c321a9e2eb73645f83fc141534a`，并以一次性 P-256 私钥完成 Host 打包自验后删除私钥。同步修正文档中的 XIAO ESP32S3 Sense profile、构建目录和验收边界；目标硬件验证状态不变。
- 2026-07-24：修复 Stage 1 OTA 重启健康观察的运动锁缺口——`MotionController` 改为多 owner inhibit，BootHealthMonitor 在 pending unknown、健康窗口、`mark_healthy` 异常与 rollback 失败期间持续持有独立 boot-health owner；真实 `MotionController`、`ActionPlayer`、`ServoBank` 与 OE gate 集成测试证明 pending-verify 期间 `sit` 被拒绝且 OE 不会拉低，并验证 boot-health/coordinator owner 不会互相解锁。HEAD `c1d84bb` 的冻结模块与原生 C 已重新编译，`micropython.bin` 为 1,838,560 字节，SHA-256 为 `5b010674879b3829572918d29dbbd9da16a34aa245addcfb62f172a57dd785d3`。该证据仍仅为构建与自动化软件/fake I2C/OE 集成验证；真实设备 pending-verify、回滚失败注入、签名升级和 30 分钟稳定性未验收。
- 2026-07-24：修复 Stage 1 OTA native session 生命周期失配——原生模块新增可信 `session_active()` handle 状态，coordinator 在新请求、partial begin 异常及 soft reset 后均以 native 状态为准；active 或状态 unknown 时持续持有 OTA motion owner、保持 OE disabled，并通过专属 worker 跨 tick 重试 abort，只有确认 native inactive 后才释放 owner 和允许后续请求。HEAD `58b453b` 已重新生成冻结模块并编译原生 C；`micropython.bin` 为 1,839,888 字节，SHA-256 为 `06c3ac2c7cc00c2187b3ad309711a03a04931f7e9ce9889032dffc52a4e29a8e`。本轮尚无真实设备 partial begin、abort 失败、soft reset 恢复、签名升级、回滚和 30 分钟稳定性证据，相关项目仍未验收。
- 2026-07-24：修复 Stage 1 OTA cleanup dependency 异常逃出安全循环——fail-closed 兜底不再同步递归重入 cleanup；worker poll/submit、motion inhibit 获取与释放等外部清理边界独立捕获异常，每个 50ms tick 最多尝试一次并跨 tick 重试。持续 poll 异常期间 L0 可继续执行，OTA motion owner 与 OE disabled 保持不变；只有 worker abort 完成且 native session 明确 inactive 后才释放运动 inhibit。HEAD `891e60a` 已重新生成冻结模块并编译原生 C；`micropython.bin` 为 1,840,272 字节，SHA-256 为 `a37a04eca9fd9b5da258aff3bad56b4a4db3a8619fa4122ba22342674d858ebe`。真实设备 cleanup 依赖故障、签名升级、回滚和 30 分钟稳定性仍未验收。
