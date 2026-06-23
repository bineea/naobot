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
