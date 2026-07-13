from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import SecretStr

from .llm import LLMClient, RuleBasedLLMClient
from .models import BrainInput, Envelope, LLMDecision, RouteDecision, SoulConfig
from .runtime.registry import RuntimeRegistry
from .settings import Settings

SAFETY_EVENTS = {
    "battery_low",
    "emergency_stop",
    "fall_detected",
    "heartbeat",
    "imu_fault",
}

PRIMARY_SYSTEM_PROMPT = """你是 naobot 的大脑语义意图 agent。
你只负责输出目标、对话、参数化表情、低风险技能和待确认的记忆建议。
禁止输出 actions、舵机角度、PWM、GPIO、像素、任意代码或文件/网络工具调用。
最终输出必须是一个 JSON 对象，不要使用 Markdown 代码块。
技能仅可使用 wave、small_step_forward、turn_left、turn_right、gentle_nudge、sit、chirp、sleep、stop。
"""

SPECIALIST_PROMPTS = (
    ("emotion", "你是情绪表达 agent。只分析适合的对话语气和参数化眼睛表情，输出简短 JSON 建议。"),
    ("behavior", "你是行为技能 agent。只分析可由白名单技能表达的低风险行为，输出简短 JSON 建议。"),
    ("safety", "你是安全质询 agent。检查请求是否涉及身体风险、裸硬件控制或不必要动作，输出简短 JSON 建议。"),
)

EDITOR_SYSTEM_PROMPT = """你是 naobot 的产品负责人 agent。
请收敛多个语义建议，输出唯一最终 JSON 决策。只允许 goal、text、expression、skills、memory_suggestion；
禁止 actions、裸硬件字段、代码和工具调用。若意见冲突，选择风险更低、动作更少的方案。
"""


class StreamingAgent(Protocol):
    def reply_stream(self, inputs: Any) -> Any: ...


AgentFactory = Callable[..., StreamingAgent]


