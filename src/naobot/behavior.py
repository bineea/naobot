from __future__ import annotations

from .models import (
    Action,
    Envelope,
    LLMDecision,
    MessageType,
    RobotState,
    SkillIntent,
    new_id,
    now_ms,
)
from .policy import SKILL_ACTION_MAP, PolicyGuard


class BehaviorRuntime:
    def __init__(self, policy: PolicyGuard | None = None) -> None:
        self.policy = policy or PolicyGuard()

    def compile(
        self,
        decision: LLMDecision,
        event: Envelope,
        state: RobotState,
    ) -> Envelope:
        skills = self._normalize_skills(decision.skills)
        actions = self._compile_actions(decision, skills)
        intent = Envelope(
            type=MessageType.INTENT,
            id=new_id("int"),
            seq=event.seq + 1,
            ts_ms=now_ms(),
            session_id=event.session_id,
            priority=4,
            deadline_ms=4000,
            payload={
                "goal": decision.goal,
                "text": decision.text,
                "expression": decision.expression.model_dump() if decision.expression else None,
                "skills": [skill.model_dump() for skill in skills],
                "actions": [action.model_dump() for action in actions],
            },
        )
        result = self.policy.validate_intent(intent, state)
        if result.accepted:
            return intent
        return Envelope(
            type=MessageType.ERROR,
            id=new_id("err"),
            seq=event.seq + 1,
            ts_ms=now_ms(),
            session_id=event.session_id,
            priority=8,
            payload={"code": "POLICY_REJECTED", "message": result.reason},
        )

    @staticmethod
    def _normalize_skills(skills: list[SkillIntent]) -> list[SkillIntent]:
        unique: list[SkillIntent] = []
        for skill in skills:
            if skill not in unique:
                unique.append(skill)
        return (
            [next(skill for skill in unique if skill.name == "stop")]
            if any(skill.name == "stop" for skill in unique)
            else unique
        )

    @staticmethod
    def _compile_actions(
        decision: LLMDecision,
        skills: list[SkillIntent],
    ) -> list[Action]:
        actions: list[Action] = []
        if decision.expression is not None:
            actions.append(Action(name="set_expression", args=decision.expression.model_dump()))
        for skill in skills:
            action_name = SKILL_ACTION_MAP.get(skill.name)
            if action_name is not None:
                action = Action(name=action_name, args=skill.args)
                if action not in actions:
                    actions.append(action)
        if any(action.name == "stop" for action in actions):
            return [next(action for action in actions if action.name == "stop")]
        return actions
