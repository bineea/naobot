import pytest

from naobot.agent import NaobotAgent
from naobot.llm import RuleBasedLLMClient
from naobot.models import Envelope, MessageType
from naobot.settings import Settings


@pytest.mark.asyncio
async def test_touch_head_creates_safe_intent(tmp_path) -> None:
    agent = NaobotAgent(Settings(runtime_dir=tmp_path), llm=RuleBasedLLMClient())
    event = Envelope(
        type=MessageType.EVENT,
        seq=1,
        session_id="test",
        payload={"name": "touch_head", "battery_pct": 80, "posture": "upright"},
    )
    response = await agent.handle_robot_message(event)
    assert response is not None
    assert response.type == MessageType.INTENT
    assert response.payload["actions"][0]["name"] == "set_face"


@pytest.mark.asyncio
async def test_low_battery_event_does_not_emit_movement(tmp_path) -> None:
    agent = NaobotAgent(Settings(runtime_dir=tmp_path), llm=RuleBasedLLMClient())
    event = Envelope(
        type=MessageType.EVENT,
        seq=1,
        session_id="test",
        payload={"name": "battery_low", "battery_pct": 5, "posture": "upright"},
    )
    response = await agent.handle_robot_message(event)
    assert response is not None
    assert response.type == MessageType.INTENT
    names = [action["name"] for action in response.payload["actions"]]
    assert "wave" not in names


def test_heartbeat_updates_control_state(tmp_path) -> None:
    agent = NaobotAgent(Settings(runtime_dir=tmp_path, robot_heartbeat_timeout_ms=7000), llm=RuleBasedLLMClient())
    heartbeat = Envelope(
        type=MessageType.HEARTBEAT,
        seq=9,
        ts_ms=1_000,
        payload={
            "source": "firmware",
            "battery_pct": 70,
            "posture": "upright",
            "control_authority": "reflex",
            "reflex_state": "fall_detected",
            "motion_state": "cancelled",
            "last_reflex": "brace_and_sit",
        },
    )

    agent.update_state_from_envelope(heartbeat)

    agent.refresh_link_state(current_ms=2_000)

    assert agent.state.link_state == "connected"
    assert agent.state.agent_connected is True
    assert agent.state.last_robot_seen_ms == 1_000
    assert agent.state.last_heartbeat_ms == 1_000
    assert agent.state.heartbeat_age_ms == 1_000
    assert agent.state.heartbeat_seq == 9
    assert agent.state.control_authority == "reflex"
    assert agent.state.reflex_state == "fall_detected"
    assert agent.state.motion_state == "cancelled"
    assert agent.state.last_reflex == "brace_and_sit"


def test_robot_link_becomes_stale_after_heartbeat_timeout(tmp_path) -> None:
    agent = NaobotAgent(Settings(runtime_dir=tmp_path, robot_heartbeat_timeout_ms=7000), llm=RuleBasedLLMClient())
    heartbeat = Envelope(type=MessageType.HEARTBEAT, ts_ms=1_000, payload={"source": "firmware"})

    agent.update_state_from_envelope(heartbeat)
    agent.refresh_link_state(current_ms=9_001)

    assert agent.state.link_state == "stale"
    assert agent.state.agent_connected is False


def test_host_heartbeat_payload_contains_brain_state(tmp_path) -> None:
    agent = NaobotAgent(Settings(runtime_dir=tmp_path), llm=RuleBasedLLMClient())
    agent.last_intent = Envelope(type=MessageType.INTENT, id="int_test")

    heartbeat = agent.host_heartbeat()

    assert heartbeat.type == MessageType.HEARTBEAT
    assert heartbeat.payload["source"] == "host"
    assert heartbeat.payload["last_intent_id"] == "int_test"
    assert heartbeat.payload["agent_mode"] == agent.state.mode