class AgentScopeBrainRuntime(LLMClient):
    """AgentScope 驱动的 L3 语义大脑，失败时退回确定性规则。"""

    def __init__(
        self,
        settings: Settings,
        agent_factory: AgentFactory | None = None,
        fallback: LLMClient | None = None,
        runtime_registry: RuntimeRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.single_timeout_seconds = max(settings.brain_single_timeout_seconds, 0.001)
        self.team_timeout_seconds = max(settings.brain_team_timeout_seconds, 0.001)
        self.max_iters = min(max(settings.brain_max_iters, 1), 4)
        self._agent_factory = agent_factory
        self._fallback = fallback or RuleBasedLLMClient()
        self._runtime_registry = runtime_registry or RuntimeRegistry(settings)
        self._mode = "fallback" if not settings.llm_configured and agent_factory is None else "agentscope"
        self._last_error: str | None = None
        self._team_used = False
        self._last_route = RouteDecision()

    async def decide(
        self,
        event: Envelope,
        soul: SoulConfig,
        memories: list[str],
    ) -> LLMDecision:
        self._team_used = False
        self._last_error = None
        brain_input = self._build_brain_input(event)
        route = self._route(brain_input)
        self._last_route = route

        if route.mode == "deterministic":
            return await self._use_fallback(event, soul, memories, "deterministic_safety_event")

        if not self.settings.llm_configured and self._agent_factory is None:
            return await self._use_fallback(event, soul, memories, "llm_not_configured")

        try:
            if route.mode == "team":
                self._team_used = True
                decision = await asyncio.wait_for(
                    self._run_team_decision(event, brain_input, soul, memories, route),
                    timeout=self.team_timeout_seconds,
                )
            else:
                decision = await asyncio.wait_for(
                    self._run_single_decision(event, brain_input, soul, memories, route),
                    timeout=self.single_timeout_seconds,
                )
                escalation_reason = self._needs_team_escalation(decision)
                if escalation_reason:
                    escalated_route = route.model_copy(
                        update={
                            "mode": "team",
                            "self_escalated": True,
                            "score": max(route.score, 4),
                            "reasons": [*route.reasons, escalation_reason],
                        }
                    )
                    self._last_route = escalated_route
                    self._team_used = True
                    decision = await asyncio.wait_for(
                        self._run_team_decision(
                            event,
                            brain_input,
                            soul,
                            memories,
                            escalated_route,
                            seed_decision=decision,
                        ),
                        timeout=self.team_timeout_seconds,
                    )
            self._mode = "agentscope"
            return decision
        except asyncio.TimeoutError:
            return await self._use_fallback(event, soul, memories, "brain_timeout")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            return await self._use_fallback(event, soul, memories, error)

    async def _run_single_decision(
        self,
        event: Envelope,
        brain_input: BrainInput,
        soul: SoulConfig,
        memories: list[str],
        route: RouteDecision,
    ) -> LLMDecision:
        prompt = self._build_prompt(event, brain_input, soul, memories, route)
        output = await self._run_agent(
            PRIMARY_SYSTEM_PROMPT,
            prompt,
            agent_role="primary",
            person_id=brain_input.person_id,
            is_guest=self._is_guest(brain_input.person_id),
        )
        return LLMDecision.model_validate(self._parse_json_object(output))

    async def _run_team_decision(
        self,
        event: Envelope,
        brain_input: BrainInput,
        soul: SoulConfig,
        memories: list[str],
        route: RouteDecision,
        *,
        seed_decision: LLMDecision | None = None,
    ) -> LLMDecision:
        prompt = self._build_prompt(event, brain_input, soul, memories, route)
        is_guest = self._is_guest(brain_input.person_id)
        recommendations = await asyncio.gather(
            *(
                self._run_agent(
                    system_prompt,
                    prompt,
                    agent_role=role,
                    person_id=brain_input.person_id,
                    is_guest=is_guest,
                )
                for role, system_prompt in SPECIALIST_PROMPTS
            )
        )
        editor_prompt = json.dumps(
            {
                "request": json.loads(prompt),
                "route": route.model_dump(),
                "seed_decision": seed_decision.model_dump() if seed_decision else None,
                "specialist_recommendations": recommendations,
                "output_contract": self._output_contract(),
            },
            ensure_ascii=False,
        )
        output = await self._run_agent(
            EDITOR_SYSTEM_PROMPT,
            editor_prompt,
            agent_role="editor",
            person_id=brain_input.person_id,
            is_guest=is_guest,
        )
        return LLMDecision.model_validate(self._parse_json_object(output))

    async def _run_agent(
        self,
        system_prompt: str,
        prompt: str,
        *,
        agent_role: str,
        person_id: str | None,
        is_guest: bool,
    ) -> str:
        from agentscope.message import Msg, TextBlock

        agent = await self._create_agent(system_prompt, agent_role, person_id, is_guest=is_guest)
        message = Msg(name="naobot", role="user", content=[TextBlock(text=prompt)])
        chunks: list[str] = []
        async for event in agent.reply_stream(message):
            event_type = getattr(event, "type", None)
            event_type = getattr(event_type, "value", event_type)
            if event_type != "TEXT_BLOCK_DELTA":
                continue
            delta = getattr(event, "delta", None)
            if isinstance(delta, str):
                chunks.append(delta)
        output = "".join(chunks).strip()
        if not output:
            raise ValueError("AgentScope returned an empty response")
        state = getattr(agent, "state", None)
        if state is not None and person_id:
            await self._runtime_registry.save_state(
                person_id,
                agent_role,
                state,
                is_guest=is_guest,
            )
        return output

    async def _create_agent(
        self,
        system_prompt: str,
        agent_role: str,
        person_id: str | None,
        *,
        is_guest: bool,
    ) -> StreamingAgent:
        from agentscope.agent import Agent, ContextConfig, ReActConfig
        from agentscope.credential import OpenAICredential
        from agentscope.model import OpenAIChatModel
        from agentscope.state import AgentState
        from agentscope.tool import Toolkit

        state = AgentState()
        if person_id:
            state = await self._runtime_registry.load_state(
                person_id,
                agent_role,
                is_guest=is_guest,
            )
        context_config = ContextConfig(trigger_ratio=0.8, reserve_ratio=0.1)
        if self._agent_factory is not None:
            created = self._invoke_agent_factory(
                system_prompt,
                agent_role=agent_role,
                person_id=person_id,
                state=state,
                context_config=context_config,
            )
            if inspect.isawaitable(created):
                created = await created
            return created

        credential = OpenAICredential(
            api_key=SecretStr(self.settings.llm_api_key or "not-required"),
            base_url=self.settings.llm_base_url,
        )
        model = OpenAIChatModel(
            credential=credential,
            model=str(self.settings.llm_model),
            parameters=OpenAIChatModel.Parameters(
                max_tokens=900,
                temperature=0.35,
                parallel_tool_calls=False,
            ),
            stream=True,
            max_retries=1,
        )
        return Agent(
            name=f"naobot-{agent_role}",
            system_prompt=system_prompt,
            model=model,
            toolkit=Toolkit(),
            state=state,
            context_config=context_config,
            react_config=ReActConfig(max_iters=self.max_iters),
        )

    def _invoke_agent_factory(self, system_prompt: str, **kwargs: Any) -> StreamingAgent:
        if self._agent_factory is None:
            raise RuntimeError("agent_factory is not configured")
        try:
            signature = inspect.signature(self._agent_factory)
        except (TypeError, ValueError):
            return self._agent_factory(system_prompt)
        accepts_var_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        if accepts_var_kwargs:
            return self._agent_factory(system_prompt, **kwargs)
        accepted_kwargs = {
            name: value for name, value in kwargs.items() if name in signature.parameters
        }
        try:
            return self._agent_factory(system_prompt, **accepted_kwargs)
        except TypeError:
            return self._agent_factory(system_prompt)

    def _route(self, brain_input: BrainInput) -> RouteDecision:
        event_name = self._event_name(brain_input)
        if event_name in SAFETY_EVENTS:
            return RouteDecision(mode="deterministic", score=0, reasons=["safety_event"])

        payload = brain_input.event.get("payload", {})
        score = 0
        reasons: list[str] = []
        text = brain_input.transcript.strip()
        combined = " ".join([text, brain_input.vision_summary]).strip()

        if self._has_multi_objective_signal(text):
            score += 1
            reasons.append("multi_objective")
        if self._contains_any(text, ("但是", "不过", "冲突", "矛盾", "改主意", "一方面", "另一方面")):
            score += 1
            reasons.append("conflict")
        if self._contains_any(combined, ("记住", "忘掉", "我叫", "我是", "身份", "名字", "昵称", "联系人")):
            score += 1
            reasons.append("memory_identity")
        if brain_input.vision_summary and self._contains_any(
            combined,
            ("刚才", "现在", "之前", "左边", "右边", "谁", "哪个", "画面", "镜头"),
        ):
            score += 1
            reasons.append("temporal_vision")
        if self._contains_any(text, ("不确定", "如果", "是不是", "先问我", "也许", "大概", "可能")):
            score += 1
            reasons.append("ambiguity")
        if len(text) >= 80:
            score += 1
            reasons.append("long_transcript")

        debug_forced = False
        if payload.get("requires_team") is True:
            debug_forced = True
            reasons.append("debug_requires_team")
        if int(payload.get("complexity", 0) or 0) >= 7:
            debug_forced = True
            reasons.append("debug_complexity")

        if not self.settings.brain_team_enabled:
            return RouteDecision(mode="single", score=score, reasons=reasons)
        if debug_forced or score >= 4:
            return RouteDecision(mode="team", score=score, reasons=reasons)
        return RouteDecision(mode="single", score=score, reasons=reasons)

    async def _use_fallback(
        self,
        event: Envelope,
        soul: SoulConfig,
        memories: list[str],
        error: str,
    ) -> LLMDecision:
        self._mode = "fallback"
        self._last_error = error
        self._team_used = False
        return await self._fallback.decide(event, soul, memories)

    def status(self) -> dict[str, Any]:
        return {
            "runtime": "agentscope-2.0.4",
            "mode": self._mode,
            "team_enabled": self.settings.brain_team_enabled,
            "team_used": self._team_used,
            "max_iters": self.max_iters,
            "single_timeout_seconds": self.single_timeout_seconds,
            "team_timeout_seconds": self.team_timeout_seconds,
            "last_error": self._last_error,
            "last_route": self._last_route.model_dump(),
            "tools_enabled": False,
        }

    def _build_brain_input(self, event: Envelope) -> BrainInput:
        payload = event.payload
        media_refs = payload.get("media_refs") or []
        if not isinstance(media_refs, list):
            media_refs = [str(media_refs)]
        return BrainInput(
            event=event.model_dump(),
            person_id=payload.get("person_id"),
            transcript=str(payload.get("transcript") or payload.get("text") or ""),
            vision_summary=str(payload.get("vision_summary") or ""),
            media_refs=[str(item) for item in media_refs],
        )

    def _build_prompt(
        self,
        event: Envelope,
        brain_input: BrainInput,
        soul: SoulConfig,
        memories: list[str],
        route: RouteDecision,
    ) -> str:
        return json.dumps(
            {
                "soul": soul.model_dump(),
                "event": event.model_dump(),
                "brain_input": brain_input.model_dump(),
                "route": route.model_dump(),
                "confirmed_memories": memories,
                "output_contract": self._output_contract(),
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _output_contract() -> dict[str, Any]:
        return {
            "text": "string",
            "goal": "string",
            "expression": {
                "emotion": "idle|happy|sad|dizzy|sleepy|alert|curious|confused|proud|shy",
                "valence": "-1..1",
                "arousal": "0..1",
                "eye_open": "0..1",
                "pupil_offset_x": "-1..1",
                "blink_rate": "0..1",
                "duration_ms": "0..5000",
            },
            "skills": [{"name": "allowlisted skill", "args": {}}],
            "memory_suggestion": {"type": "none|suggest", "text": "string"},
            "confidence": "0..1",
            "needs_team": "boolean",
            "escalation_reason": "string|null",
        }

    @staticmethod
    def _parse_json_object(output: str) -> dict[str, Any]:
        text = output.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1]).strip()
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < start:
            raise ValueError("AgentScope output is not a JSON object")
        value = json.loads(text[start : end + 1])
        if not isinstance(value, dict):
            raise ValueError("AgentScope output must be a JSON object")
        if "actions" in value:
            raise ValueError("L3 brain is not allowed to author actions")
        return value

    @staticmethod
    def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
        return any(keyword in text for keyword in keywords)

    @staticmethod
    def _has_multi_objective_signal(text: str) -> bool:
        markers = ("先", "再", "然后", "并且", "同时", "另外", "之后")
        return sum(marker in text for marker in markers) >= 2

    @staticmethod
    def _event_name(brain_input: BrainInput) -> str:
        return str(brain_input.event.get("payload", {}).get("name", ""))

    @staticmethod
    def _needs_team_escalation(decision: LLMDecision) -> str | None:
        if decision.needs_team:
            return decision.escalation_reason or "llm_requested_team"
        if decision.confidence < 0.65:
            return decision.escalation_reason or f"low_confidence:{decision.confidence:.2f}"
        return None

    @staticmethod
    def _is_guest(person_id: str | None) -> bool:
        if not person_id:
            return True
        lowered = person_id.lower()
        return lowered.startswith("guest") or lowered.startswith("visitor")
