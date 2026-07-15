from naobot.behavior import BehaviorRuntime
from naobot.models import (
    Action,
    Envelope,
    ExpressionIntent,
    LLMDecision,
    MessageType,
    RobotState,
    SkillIntent,
)


def test_compile_preserves_semantics_and_source_metadata() -> None:
    runtime = BehaviorRuntime()
    event = Envelope(type=MessageType.EVENT, seq=7, session_id="session-test")
    expression = ExpressionIntent(emotion="happy", valence=0.7, eye_open=0.75)
    decision = LLMDecision(
        goal="回应用户",
        text="我在呢。",
        expression=expression,
        skills=[SkillIntent(name="wave", args={"level": 1})],
    )

    intent = runtime.compile(decision, event, RobotState(battery_pct=100))

    assert intent.type == MessageType.INTENT
    assert intent.id.startswith("int_")
    assert intent.seq == 8
    assert intent.session_id == "session-test"
    assert intent.priority == 4
    assert intent.deadline_ms == 4000
    assert intent.payload == {
        "goal": "回应用户",
        "text": "我在呢。",
        "expression": expression.model_dump(),
        "skills": [{"name": "wave", "args": {"level": 1}}],
        "actions": [
            {"name": "set_expression", "args": expression.model_dump()},
            {"name": "wave", "args": {"level": 1}},
        ],
    }


def test_compile_ignores_llm_actions_and_deduplicates_skills() -> None:
    decision = LLMDecision(
        skills=[
            SkillIntent(name="wave", args={"level": 1}),
            SkillIntent(name="wave", args={"level": 1}),
            SkillIntent(name="chirp", args={"tone": "happy"}),
        ],
        actions=[
            Action(name="stop"),
            Action(name="flip", args={"servo_id": 1}),
        ],
    )

    intent = BehaviorRuntime().compile(
        decision,
        Envelope(type=MessageType.EVENT),
        RobotState(battery_pct=100),
    )

    assert intent.type == MessageType.INTENT
    assert intent.payload["skills"] == [
        {"name": "wave", "args": {"level": 1}},
        {"name": "chirp", "args": {"tone": "happy"}},
    ]
    assert intent.payload["actions"] == [
        {"name": "wave", "args": {"level": 1}},
        {"name": "chirp", "args": {"tone": "happy"}},
    ]


def test_stop_is_exclusive_and_remains_available_in_low_battery_state() -> None:
    decision = LLMDecision(
        expression=ExpressionIntent(emotion="alert"),
        skills=[
            SkillIntent(name="wave", args={"level": 1}),
            SkillIntent(name="stop"),
            SkillIntent(name="chirp", args={"tone": "alert"}),
            SkillIntent(name="stop"),
        ],
    )

    intent = BehaviorRuntime().compile(
        decision,
        Envelope(type=MessageType.EVENT),
        RobotState(battery_pct=5),
    )

    assert intent.type == MessageType.INTENT
    assert intent.payload["skills"] == [{"name": "stop", "args": {}}]
    assert intent.payload["actions"] == [{"name": "stop", "args": {}}]


def test_policy_rejection_returns_error_envelope() -> None:
    event = Envelope(type=MessageType.EVENT, seq=3, session_id="session-test")
    decision = LLMDecision(skills=[SkillIntent(name="drop_hook")])

    result = BehaviorRuntime().compile(decision, event, RobotState())

    assert result.type == MessageType.ERROR
    assert result.seq == 4
    assert result.session_id == "session-test"
    assert result.priority == 8
    assert result.payload["code"] == "POLICY_REJECTED"
    assert "未知或未授权技能" in result.payload["message"]


def test_policy_rejects_dangerous_skill_arguments() -> None:
    decision = LLMDecision(
        skills=[SkillIntent(name="wave", args={"level": 1, "servo_id": 2, "angle": 150})]
    )

    result = BehaviorRuntime().compile(
        decision,
        Envelope(type=MessageType.EVENT),
        RobotState(),
    )

    assert result.type == MessageType.ERROR
    assert "裸硬件字段" in result.payload["message"]


def test_policy_rejects_invalid_expression() -> None:
    decision = LLMDecision(expression=ExpressionIntent(emotion="angry", eye_open=2.0))

    result = BehaviorRuntime().compile(
        decision,
        Envelope(type=MessageType.EVENT),
        RobotState(),
    )

    assert result.type == MessageType.ERROR
    assert "不支持的表情情绪" in result.payload["message"]


def test_policy_rejects_movement_skill_during_low_battery() -> None:
    decision = LLMDecision(skills=[SkillIntent(name="small_step_forward", args={"steps": 1})])

    result = BehaviorRuntime().compile(
        decision,
        Envelope(type=MessageType.EVENT),
        RobotState(battery_pct=5),
    )

    assert result.type == MessageType.ERROR
    assert "低电量拒绝运动动作" in result.payload["message"]


def test_policy_rejects_movement_skill_when_battery_soc_is_unknown() -> None:
    decision = LLMDecision(skills=[SkillIntent(name="wave", args={"level": 1})])

    result = BehaviorRuntime().compile(
        decision,
        Envelope(type=MessageType.EVENT),
        RobotState(battery_pct=None),
    )

    assert result.type == MessageType.ERROR
    assert "电量未知" in result.payload["message"]
