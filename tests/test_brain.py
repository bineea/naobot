import asyncio
import json
from types import SimpleNamespace

import pytest
from agentscope.state import AgentState

from naobot.brain import PRIMARY_SYSTEM_PROMPT, AgentScopeBrainRuntime
from naobot.llm import RuleBasedLLMClient
from naobot.models import Envelope, MessageType, SoulConfig
from naobot.runtime.registry import RuntimeRegistry
from naobot.settings import Settings


class FakeStreamingAgent:
    def __init__(
        self,
        output: str,
        delay: float = 0.0,
        prefix_events=None,
        state: AgentState | None = None,
    ) -> None:
        self.output = output
        self.delay = delay
        self.prefix_events = prefix_events or []
        self.inputs = []
        self.state = state

    async def reply_stream(self, inputs):
        self.inputs.append(inputs)
        if self.delay:
            await asyncio.sleep(self.delay)
        for event in self.prefix_events:
            yield event
        yield SimpleNamespace(type="TEXT_BLOCK_DELTA", delta=self.output)


class CoordinatedStateAgent:
    def __init__(
        self,
        *,
        output: str,
        state: AgentState | None,
        started: asyncio.Event,
        release: asyncio.Event | None = None,
    ) -> None:
        self.output = output
        self.state = state or AgentState()
        self.started = started
        self.release = release

    async def reply_stream(self, inputs):
        payload = json.loads(inputs.content[0].text)
        transcript = payload["brain_input"]["transcript"]
        history = list(self.state.middle_context.get("history", []))
        history.append(transcript)
        self.state.middle_context["history"] = history
        self.started.set()
        if self.release is not None:
            await self.release.wait()
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
    assert decision.confidence == pytest.approx(1.0)
    assert decision.needs_team is False
    assert decision.escalation_reason is None
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
            brain_single_timeout_seconds=0.01,
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
async def test_team_route_uses_dedicated_timeout_setting(tmp_path) -> None:
    def factory(system_prompt: str, **kwargs):
        if "产品负责人" in system_prompt:
            return FakeStreamingAgent('{"text":"好的","goal":"团队完成","skills":[]}', delay=0.1)
        return FakeStreamingAgent('{"recommendation":"继续"}', delay=0.1)

    runtime = AgentScopeBrainRuntime(
        Settings(
            runtime_dir=tmp_path,
            llm_base_url="http://example.test/v1",
            llm_model="test",
            brain_team_timeout_seconds=0.01,
        ),
        agent_factory=factory,
        fallback=RuleBasedLLMClient(),
    )
    event = Envelope(
        type=MessageType.EVENT,
        payload={
            "name": "user_request",
            "text": "请记住我是阿明，再结合刚才和现在的视觉画面判断左边的红包是不是我的，不确定就先问我。",
            "vision_summary": "刚才左边的人拿着红色背包，现在镜头里只剩红色背包。",
            "person_id": "person-timeout",
        },
    )

    decision = await runtime.decide(event, SoulConfig(), [])

    assert decision.goal == "轻量确认收到事件"
    assert runtime.status()["mode"] == "fallback"
    assert runtime.status()["last_error"] == "brain_timeout"


@pytest.mark.asyncio
async def test_default_route_ignores_requires_team_and_complexity_without_debug_override(tmp_path) -> None:
    calls = []

    def factory(system_prompt: str, **kwargs):
        calls.append(system_prompt)
        return FakeStreamingAgent('{"text":"收到","goal":"单路由处理","skills":[]}')

    runtime = AgentScopeBrainRuntime(
        Settings(runtime_dir=tmp_path, llm_base_url="http://example.test/v1", llm_model="test"),
        agent_factory=factory,
    )

    decision = await runtime.decide(
        Envelope(
            type=MessageType.EVENT,
            payload={
                "name": "user_request",
                "text": "帮我看下天气",
                "requires_team": True,
                "complexity": 99,
                "person_id": "person-default-route",
            },
        ),
        SoulConfig(),
        [],
    )

    assert decision.goal == "单路由处理"
    assert len(calls) == 1
    assert runtime.status()["last_route"]["mode"] == "single"
    assert "debug_requires_team" not in runtime.status()["last_route"]["reasons"]
    assert "debug_complexity" not in runtime.status()["last_route"]["reasons"]


