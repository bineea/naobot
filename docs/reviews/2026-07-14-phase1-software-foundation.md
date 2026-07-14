# Phase 1 软件基础推进审计（2026-07-14）

## 审计目的

本报告记录 `feature/agentscope-brain` 分支在 `2026-07-14-product-capability-audit.md` 之后的一次阶段性推进：Phase 1 软件基础（A→B→C）。它不覆盖产品能力审计，只记录本次改了什么、验证到什么程度、对成熟度的影响，以及仍存在的缺口。长期目标见 `docs/product/prd.md`，推进顺序见 `docs/product/roadmap.md`，完成定义见 `docs/product/acceptance.md`。

本次推进的提交：`e0b71b9 feat: add Phase 1 software foundation`（已推送 `origin/feature/agentscope-brain`）。

## 范围与约束

按用户确认推进 **Phase 1 软件基础**，三项工作串行（A → B → C）：

- **A**：OLED/蜂鸣器动作 tick/cancel 化，解除动作队列对安全循环的阻塞。
- **B**：intent 回执三态 + 有界队列 + 去重，建立可信 intent 生命周期。
- **C**：触摸确认链路桥接，打通控制 WS 触摸事件到媒体注册流程。

**约束**：无真实 N16R8 硬件，仅做软件实现 + 自动化测试，交付物声称上限为 **L2 自动化验证**，不声称实机已通过。本机 `generic` MicroPython bin 不含项目定制 `camera` 模块，不能作为媒体固件验收产物。

## 本次改动

### A. OLED/蜂鸣器 tick/cancel 化

- `firmware/esp32/hardware/display.py`：抽取模块级 `FACE_ANIMATIONS`（idle/happy/alert/sleepy 的帧序列与延时）和 `BLINK_DELAY_MS`；`_animate` 改为查表（行为不变）；新增公共 `render_frame(frame, status=None)` 供 Skill 调用。同步 `set_face/blink/set_expression/show_status` 保留不变。
- `firmware/esp32/hardware/buzzer.py`：新增公共 `play_step(freq, duration_ms)` 与 `off()`；`chirp` 内部改用 `play_step` 循环（行为不变）。
- `firmware/esp32/motion/action_player.py`：新增 `DisplaySkill`、`BuzzerSkill`（与 `PoseSkill` 同构的 `start/tick/cancel`）；`build_skill` 把 `set_face`/`blink` 路由到 `DisplaySkill`，`chirp` 路由到 `BuzzerSkill`（无 buzzer 时回退 `ImmediateSkill`）；`set_expression`/`sit`/`sleep`/`stop` 仍走 `ImmediateSkill`（单帧/瞬态，不阻塞）。`DisplaySkill.start` 只设状态不渲染（仿 `PoseSkill`），由 `MotionController.tick` 逐帧推进。

**已知残留（本次不修）**：reflex 路径 `display.set_face("alert")` 仍同步阻塞 ~280ms（reflex 罕见且为现有行为）。

### B. intent 回执三态 + 有界队列 + 去重

- 协议（`docs/architecture/protocol.md` 同步）：`ack.payload.status` 三态——`accepted`（已入队，非终态）、`completed`（动作自然播完，终态）、`failed`（被 reflex/中断取消，携带 `reason`，终态）；`error` 信封保留用于入队前拒绝（`POLICY_DENIED`/`REFLEX_ACTIVE`/`INTENT_EXPIRED`/`HOST_CLOCK_UNAVAILABLE`/`INVALID_INTENT`/`EXECUTION_FAILED`），不与 `ack failed` 混用。向后兼容：旧 host 只 log、旧固件只发 `accepted`。
- `firmware/esp32/control/motion_controller.py`：`__init__` 增加 `queue_capacity=8`、`seen_capacity=32`、`on_intent_done` 回调；`submit_action` 入队前检查容量，满时返回 `(False, "motion queue full")`；`submit_intent` 维护 `_seen` LRU 去重（重复 `intent_id` 返回 `(True, "duplicate")` 不重入队）和 `_pending` intent 分组计数；`tick` 在 skill 自然完成时调 `on_intent_done(completed)`；`cancel` 对所有 `_pending` 调 `on_intent_done(failed, reason)`；部分入队失败时 `_abort_intent` 清理该 intent 的已入队 skill。
- `firmware/esp32/main.py`：`FirmwareProtocol.ack` 增加 `reason`；`execute_intent` 三态路由（motion 路径 duplicate→`accepted`、队列满→`error EXECUTION_FAILED`、入队成功→`accepted`，终态由回调发；非 motion 路径同步执行成功→`completed`、失败→`error EXECUTION_FAILED`）；`main()` 调整初始化顺序，新增 `_on_intent_done` 回调通过 `network_state["ws"]` 回执。
- `src/naobot/intent_tracker.py`（新增）：Host 侧 `IntentTracker`——`track` 去重、`observe_ack`/`observe_error` 终态移除、`reclaim` deadline 超时回收、有界 LRU（容量 64）。
- `src/naobot/agent.py`：`NaobotAgent` 接入 `IntentTracker`；`observe_robot_message` 处理 ACK/ERROR 更新状态；`create_intent` 调 `track`（重复 log `intent_dedup`）；新增 `reclaim_stale_intents`。
- `src/naobot/server.py`：`heartbeat_worker` 周期调 `agent.reclaim_stale_intents()`。

