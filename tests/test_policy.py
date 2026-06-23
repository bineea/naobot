from naobot.models import Action, Envelope, MessageType, RobotState, now_ms
from naobot.policy import PolicyGuard


def test_policy_allows_safe_action() -> None:
    result = PolicyGuard().validate_actions([Action(name="wave", args={"level": 1})], RobotState())
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


def test_policy_rejects_expired_intent() -> None:
    envelope = Envelope(
        type=MessageType.INTENT,
        ts_ms=now_ms() - 10_000,
        deadline_ms=100,
        payload={"actions": [{"name": "blink", "args": {}}]},
    )
    result = PolicyGuard().validate_intent(envelope, RobotState())
    assert not result.accepted
    assert "过期" in result.reason
