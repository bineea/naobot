# 验收标准

## 自动化验收

- `pytest` 全部通过。
- `ruff check .` 通过。
- 协议校验覆盖合法事件、非法消息、intent、ack、status、error。
- 安全回归覆盖未知动作、裸舵机字段、低电运动、过期 intent、未确认记忆。
- WebSocket 覆盖 `touch_head -> intent -> ack` 闭环。

## 手动验收

- 运行 `naobot serve` 后 Dashboard 可访问。
- 运行 `naobot simulate --event touch_head` 后 Dashboard 显示机器人事件、Agent intent 和 ack。
- 修改 Soul 的名字或称呼后，下一次规则 fallback 回应随配置变化。
- Dashboard 急停生成 `stop` intent。
- 未配置 LLM 时系统仍可安全运行。
- `firmware/esp32/README.md` 中的上传说明可作为 ESP32 bring-up 起点。

## 文档验收

- README 指向 PRD、架构、安全策略、agent 文档。
- `AGENTS.md` 说明 agent 必读顺序、目录职责、常用命令和安全红线。
- PRD、架构、安全策略中的不做项保持一致。

