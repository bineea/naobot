# 安全策略

## 控制优先级

naobot 默认拒绝，低层控制权不能被高层覆盖：

```text
急停 > 反射安全 > 小脑姿态控制 > 固件技能 > Host/LLM intent > Routine/idle
```

L3 只输出 `goal/text/expression/skills/memory_suggestion`；L2 `BehaviorRuntime` 忽略 LLM `actions`，确定性生成兼容动作并通过 `PolicyGuard`。固件再次校验并保留最终拒绝权。

## 动作与 LLM 边界

- 白名单为 `set_face`、`set_expression`、`blink`、`wave`、`small_step_forward`、`turn_left`、`turn_right`、`gentle_nudge`、`sit`、`chirp`、`sleep`、`stop`。
- 未知动作、裸硬件字段、危险关键词、越界表情、过长持续时间、过期 intent、低电运动和异常姿态运动一律拒绝。
- MPU6050 缺失或读取失败按 `unknown` 处理；`fallen`/`unknown` 时禁止位移动作。
- AgentScope Toolkit 为空，不提供 shell、文件、Python、MCP、硬件工具或任意代码执行。
- 自动路由 `score >= 4` 才直接组队；单 Agent `needs_team=true` 或 `confidence < 0.65` 可升级。团队最多三专家加负责人，安全事件绝不组队。
- 单 Agent 6 秒、团队 15 秒、最多 4 轮；未配置、超时、异常或非法输出进入 fallback，fallback 仍经过 L2 和 `PolicyGuard`。

## 会话与媒体隐私

- 唤醒词、短问候、摸头或持续目光激活会话之前，不调用 cloud ASR/TTS/vision provider，也不调用 Agent。
- 视频 10 秒、音频 15 秒和时序摘要仅保存在 RAM 窗口；原始短期媒体不写文件、不写 SQLite、不写日志。
- Agent runtime 持久化前必须把 base64/URL 媒体清洗为摘要与 SHA-256。
- 未知人员使用隔离的 visitor runtime，只在内存存在，连接结束时销毁；不能复用已识别人员上下文。
- 身份注册仅接受未知单人，要求 5 张样本、口头确认和摸头确认；没有 `NAOBOT_DATA_KEY` 时必须拒绝注册。
- Fernet 只加密人脸 embedding 和这 5 张注册样本。`naobot.db` 不是全数据库加密，people 元数据、conversation session 和 Agent runtime state 是普通 SQLite 内容。
- 未确认 Memory 不进入长期记忆；未确认 Routine 不启用。

## People API 鉴权

- `GET /api/people`、`POST /api/people/{person_id}/runtime/reset`、`DELETE /api/people/{person_id}` 和 `POST /api/people/enrollment/cancel` 都执行鉴权。
- 配置 `NAOBOT_DEVICE_TOKEN` 时，客户端必须提供 `Authorization: Bearer <token>` 或 `X-Naobot-Token`；比较使用恒定时间函数。
- 未配置 token 时只允许 loopback；非本机请求返回 403。People 页面或 API 不得成为绕过身份确认和删除审计的入口。

## 传输与音频限制

- 固件当前只支持明文 `ws://`，不支持 `wss://`。同一局域网中的监听、伪造、中间人和 token 泄露风险仍存在；部署时应使用受信任隔离网络、限制 Host 监听地址并保护 device token。
- 已实现音频半双工，不是全双工音频：TTS 期间只暂停麦克风上行，摄像头继续按 10/15 FPS 上传；固件排空后恢复麦克风，Host 再等待 200 ms。AEC 与 barge-in 未实现，不得在文档或产品承诺中宣称完成。
- 媒体坏帧、队列溢出、TTS/provider 异常、媒体断线或分片错误只影响媒体状态；不能发送运动 intent、改变 `control_authority` 或覆盖反射。

## 固件最终安全线

- 固件反射始终优先于 Host intent、媒体、TTS 和网络重连。
- Host 心跳超时后取消 Host skill，进入本地自治；反射仍可执行。
- 语义 `skills/expression` 优先于兼容 `actions`，避免重复动作。
- 动作未实现或执行失败必须返回 `error`，不能回假 `ack`。
- 固件不保存长期记忆、不调用 LLM、不实现 Dashboard/Blockly，也不把硬件对象交给连接 worker 线程。
- 未实现力反馈、电流检测、触觉或边缘检测前，不启用夹持或桌边反射。

## 硬件事实边界

CPython fake、协议测试、Python 语法检查和静态构建配方不等于硬件安全验收。当前没有真实 C 编译、项目定制 bin、N16R8、OV2640、I2S、PSRAM、CH343、舵机或 30 分钟稳定性实测记录；在记录产生前必须维持“未验收”状态。
