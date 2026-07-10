# 安全策略

## 默认策略

naobot 安全策略采用默认拒绝。所有动作、技能和参数化表情必须来自白名单，并经过 `PolicyGuard` 校验。

控制优先级：

```text
急停 > 反射安全 > 小脑姿态控制 > 固件技能 > Host/LLM intent > Routine/idle
```

## 动作白名单

- `set_face`
- `set_expression`
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
- 未授权技能、越界表情参数或过长表情持续时间拒绝。
- 过期 intent 拒绝。
- 低电状态下位移动作拒绝。
- fault 或异常姿态下位移动作拒绝。
- MPU6050 缺失或读取失败时，固件姿态视为 `unknown`，按异常姿态处理。
- 未确认 Memory 不进入长期记忆。
- 未确认 Routine 不自动启用。

## LLM 边界

- LLM 只能生成文本、goal、参数化表情、白名单技能、兼容白名单动作和待确认记忆建议。
- LLM 不能直接控制舵机角度、PWM、servo id 或其他裸硬件字段。
- LLM 调用失败、超时或返回格式错误时，宿主机使用规则 fallback。
- fallback 不绕过 `PolicyGuard`。

## Dashboard 边界

- Dashboard 只提供状态、动作测试、急停、Soul、Memory、Routine 和诊断日志。
- Dashboard 不提供 Blockly、脚本编辑器、终端、任意代码执行或底层舵机控制。

## 固件边界

- 固件是最后一道安全线。
- 固件反射层拥有最高控制权，Host/LLM intent 不能覆盖急停、低电、跌倒、IMU fault 或硬件保护。
- 即使宿主机失联或协议异常，固件也要保持本地降级、动作限幅和安全停止。
- Host 心跳超时后，固件必须停止当前 Host skill，进入本地自治或 fallback；反射安全仍保持最高优先级。
- 固件中 MPU6050 姿态为 `fallen` 或 `unknown` 时，禁止运动动作，只允许表情、提示音、休眠、急停等低风险动作。
- 固件执行层必须与 Host 白名单动作对齐；未实现或执行失败的动作必须返回 `error`，不能回假 `ack`。
- 固件不保存长期记忆，不调用 LLM，不实现图形化编程。
- 未实现力反馈、电流检测、触觉或边缘检测前，不启用默认夹持/抓住桌边反射。

