import pytest
from agentscope.message import Base64Source, DataBlock

from naobot.agent import NaobotAgent
from naobot.brain import AgentScopeBrainRuntime
from naobot.llm import RuleBasedLLMClient
from naobot.models import Action, Envelope, ExpressionIntent, LLMDecision, MessageType, SkillIntent
from naobot.settings import Settings


class CountingLLM(RuleBasedLLMClient):
    def __init__(self) -> None:
        self.calls = 0

    async def decide(self, event, soul, memories):
        self.calls += 1
        return await super().decide(event, soul, memories)


class UntrustedActionsLLM(RuleBasedLLMClient):
    async def decide(self, event, soul, memories):
        return LLMDecision(
            goal="安全挥手",
            expression=ExpressionIntent(emotion="happy"),
            skills=[SkillIntent(name="wave", args={"level": 1})],
            actions=[Action(name="flip", args={"servo_id": 1, "angle": 180})],
        )


class MediaAwareLLM(RuleBasedLLMClient):
    def __init__(self) -> None:
        self.last_media_blocks = None

    async def decide(self, event, soul, memories, media_blocks=None):
        self.last_media_blocks = media_blocks
        return await super().decide(event, soul, memories)


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
    assert response.payload["actions"][0]["name"] == "set_expression"


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


def test_heartbeat_updates_control_state_with_host_receive_time(tmp_path, monkeypatch) -> None:
    host_receive_ms = 1_753_000_000_000
    monkeypatch.setattr("naobot.agent.now_ms", lambda: host_receive_ms)
    agent = NaobotAgent(Settings(runtime_dir=tmp_path, robot_heartbeat_timeout_ms=7000), llm=RuleBasedLLMClient())
    heartbeat = Envelope(
        type=MessageType.HEARTBEAT,
        seq=9,
        ts_ms=1_000,
        payload={
            "source": "firmware",
            "uptime_ms": 900,
            "battery_pct": 70,
            "posture": "upright",
            "control_authority": "reflex",
            "reflex_state": "fall_detected",
            "motion_state": "cancelled",
            "last_reflex": "brace_and_sit",
            "camera_fps": 10,
            "audio_state": "listening",
            "media_queue": 3,
            "media_dropped": 4,
            "psram_free": 7_654_321,
            "local_loop_interval_ms": 51,
            "local_loop_overrun_ms": 3,
        },
    )

    agent.update_state_from_envelope(heartbeat)

    agent.refresh_link_state(current_ms=host_receive_ms + 1_000)

    assert agent.state.link_state == "connected"
    assert agent.state.agent_connected is True
    assert agent.state.last_robot_seen_ms == host_receive_ms
    assert agent.state.last_heartbeat_ms == host_receive_ms
    assert agent.state.heartbeat_age_ms == 1_000
    assert agent.state.heartbeat_seq == 9
    assert agent.state.remote_heartbeat_ts_ms == 1_000
    assert agent.state.remote_uptime_ms == 900
    assert agent.state.control_authority == "reflex"
    assert agent.state.reflex_state == "fall_detected"
    assert agent.state.motion_state == "cancelled"
    assert agent.state.last_reflex == "brace_and_sit"
    assert agent.state.camera_fps == 10
    assert agent.state.audio_state == "listening"
    assert agent.state.media_queue == 3
    assert agent.state.media_dropped == 4
    assert agent.state.psram_free == 7_654_321
    assert agent.state.local_loop_interval_ms == 51
    assert agent.state.local_loop_overrun_ms == 3
    assert agent.status()["robot"]["camera_fps"] == 10
    assert agent.status()["robot"]["audio_state"] == "listening"
    assert agent.status()["robot"]["media_queue"] == 3
    assert agent.status()["robot"]["media_dropped"] == 4
    assert agent.status()["robot"]["psram_free"] == 7_654_321
    assert agent.status()["robot"]["local_loop_interval_ms"] == 51
    assert agent.status()["robot"]["local_loop_overrun_ms"] == 3


@pytest.mark.parametrize(
    "message_type",
    [MessageType.EVENT, MessageType.STATUS, MessageType.ACK, MessageType.ERROR],
)
def test_robot_messages_refresh_last_seen_using_host_time(tmp_path, monkeypatch, message_type) -> None:
    host_receive_ms = 1_753_000_000_000
    monkeypatch.setattr("naobot.agent.now_ms", lambda: host_receive_ms)
    agent = NaobotAgent(Settings(runtime_dir=tmp_path), llm=RuleBasedLLMClient())
    envelope = Envelope(type=message_type, ts_ms=1_000, payload={})

    agent.update_state_from_envelope(envelope)

    assert agent.state.last_robot_seen_ms == host_receive_ms
    assert agent.state.link_state == "connected"


def test_robot_link_becomes_stale_after_heartbeat_timeout(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("naobot.agent.now_ms", lambda: 1_000)
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


def test_host_heartbeat_sequence_increments_per_agent_instance(tmp_path) -> None:
    agent = NaobotAgent(Settings(runtime_dir=tmp_path), llm=RuleBasedLLMClient())

    first = agent.host_heartbeat()
    second = agent.host_heartbeat()
    recreated_agent = NaobotAgent(Settings(runtime_dir=tmp_path), llm=RuleBasedLLMClient())

    assert [first.seq, second.seq] == [1, 2]
    assert recreated_agent.host_heartbeat().seq == 1


def test_observe_robot_message_updates_state_without_running_brain(tmp_path) -> None:
    llm = CountingLLM()
    agent = NaobotAgent(Settings(runtime_dir=tmp_path), llm=llm)
    event = Envelope(
        type=MessageType.EVENT,
        payload={"name": "touch_head", "battery_pct": 81, "posture": "upright"},
    )

    agent.observe_robot_message(event)

    assert llm.calls == 0
    assert agent.state.last_event == "touch_head"
    assert agent.state.battery_pct == 81


def test_default_agent_uses_agentscope_runtime_with_observable_fallback(tmp_path) -> None:
    agent = NaobotAgent(Settings(runtime_dir=tmp_path))

    assert isinstance(agent.llm, AgentScopeBrainRuntime)
    assert agent.status()["brain"]["runtime"] == "agentscope-2.0.4"
    assert agent.status()["brain"]["mode"] == "fallback"


@pytest.mark.asyncio
async def test_behavior_layer_ignores_untrusted_llm_actions(tmp_path) -> None:
    agent = NaobotAgent(Settings(runtime_dir=tmp_path), llm=UntrustedActionsLLM())
    event = Envelope(
        type=MessageType.EVENT,
        payload={"name": "touch_head", "battery_pct": 80, "posture": "upright"},
    )

    response = await agent.handle_robot_message(event)

    assert response is not None
    assert response.type == MessageType.INTENT
    names = [action["name"] for action in response.payload["actions"]]
    assert names == ["set_expression", "wave"]
    assert "servo_id" not in str(response.payload)


@pytest.mark.asyncio
async def test_agent_create_intent_passes_media_blocks_to_brain(tmp_path) -> None:
    llm = MediaAwareLLM()
    agent = NaobotAgent(Settings(runtime_dir=tmp_path), llm=llm)
    event = Envelope(
        type=MessageType.EVENT,
        payload={"name": "user_utterance", "transcript": "看看我手里的东西"},
    )
    media_blocks = [
        DataBlock(
            name="frame-1.jpg",
            source=Base64Source(data="aGVsbG8=", media_type="image/jpeg"),
        )
    ]

    await agent.create_intent(event, media_blocks=media_blocks)

    assert llm.last_media_blocks == media_blocks
