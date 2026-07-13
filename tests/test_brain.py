import asyncio
from types import SimpleNamespace

import pytest

from naobot.brain import PRIMARY_SYSTEM_PROMPT, AgentScopeBrainRuntime
from naobot.llm import RuleBasedLLMClient
from naobot.models import Envelope, MessageType, SoulConfig
from naobot.settings import Settings


class FakeStreamingAgent:
    def __init__(self, output: str, delay: float = 0.0, prefix_events=None) -> None:
        self.output = output
        self.delay = delay
        self.prefix_events = prefix_events or []
        self.inputs = []

    async def reply_stream(self, inputs):
        self.inputs.append(inputs)
        if self.delay:
            await asyncio.sleep(self.delay)
        for event in self.prefix_events:
            yield event
        yield SimpleNamespace(type="TEXT_BLOCK_DELTA", delta=self.output)


@pytest.mark.asyncio
async def test_agentscope_brain_parses_streamed_semantic_decision(tmp_path) -> None:
    output = """{
        "text": "我在呢。",
        "goal": "回应摸头",
        "expression": {"emotion": "happy", "valence": 0.7},
        "skills": [{"name": "wave", "args": {"level": 1}}],
        "memory_suggestion": {"type": "none"}
    }"""
    agents = []

    def factory(system_prompt: str):
        agent = FakeStreamingAgent(output)
        agents.append((system_prompt, agent))
        return agent

    runtime = AgentScopeBrainRuntime(
        Settings(runtime_dir=tmp_path, llm_base_url="http://example.test/v1", llm_model="test"),
        agent_factory=factory,
    )
    event = Envelope(type=MessageType.EVENT, payload={"name": "touch_head"})

    decision = await runtime.decide(event, SoulConfig(), ["用户喜欢安静"])

    assert decision.goal == "回应摸头"
    assert decision.expression is not None
    assert decision.expression.emotion == "happy"
    assert decision.skills[0].name == "wave"
    assert decision.actions == []
    assert len(agents) == 1
    assert runtime.status()["mode"] == "agentscope"
    assert runtime.status()["team_used"] is False


@pytest.mark.asyncio
async def test_agentscope_brain_falls_back_on_invalid_output(tmp_path) -> None:
    runtime = AgentScopeBrainRuntime(
        Settings(runtime_dir=tmp_path, llm_base_url="http://example.test/v1", llm_model="test"),
        agent_factory=lambda _: FakeStreamingAgent("not json"),
        fallback=RuleBasedLLMClient(),
    )
    event = Envelope(type=MessageType.EVENT, payload={"name": "touch_head"})

    decision = await runtime.decide(event, SoulConfig(), [])

    assert decision.goal == "回应用户摸头并友好打招呼"
    assert runtime.status()["mode"] == "fallback"
    assert runtime.status()["last_error"]


@pytest.mark.asyncio
async def test_agentscope_brain_rejects_actions_and_falls_back(tmp_path) -> None:
    runtime = AgentScopeBrainRuntime(
        Settings(runtime_dir=tmp_path, llm_base_url="http://example.test/v1", llm_model="test"),
        agent_factory=lambda _: FakeStreamingAgent(
            '{"goal":"越权动作","actions":[{"name":"wave","args":{}}]}'
        ),
    )
    event = Envelope(type=MessageType.EVENT, payload={"name": "touch_head"})

    decision = await runtime.decide(event, SoulConfig(), [])

    assert decision.goal == "回应用户摸头并友好打招呼"
    assert decision.actions == []
    assert runtime.status()["mode"] == "fallback"
    assert "not allowed to author actions" in runtime.status()["last_error"]


@pytest.mark.asyncio
async def test_agentscope_brain_ignores_thinking_stream_content(tmp_path) -> None:
    runtime = AgentScopeBrainRuntime(
        Settings(runtime_dir=tmp_path, llm_base_url="http://example.test/v1", llm_model="test"),
        agent_factory=lambda _: FakeStreamingAgent(
            '{"text":"完成","goal":"安全回应","skills":[]}',
            prefix_events=[
                SimpleNamespace(type="THINKING_BLOCK_DELTA", delta='{"private":"reasoning"}')
            ],
        ),
    )
    event = Envelope(type=MessageType.EVENT, payload={"name": "touch_head"})

    decision = await runtime.decide(event, SoulConfig(), [])

    assert decision.goal == "安全回应"
    assert runtime.status()["mode"] == "agentscope"


