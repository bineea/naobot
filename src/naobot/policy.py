from __future__ import annotations

from dataclasses import dataclass

from .actions import find_dangerous_field, is_movement_action, validate_action
from .models import Action, Envelope, ExpressionIntent, RobotMode, RobotState, SkillIntent, now_ms

ALLOWED_EMOTIONS = {"idle", "happy", "sad", "dizzy", "sleepy", "alert", "curious", "confused", "proud", "shy"}
ALLOWED_SKILLS = {
    "wave",
    "small_step_forward",
    "turn_left",
    "turn_right",
    "gentle_nudge",
    "sit",
    "chirp",
    "sleep",
    "stop",
}
SKILL_ACTION_MAP = {
    "wave": "wave",
    "small_step_forward": "small_step_forward",
    "turn_left": "turn_left",
    "turn_right": "turn_right",
    "gentle_nudge": "gentle_nudge",
    "sit": "sit",
    "chirp": "chirp",
    "sleep": "sleep",
    "stop": "stop",
}


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
        if state.battery_pct is None:
            for action in actions:
                if is_movement_action(action.name):
                    return PolicyResult(False, "电量未知，拒绝运动动作")
        elif state.battery_pct <= self.low_battery_threshold:
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

    def validate_expression(self, expression: ExpressionIntent | None) -> PolicyResult:
        if expression is None:
            return PolicyResult(True)
        if expression.emotion not in ALLOWED_EMOTIONS:
            return PolicyResult(False, f"不支持的表情情绪: {expression.emotion}")
        numeric_ranges = {
            "valence": (-1.0, 1.0),
            "arousal": (0.0, 1.0),
            "eye_open": (0.0, 1.0),
            "pupil_offset_x": (-1.0, 1.0),
            "blink_rate": (0.0, 1.0),
        }
        data = expression.model_dump()
        for field, (minimum, maximum) in numeric_ranges.items():
            value = float(data[field])
            if value < minimum or value > maximum:
                return PolicyResult(False, f"表情参数越界: {field}")
        if expression.duration_ms < 0 or expression.duration_ms > 5000:
            return PolicyResult(False, "表情持续时间越界")
        unsafe_field = find_dangerous_field(data)
        if unsafe_field:
            return PolicyResult(False, f"表情包含裸硬件字段: {unsafe_field}")
        return PolicyResult(True)

    def validate_skills(self, skills: list[SkillIntent], state: RobotState) -> PolicyResult:
        actions: list[Action] = []
        for skill in skills:
            if skill.name not in ALLOWED_SKILLS:
                return PolicyResult(False, f"未知或未授权技能: {skill.name}")
            unsafe_field = find_dangerous_field(skill.args)
            if unsafe_field:
                return PolicyResult(False, f"技能包含裸硬件字段: {unsafe_field}")
            actions.append(Action(name=SKILL_ACTION_MAP[skill.name], args=skill.args))
        return self.validate_actions(actions, state)

    def validate_intent(self, envelope: Envelope, state: RobotState) -> PolicyResult:
        unsafe_field = find_dangerous_field(envelope.payload)
        if unsafe_field:
            return PolicyResult(False, f"intent 包含裸硬件字段: {unsafe_field}")
        expression_result = self.validate_expression(
            ExpressionIntent.model_validate(envelope.payload["expression"]) if envelope.payload.get("expression") else None
        )
        if not expression_result.accepted:
            return expression_result
        raw_skills = envelope.payload.get("skills", [])
        skills = [SkillIntent.model_validate(skill) for skill in raw_skills]
        skill_result = self.validate_skills(skills, state)
        if not skill_result.accepted:
            return skill_result
        raw_actions = envelope.payload.get("actions", [])
        actions = [Action.model_validate(action) for action in raw_actions]
        return self.validate_actions(actions, state, envelope)
