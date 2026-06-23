from __future__ import annotations

from dataclasses import dataclass

from .actions import is_movement_action, validate_action
from .models import Action, Envelope, RobotMode, RobotState, now_ms


@dataclass(frozen=True)
class PolicyResult:
    accepted: bool
    reason: str = ""


class PolicyGuard:
    def __init__(self, low_battery_threshold: int = 15) -> None:
        self.low_battery_threshold = low_battery_threshold

    def validate_actions(
        self,
        actions: list[Action],
        state: RobotState,
        envelope: Envelope | None = None,
    ) -> PolicyResult:
        if envelope and envelope.is_expired(now_ms()):
            return PolicyResult(False, "intent 已过期")
        if state.mode in {RobotMode.FAULT, RobotMode.LOW_BATTERY}:
            for action in actions:
                if is_movement_action(action.name):
                    return PolicyResult(False, f"{state.mode} 状态拒绝运动动作")
        if state.battery_pct <= self.low_battery_threshold:
            for action in actions:
                if is_movement_action(action.name):
                    return PolicyResult(False, "低电量拒绝运动动作")
        if state.posture not in {"upright", "sitting"}:
            for action in actions:
                if is_movement_action(action.name):
                    return PolicyResult(False, "姿态异常拒绝运动动作")

        for action in actions:
            result = validate_action(action.name, action.args)
            if not result.accepted:
                return PolicyResult(False, result.reason)
        return PolicyResult(True)

    def validate_intent(self, envelope: Envelope, state: RobotState) -> PolicyResult:
        raw_actions = envelope.payload.get("actions", [])
        actions = [Action.model_validate(action) for action in raw_actions]
        return self.validate_actions(actions, state, envelope)
