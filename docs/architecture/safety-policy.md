# 安全策略

## 默认策略

naobot 安全策略采用默认拒绝。所有动作必须来自白名单，并经过 `PolicyGuard` 校验。

## 动作白名单

- `set_face`
- `blink`
- `wave`
- `small_step_forward`
- `turn_left`
- `turn_right`
- `gentle_nudge`
- `sit`
- `chirp`
- `sleep`
- `stop`

## 拒绝规则

- 未知动作拒绝。
- 含裸硬件字段或危险关键词的动作拒绝。
- 过期 intent 拒绝。
- 低电状态下位移动作拒绝。
- fault 或异常姿态下位移动作拒绝。
- 未确认 Memory 不进入长期记忆。
- 未确认 Routine 不自动启用。

## LLM 边界

- LLM 只能生成文本、白名单动作和待确认记忆建议。
- LLM 不能直接控制舵机角度、PWM、servo id 或其他裸硬件字段。
- LLM 调用失败、超时或返回格式错误时，宿主机使用规则 fallback。
- fallback 不绕过 `PolicyGuard`。

## Dashboard 边界

- Dashboard 只提供状态、动作测试、急停、Soul、Memory、Routine 和诊断日志。
- Dashboard 不提供 Blockly、脚本编辑器、终端、任意代码执行或底层舵机控制。

## 固件边界

- 固件是最后一道安全线。
- 即使宿主机失联或协议异常，固件也要保持本地降级、动作限幅和安全停止。
- 固件不保存长期记忆，不调用 LLM，不实现图形化编程。