@pytest.mark.asyncio
async def test_debug_override_can_force_team_route_for_compatibility(tmp_path) -> None:
    calls = []

    def factory(system_prompt: str, **kwargs):
        calls.append(system_prompt)
        if "产品负责人" in system_prompt:
            return FakeStreamingAgent('{"text":"收到","goal":"团队处理","skills":[]}')
        return FakeStreamingAgent('{"recommendation":"兼容调试强制团队"}')

    runtime = AgentScopeBrainRuntime(
        Settings(
            runtime_dir=tmp_path,
            llm_base_url="http://example.test/v1",
            llm_model="test",
            brain_debug_force_team_override=True,
        ),
        agent_factory=factory,
    )

    decision = await runtime.decide(
        Envelope(
            type=MessageType.EVENT,
            payload={
                "name": "user_request",
                "text": "帮我看下天气",
                "requires_team": True,
                "complexity": 99,
                "person_id": "person-debug-route",
            },
        ),
        SoulConfig(),
        [],
    )

    assert decision.goal == "团队处理"
    assert len(calls) == 4
    assert runtime.status()["last_route"]["mode"] == "team"
    assert "debug_requires_team" in runtime.status()["last_route"]["reasons"]
    assert "debug_complexity" in runtime.status()["last_route"]["reasons"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_name",
    ["fall_detected", "battery_low", "emergency_stop", "imu_fault"],
)
async def test_safety_events_never_start_dynamic_team(tmp_path, event_name) -> None:
    calls = []

    def factory(system_prompt: str, **kwargs):
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

    assert len(calls) == 0
    assert runtime.status()["team_used"] is False
    assert runtime.status()["last_route"]["mode"] == "deterministic"


@pytest.mark.asyncio
async def test_complex_user_request_uses_specialists_and_product_owner(tmp_path) -> None:
    prompts = []
    agents = []

    def factory(system_prompt: str, **kwargs):
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
        payload={
            "name": "user_request",
            "text": (
                "请先记住我叫林舟，再结合刚才和现在画面里左边的人是不是我，"
                "如果你不确定就先问我，但也给出一个谨慎建议。"
            ),
            "vision_summary": "刚才左边是戴帽子的人，现在左边换成了拿红包的人。",
            "person_id": "person-complex",
        },
    )

    decision = await runtime.decide(event, SoulConfig(), [])

    assert decision.goal == "谨慎回应复杂请求"
    assert len(prompts) == 4
    assert runtime.status()["team_used"] is True
    assert runtime.status()["last_route"]["mode"] == "team"
    assert runtime.status()["last_route"]["score"] >= 4
    assert runtime.status()["last_route"]["self_escalated"] is False
    editor_prompt = agents[-1].inputs[0].content[0].text
    assert "specialist_recommendations" in editor_prompt
    assert "保持低风险并简短回应" in editor_prompt


@pytest.mark.asyncio
async def test_low_confidence_single_agent_self_escalates_to_team(tmp_path) -> None:
    prompts = []

    def factory(system_prompt: str, **kwargs):
        prompts.append(system_prompt)
        if system_prompt == PRIMARY_SYSTEM_PROMPT:
            return FakeStreamingAgent(
                '{"text":"我想确认一下","goal":"先给初步回应","confidence":0.42,"skills":[]}'
            )
        if "产品负责人" in system_prompt:
            return FakeStreamingAgent(
                '{"text":"我确认后再回答你","goal":"团队复核后回应","skills":[]}'
            )
        return FakeStreamingAgent('{"recommendation":"置信度不足，需要团队复核"}')

    runtime = AgentScopeBrainRuntime(
        Settings(runtime_dir=tmp_path, llm_base_url="http://example.test/v1", llm_model="test"),
        agent_factory=factory,
    )

    decision = await runtime.decide(
        Envelope(type=MessageType.EVENT, payload={"name": "touch_head", "person_id": "person-low-conf"}),
        SoulConfig(),
        [],
    )

    assert decision.goal == "团队复核后回应"
    assert len(prompts) == 5
    assert runtime.status()["team_used"] is True
    assert runtime.status()["last_route"]["mode"] == "team"
    assert runtime.status()["last_route"]["self_escalated"] is True


@pytest.mark.asyncio
async def test_create_agent_receives_state_and_context_config_and_persists_runtime(tmp_path) -> None:
    captures = []
    registry = RuntimeRegistry(
        Settings(runtime_dir=tmp_path, llm_base_url="http://example.test/v1", llm_model="test")
    )

    def factory(system_prompt: str, **kwargs):
        captures.append(kwargs)
        return FakeStreamingAgent(
            '{"text":"你好","goal":"完成问候","skills":[]}',
            state=kwargs.get("state"),
        )

    runtime = AgentScopeBrainRuntime(
        Settings(runtime_dir=tmp_path, llm_base_url="http://example.test/v1", llm_model="test"),
        agent_factory=factory,
        runtime_registry=registry,
    )

    await runtime.decide(
        Envelope(type=MessageType.EVENT, payload={"name": "touch_head", "person_id": "person-42"}),
        SoulConfig(),
        [],
    )

    assert captures
    assert captures[0]["context_config"].trigger_ratio == pytest.approx(0.8)
    assert captures[0]["context_config"].reserve_ratio == pytest.approx(0.1)
    assert captures[0]["state"].session_id

    saved_state = await registry.load_state("person-42", "primary")
    assert saved_state.session_id == captures[0]["state"].session_id


