from naobot.models import Action, Envelope, MessageType, RobotState, now_ms
from naobot.policy import PolicyGuard


def test_policy_allows_safe_action() -> None:
    result = PolicyGuard().validate_actions(
        [Action(name="wave", args={"level": 1})], RobotState(battery_pct=80)
    )
    assert result.accepted


def test_policy_rejects_unknown_action() -> None:
    result = PolicyGuard().validate_actions([Action(name="flip")], RobotState())
    assert not result.accepted
    assert "未知" in result.reason


def test_policy_rejects_low_battery_movement() -> None:
    state = RobotState(battery_pct=5)
    result = PolicyGuard().validate_actions([Action(name="wave", args={"level": 1})], state)
    assert not result.accepted
    assert "低电量" in result.reason


def test_policy_rejects_movement_when_battery_soc_is_unknown() -> None:
    state = RobotState(battery_pct=None)

    result = PolicyGuard().validate_actions([Action(name="wave", args={"level": 1})], state)

    assert not result.accepted
    assert "电量未知" in result.reason


def test_policy_allows_non_movement_when_battery_soc_is_unknown() -> None:
    result = PolicyGuard().validate_actions(
        [Action(name="chirp", args={"tone": "soft"})], RobotState(battery_pct=None)
    )

    assert result.accepted


def test_policy_rejects_expired_intent() -> None:
    envelope = Envelope(
        type=MessageType.INTENT,
        ts_ms=now_ms() - 10_000,
        deadline_ms=100,
        payload={"actions": [{"name": "blink", "args": {}}]},
    )
    result = PolicyGuard().validate_intent(envelope, RobotState(battery_pct=80))
    assert not result.accepted
    assert "过期" in result.reason


def test_policy_allows_semantic_intent() -> None:
    envelope = Envelope(
        type=MessageType.INTENT,
        payload={
            "goal": "开心地打招呼",
            "expression": {
                "emotion": "happy",
                "valence": 0.8,
                "arousal": 0.4,
                "eye_open": 0.75,
                "pupil_offset_x": 0.0,
                "blink_rate": 0.2,
                "duration_ms": 1200,
            },
            "skills": [{"name": "wave", "args": {"level": 1}}],
            "actions": [{"name": "set_face", "args": {"face": "happy"}}],
        },
    )

    result = PolicyGuard().validate_intent(envelope, RobotState(battery_pct=80))

    assert result.accepted


def test_policy_rejects_expression_out_of_range() -> None:
    envelope = Envelope(
        type=MessageType.INTENT,
        payload={"expression": {"emotion": "happy", "valence": 2.0}},
    )

    result = PolicyGuard().validate_intent(envelope, RobotState())

    assert not result.accepted
    assert "表情参数越界" in result.reason


def test_policy_rejects_unknown_skill() -> None:
    envelope = Envelope(type=MessageType.INTENT, payload={"skills": [{"name": "drop_hook", "args": {}}]})

    result = PolicyGuard().validate_intent(envelope, RobotState())

    assert not result.accepted
    assert "未知或未授权技能" in result.reason


def test_policy_rejects_bare_hardware_fields() -> None:
    envelope = Envelope(type=MessageType.INTENT, payload={"skills": [{"name": "wave", "args": {"servo_id": 1}}]})

    result = PolicyGuard().validate_intent(envelope, RobotState())

    assert not result.accepted
    assert "裸硬件字段" in result.reason
