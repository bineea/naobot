# naobot AI Agent 工作入口

所有 AI agent 进入本仓库后先读本文件。任何完成状态都必须来自当前 HEAD 的代码、测试或带时间戳实测记录，不能复制旧文档中的完成声明。

## 必读顺序

1. `docs/product/prd.md`：唯一产品事实源与事实边界。
2. `docs/product/roadmap.md`、`docs/product/acceptance.md`：软件已完成项与 N16R8 待验收项。
3. `docs/architecture/system-architecture.md`：L3/L2/L1/L0、双 WebSocket、runtime 与媒体组件。
4. `docs/architecture/protocol.md`：控制 envelope、媒体 hello、24 字节帧头、TTS、分片和 heartbeat。
5. `docs/architecture/safety-policy.md`：控制优先级、隐私、People 鉴权和明文局域网风险。
6. `docs/development.md`：环境、全部 `NAOBOT_*` 配置、People 调试、定制固件构建/烧录。
7. `docs/decisions/ADR-0003-agentscope-brain-runtime.md` 与 `docs/agents/review-checklist.md`。

## 当前实现事实

- L3 只允许 `goal/text/expression/skills/memory_suggestion`；L2 确定性编译兼容 `actions` 并忽略 LLM `actions`；L0 固件反射最高优先级。
- 自动路由 `score >= 4` 组队，`needs_team=true` 或 `confidence < 0.65` 自升级；团队为情绪、行为、安全三专家加负责人，安全事件不组队。
- 单 Agent 默认 6 秒，团队总预算 15 秒，ReAct 最多 4 轮；Toolkit 为空，失败走 fallback。
- 已识别人员 runtime 持久化到 SQLite WAL；visitor/guest runtime 仅内存并在连接结束时销毁。
- 控制 `/ws/kt2` 与媒体 `/ws/media` 分离；媒体支持 hello/token、10/15 FPS、VAD、TTS 和半双工。
- People 注册要求未知单人、5 张样本、口头确认、摸头确认和 `NAOBOT_DATA_KEY`；People API 有鉴权。

## 事实与隐私红线

- 会话激活前不得调用 cloud provider 或 Agent；RAM 音视频窗口不得落盘。
- Fernet 只覆盖人脸 embedding 和 5 张注册样本，严禁写成“全数据库加密”。
- 未知身份必须与已识别人员 runtime 隔离；未经确认不得写长期 Memory 或启用 Routine。
- 不给 AgentScope 增加 shell、文件、Python、MCP、硬件工具或自主长期记忆。
- 不让 LLM 输出裸舵机角度、PWM、GPIO、servo id 或绕过 `PolicyGuard`。
- 当前仅支持明文 `ws://`；不得声称 `wss://`、AEC、barge-in 或任意 RFC 扩展已完成。

## 硬件声明红线

仓库 generic bin 不含定制 `camera` 模块；构建配方不等于真实 C 编译。没有实测记录时，不得声称真实 bin 已生成、N16R8/OV2640/I2S/PSRAM/CH343/舵机已验证或 30 分钟指标已通过。模拟器、CPython fake、协议测试和 Python 语法检查都不能冒充硬件验收。

## 常用命令

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m pip install -e ".[media-local]"
git config core.hooksPath .githooks
.\.venv\Scripts\naobot.exe serve
.\.venv\Scripts\naobot.exe simulate --event touch_head
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
git diff --check
```

文档同步检查依赖暂存区：先暂存本次允许文件，再运行 `.\.venv\Scripts\python.exe tools/check_prd_sync.py`。代码变更必须同步暂存 `docs/product/prd.md`。

## 工作协议

- 修改前核对 `git status` 和当前 HEAD，不回退其他人的工作。
- 只改任务明确授权的路径；新增跨模块事实时同步 PRD、架构、协议、安全、验收和 ADR。
- 评审使用 `docs/agents/review-checklist.md`，先列风险、测试缺口和硬件事实边界。
- 交付时列出修改文件、验证命令、未执行的硬件项目和 commit hash。
