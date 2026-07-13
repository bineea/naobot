# naobot PRD

## 背景

KT2 是一个口袋尺寸桌面机器人。naobot 的目标是在复刻 KT2 的基础上，引入 LLM 宿主机 Agent，让机器人从固定动作玩具升级为可配置、可观察、可安全扩展的智能机器人。

本 PRD 是项目唯一主 PRD。实现细节放在 `docs/architecture/`，开发流程放在 `docs/development.md`，agent 协作规则放在 `docs/agents/`。

## 目标用户

- 复刻 KT2 的个人开发者。
- 想用 LLM 扩展桌面机器人的 maker。
- 需要在真实硬件前先用模拟器验证行为的开发者。
- 使用 AI agent 协作推进功能开发、测试和评审的项目维护者。

## MVP 目标

- 宿主机运行 Agent 和 Dashboard，负责理解事件、调用 LLM 或规则 fallback、生成安全 intent。
- ESP32 固件负责事件采集、动作执行、本地安全守卫和失联降级。
- 分层自治原则：LLM 负责语义意图、情绪表达和复杂行为选择；固件小脑负责实时运动协调、姿态恢复和安全反射；固件安全层拥有最高控制权。
- 模拟器可在没有真实硬件时完成 event -> intent -> ack 闭环。
- 动作、记忆、routine 均有安全边界和人工确认机制。
- 项目文档支持 AI-native 协作，agent 能根据统一入口继续拆解和实现功能。

## 核心能力

- Dashboard：查看状态、测试白名单动作、急停、配置 Soul、管理 Memory、管理 Routine、查看诊断日志。
- Host Agent：基于 `agentscope==2.0.4` 的 AgentScope Agent 调用 OpenAI-compatible 模型；L3 大脑仅输出 `goal`、`text`、`expression`、`skills`、`memory_suggestion`，未配置、超时、异常或非法输出时使用安全规则 fallback。
- BehaviorRuntime：L2 行为层确定性编译 L3 语义输出，忽略 LLM 返回的 `actions`，只由白名单 `skills` 和参数化表情生成兼容动作。
- 协议：支持 `event`、`intent`、`ack`、`status`、`error`、`heartbeat` envelope。
- 大脑运行约束：单次推理默认 4 秒超时、ReAct 最多 4 轮；复杂请求最多启用 3 个专家 agent，并由产品负责人 agent 收敛；安全事件禁用组队。
- 安全白名单：只允许 `set_face`、`set_expression`、`blink`、`wave`、`small_step_forward`、`turn_left`、`turn_right`、`gentle_nudge`、`sit`、`chirp`、`sleep`、`stop`。
- 工具边界：AgentScope `Toolkit` 为空，禁止 shell、文件、Python、MCP、硬件工具调用和自主长期记忆。
- 运行队列与心跳：Host 使用容量 32 的有界优先级事件队列；Host heartbeat 每 2 秒发送，独立于大脑推理。
- Memory：长期记忆默认待确认，可查看、确认、删除。
- Routine：只允许白名单动作，默认待确认。
- Firmware：MicroPython 骨架包含硬件抽象、通信、本地降级、动作执行和安全守卫。

## 不做项

- 不做 Blockly/Blockley 或儿童积木编程。
- 不做 99 个游戏。
- 不做自主桌边移动。
- 不做 LLM 直接控制舵机角度、PWM、servo id 或裸硬件字段。
- 不做未经确认的长期记忆写入。
- 不把 Dashboard 做成通用编程 IDE。

## MVP 验收

- `pytest` 和 `ruff check .` 通过。
- `naobot serve` 可启动 Dashboard。
- `naobot simulate --event touch_head` 可完成 event -> intent -> ack。
- 未配置 LLM 时系统使用规则 fallback，并在 Dashboard 状态中可见。
- AgentScope Brain 状态可观察，非法输出、超时、异常和未配置模型均降级到 fallback。
- Dashboard 急停能生成并下发 `stop` intent。
- Memory 和 Routine 默认待确认。
- 固件目录包含可通过 `mpremote` 上传的 MicroPython 源码骨架和说明。

## 变更记录

- 2026-06-23：建立 AI-native 文档结构，确认 `docs/product/prd.md` 为唯一主 PRD；代码更新必须同步检查并暂存本文件。
- 2026-06-30：同步固件 demo 脚本（oled.py、scan_i2c.py、servo_180.py、servo_360.py）为示例/实验性实现，功能范围未改变，**无需需求变更**。
- 2026-06-30：新增 `firmware/esp32/demo/mpu6050_reader.py` 传感器读取示例（I2C/MPU6050 初始化与姿态数据获取），功能范围未改变，**无需需求变更**。
- 2026-06-30：主固件集成 SSD1306 OLED 显示和 MPU6050 姿态检测；MPU6050 缺失或读取失败按 `unknown` 姿态处理并禁止运动动作。
- 2026-07-08：主固件接入 WiFi 与明文 `ws://` WebSocket client，可上报事件、接收 intent、回传 ack/error；网络失败时继续本地 fallback。
- 2026-07-08：固件执行层补齐 Host 白名单动作与表情；动作执行失败返回 `error`，避免未执行动作被误认为成功。
- 2026-07-09：修复固件 MicroPython 兼容性问题，移除 `dataclasses` 依赖，并避免 WebSocket 随机数生成超过 `getrandbits(32)` 限制；功能范围未改变，**无需需求变更**。
- 2026-07-09：优化 0.96 寸 OLED 表情显示，从窄小文本升级为 128x64 大图形表情；产品目标不变。
- 2026-07-09：OLED 表情升级为眼睛驱动短动画帧，支持待机左右看、开心眯眼、警觉抖动、困倦缓慢闭眼和快速眨眼；Host 协议和动作白名单不变。
- 2026-07-09：OLED 眼睛视觉改为圆圆护目镜风格，使用圆形眼白、圆形瞳孔、高光和眼睑表达情绪；不改变 Host 协议、动作白名单或安全策略。
- 2026-07-09：调大 OLED 圆眼黑色瞳孔，提升 0.96 寸屏幕上的可读性和可爱感；功能范围不变。
- 2026-07-09：进一步放大 OLED 圆眼黑色瞳孔，让 0.96 寸屏幕预览和实机显示更饱满；功能范围不变。
- 2026-07-10：引入分层自治控制：Host 支持 goal/expression/skills 语义 intent，固件新增本地反射安全、可中断运动调度、控制权状态上报和最终安全拒绝权。
- 2026-07-10：增加 Host/固件双向 heartbeat 和链路健康状态；固件在大脑心跳超时后进入本地自治，Dashboard 展示链路状态和心跳年龄。
- 2026-07-13：同步 AgentScope Brain 已实现事实：宿主机使用 `agentscope==2.0.4` Agent/OpenAI-compatible 模型；L3 仅输出 `goal/text/expression/skills/memory_suggestion`；L2 `BehaviorRuntime` 确定性编译并忽略 LLM `actions`；空 Toolkit 禁止 shell、文件、Python、MCP、硬件工具和自主长期记忆；未配置、超时、异常、非法输出 fallback；复杂请求最多三专家加产品负责人收敛，安全事件禁用组队；4 秒/4 轮、有界优先级队列容量 32、Host heartbeat 2 秒独立于推理；语义字段优先于兼容 actions，避免固件重复执行；现有 envelope 类型保持兼容。未宣称真实硬件已验证。