@pytest.mark.asyncio
async def test_same_person_decisions_share_lifecycle_lock_and_preserve_history(tmp_path) -> None:
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()

    def factory(system_prompt: str, **kwargs):
        person_id = kwargs["person_id"]
        state = kwargs.get("state")
        if person_id == "person-serial" and not first_started.is_set():
            return CoordinatedStateAgent(
                output='{"text":"first","goal":"第一条","skills":[]}',
                state=state,
                started=first_started,
                release=release_first,
            )
        return CoordinatedStateAgent(
            output='{"text":"second","goal":"第二条","skills":[]}',
            state=state,
            started=second_started,
        )

    registry = RuntimeRegistry(
        Settings(runtime_dir=tmp_path, llm_base_url="http://example.test/v1", llm_model="test")
    )
    runtime = AgentScopeBrainRuntime(
        Settings(runtime_dir=tmp_path, llm_base_url="http://example.test/v1", llm_model="test"),
        agent_factory=factory,
        runtime_registry=registry,
    )

    first_task = asyncio.create_task(
        runtime.decide(
            Envelope(
                type=MessageType.EVENT,
                payload={"name": "user_request", "text": "A", "person_id": "person-serial"},
            ),
            SoulConfig(),
            [],
        )
    )
    await asyncio.wait_for(first_started.wait(), timeout=0.2)

    second_task = asyncio.create_task(
        runtime.decide(
            Envelope(
                type=MessageType.EVENT,
                payload={"name": "user_request", "text": "B", "person_id": "person-serial"},
            ),
            SoulConfig(),
            [],
        )
    )

    await asyncio.sleep(0.05)
    assert second_started.is_set() is False

    release_first.set()
    await asyncio.gather(first_task, second_task)

    saved_state = await registry.load_state("person-serial", "primary")
    assert saved_state.middle_context["history"] == ["A", "B"]


@pytest.mark.asyncio
async def test_different_people_can_run_in_parallel(tmp_path) -> None:
    started: dict[str, asyncio.Event] = {
        "person-a": asyncio.Event(),
        "person-b": asyncio.Event(),
    }
    release = asyncio.Event()

    def factory(system_prompt: str, **kwargs):
        return CoordinatedStateAgent(
            output='{"text":"ok","goal":"并行完成","skills":[]}',
            state=kwargs.get("state"),
            started=started[kwargs["person_id"]],
            release=release,
        )

    runtime = AgentScopeBrainRuntime(
        Settings(runtime_dir=tmp_path, llm_base_url="http://example.test/v1", llm_model="test"),
        agent_factory=factory,
    )

    task_a = asyncio.create_task(
        runtime.decide(
            Envelope(
                type=MessageType.EVENT,
                payload={"name": "user_request", "text": "A", "person_id": "person-a"},
            ),
            SoulConfig(),
            [],
        )
    )
    task_b = asyncio.create_task(
        runtime.decide(
            Envelope(
                type=MessageType.EVENT,
                payload={"name": "user_request", "text": "B", "person_id": "person-b"},
            ),
            SoulConfig(),
            [],
        )
    )

    await asyncio.wait_for(started["person-a"].wait(), timeout=0.2)
    await asyncio.wait_for(started["person-b"].wait(), timeout=0.2)
    release.set()
    await asyncio.gather(task_a, task_b)


@pytest.mark.asyncio
async def test_real_agentscope_agent_has_runtime_state_context_and_new_timeouts(tmp_path) -> None:
    runtime = AgentScopeBrainRuntime(
        Settings(
            runtime_dir=tmp_path,
            llm_base_url="http://example.test/v1",
            llm_model="test",
            brain_max_iters=99,
            brain_single_timeout_seconds=99,
            brain_team_timeout_seconds=99,
        )
    )

    agent = await runtime._create_agent(
        PRIMARY_SYSTEM_PROMPT,
        "primary",
        "person-real",
        is_guest=False,
    )

    assert agent.__class__.__name__ == "Agent"
    assert agent.react_config.max_iters == 4
    assert agent.context_config.trigger_ratio == pytest.approx(0.8)
    assert agent.context_config.reserve_ratio == pytest.approx(0.1)
    assert agent.state.session_id
    assert await agent.toolkit.get_tool_schemas() == []
    assert runtime.status()["single_timeout_seconds"] == 99
    assert runtime.status()["team_timeout_seconds"] == 99