@pytest.mark.asyncio
async def test_agentscope_brain_times_out_to_rule_fallback(tmp_path) -> None:
    runtime = AgentScopeBrainRuntime(
        Settings(
            runtime_dir=tmp_path,
            llm_base_url="http://example.test/v1",
            llm_model="test",
            brain_timeout_seconds=0.01,
        ),
        agent_factory=lambda _: FakeStreamingAgent("{}", delay=0.1),
        fallback=RuleBasedLLMClient(),
    )
    event = Envelope(type=MessageType.EVENT, payload={"name": "touch_head"})

    decision = await runtime.decide(event, SoulConfig(), [])

    assert decision.goal == "回应用户摸头并友好打招呼"
    assert runtime.status()["mode"] == "fallback"
    assert runtime.status()["last_error"] == "brain_timeout"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_name",
    ["fall_detected", "battery_low", "emergency_stop", "imu_fault"],
)
async def test_safety_events_never_start_dynamic_team(tmp_path, event_name) -> None:
    calls = []

    def factory(system_prompt: str):
        calls.append(system_prompt)
        return FakeStreamingAgent(
            '{"text":"安全确认","goal":"解释反射","skills":[],"memory_suggestion":{"type":"none"}}'
        )

    runtime = AgentScopeBrainRuntime(
        Settings(runtime_dir=tmp_path, llm_base_url="http://example.test/v1", llm_model="test"),
        agent_factory=factory,
    )
    event = Envelope(
        type=MessageType.EVENT,
        payload={"name": event_name, "requires_team": True, "complexity": 10},
    )

    await runtime.decide(event, SoulConfig(), [])

    assert len(calls) == 1
    assert runtime.status()["team_used"] is False


@pytest.mark.asyncio
async def test_complex_user_request_uses_specialists_and_product_owner(tmp_path) -> None:
    prompts = []
    agents = []

    def factory(system_prompt: str):
        prompts.append(system_prompt)
        if "产品负责人" in system_prompt:
            agent = FakeStreamingAgent(
                '{"text":"好的","goal":"谨慎回应复杂请求","expression":{"emotion":"curious"},'
                '"skills":[{"name":"wave","args":{}}],"memory_suggestion":{"type":"none"}}'
            )
        else:
            agent = FakeStreamingAgent('{"recommendation":"保持低风险并简短回应"}')
        agents.append(agent)
        return agent

    runtime = AgentScopeBrainRuntime(
        Settings(runtime_dir=tmp_path, llm_base_url="http://example.test/v1", llm_model="test"),
        agent_factory=factory,
    )
    event = Envelope(
        type=MessageType.EVENT,
        payload={"name": "user_request", "text": "请综合考虑后做出回应", "requires_team": True},
    )

    decision = await runtime.decide(event, SoulConfig(), [])

    assert decision.goal == "谨慎回应复杂请求"
    assert len(prompts) == 4
    assert runtime.status()["team_used"] is True
    editor_prompt = agents[-1].inputs[0].content[0].text
    assert "specialist_recommendations" in editor_prompt
    assert "保持低风险并简短回应" in editor_prompt


@pytest.mark.asyncio
async def test_real_agentscope_agent_has_bounded_react_and_no_tools(tmp_path) -> None:
    runtime = AgentScopeBrainRuntime(
        Settings(
            runtime_dir=tmp_path,
            llm_base_url="http://example.test/v1",
            llm_model="test",
            brain_max_iters=99,
            brain_timeout_seconds=99,
        )
    )

    agent = runtime._create_agent(PRIMARY_SYSTEM_PROMPT)

    assert agent.__class__.__name__ == "Agent"
    assert agent.react_config.max_iters == 4
    assert await agent.toolkit.get_tool_schemas() == []
    assert runtime.status()["timeout_seconds"] == 4.0