### C. 触摸确认链路桥接

- `src/naobot/media/service.py`：新增 `route_touch_event(name, person_id)`，与 `_handle_control_json` 的 `touch_head` 分支对称，但**不调用 `_emit_touch_intent`**（控制 WS 路径的 intent 由 `event_worker` 产生）。`touch_head` 在 `awaiting_touch` 时完成注册返回 `True`（消费，跳过 intent），其余返回 `False`（产 intent）；`touch_back` 仅激活会话，恒返回 `False`。
- `src/naobot/server.py`：`ws_kt2` 的 EVENT 块在 `event_queue.put` 前桥接，`consumed=True` 时 `continue` 跳过 intent 创建，规避双重 intent。
- 固件 `hardware/touch.py` 的 stub **保持不变**（触摸驱动真实化是另一项工作，需硬件验收）；本次只保证 Host 链路通畅。

## 验证证据

```
.\.venv\Scripts\python.exe -m pytest -W ignore -q   → 全量通过（含 34 个新增测试）
.\.venv\Scripts\python.exe -m ruff check .          → All checks passed
.\.venv\Scripts\python.exe tools/check_prd_sync.py  → PRD_SYNC_OK
git diff --check                                    → DIFF_CHECK_OK
```

新增测试分布：
- `tests/test_firmware_tick_skills.py`（15）：DisplaySkill/BuzzerSkill 逐帧推进、cancel 中断、build_skill 路由、MotionController tick 驱动至完成、reflex cancel 中断显示/蜂鸣器。
- `tests/test_intent_tracker.py`（10）：去重、completed/failed/error 终态、deadline 回收、LRU 淘汰。
- `tests/test_firmware_network.py`（+5）：accepted→completed 时序、duplicate 去重、队列满 EXECUTION_FAILED、reflex cancel failed、非 motion 路径 completed。
- `tests/test_media_server.py`（+4）：route_touch_event 完成注册/无 pending 产 intent/touch_back/未知 name，及 completed 时不产 intent。

现有测试零回归：`test_websocket_touch_head_to_intent`（无 pending 仍产 intent）、`test_execute_intent_*`、`test_firmware_hardware.py`（Display 同步方法）、`test_firmware_actions.py`（`execute()` 同步路径）等全部保持通过。

测试模式：复用 CPython fake 驱动（`FakeDisplay`/`FakeBuzzer`/`FakeServos` + `lambda: current_time["value"]` 时钟驱动），手动推进 `current_time` + `motion.tick()` 模拟安全循环，不依赖真实 `sleep_ms` 或硬件。

## 成熟度影响

本次推进**未改变任何能力域的成熟度等级**——所有改动停留在 L2 自动化验证，未做 L3 实机闭环。但它缩小了既有审计的 P0/P1 缺口：

| 能力域 | 审计前 | 本次推进 | 仍缺 |
| --- | --- | --- | --- |
| 身体表达 | L2 | OLED/蜂鸣器动作不再阻塞安全循环；intent 回执三态/有界队列/去重就位 | 舵机方向/零位/限位/速度/步态校准；reflex 路径 `set_face("alert")` 仍同步阻塞 ~280ms；实机动作调优 |
| 自然交流 | L2 | 触摸注册确认链路打通（控制 WS → 媒体注册） | 真实 ASR/TTS provider 开箱闭环；TTS 下行无采样率校验/重采样/限幅；对视仍为"单人脸=1.5s"占位；触摸/电量固件驱动仍 stub |
| 可靠性与产品化 | L1 | intent 生命周期可信（终态回执/超时回收/去重/有界队列） | 触摸/电量仍 stub；无定制实测 bin、OTA、配网、看门狗、校准、长期硬件记录 |
| 生命感与主动性 | L1 | 未推进 | 动机/精力/兴趣/打扰预算/冷却/主动仲裁/反馈学习仍完全缺失 |

## 遗留项与下一步

本次未触及的 P0/P1 缺口（按推荐推进顺序）：

1. **Phase 1 剩余**：TTS 下行 PCM16 契约加固（校验/重采样/限幅）；对视判断真实化（注入真实 eye_contact_estimator）；reflex 路径 OLED 阻塞消除。
2. **Phase 1 感知驱动**（需硬件）：`hardware/touch.py` 接 `machine.TouchPad`、`hardware/power.py` 接 ADC 与充电状态 GPIO；首次 N16R8 实机 30 分钟基线验收。
3. **Phase 2**：Memory 从全局 JSON 迁到按人物/家庭/访客隔离；监护人/成人/儿童/访客角色权限模型与儿童最小化持久化；`conversation_sessions` 生产会话生命周期。

## 证据边界

- 本次自动化测试证明的是软件逻辑和 fake 环境，不证明 L3 以上产品成熟度。
- 没有带时间戳的真实 N16R8 连续运行、家庭试用和儿童安全记录，因此本次推进不改变任何能力域达到 L4/L5 的状态。
- 本报告应在下一次关键阶段验收后新增下一份日期化审计，不覆盖历史报告。
