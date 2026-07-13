from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import SecretStr

from .llm import LLMClient, RuleBasedLLMClient
from .models import Envelope, LLMDecision, SoulConfig
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
    "你是情绪表达 agent。只分析适合的对话语气和参数化眼睛表情，输出简短 JSON 建议。",
    "你是行为技能 agent。只分析可由白名单技能表达的低风险行为，输出简短 JSON 建议。",
    "你是安全质询 agent。检查请求是否涉及身体风险、裸硬件控制或不必要动作，输出简短 JSON 建议。",
)

EDITOR_SYSTEM_PROMPT = """你是 naobot 的产品负责人 agent。
请收敛多个语义建议，输出唯一最终 JSON 决策。只允许 goal、text、expression、skills、memory_suggestion；
禁止 actions、裸硬件字段、代码和工具调用。若意见冲突，选择风险更低、动作更少的方案。
"""


class StreamingAgent(Protocol):
    def reply_stream(self, inputs: Any) -> Any: ...


AgentFactory = Callable[[str], StreamingAgent]


class AgentScopeBrainRuntime(LLMClient):
    """AgentScope 驱动的 L3 语义大脑，失败时退回确定性规则。"""

    def __init__(
        self,
        settings: Settings,
        agent_factory: AgentFactory | None = None,
        fallback: LLMClient | None = None,
    ) -> None:
        self.settings = settings
        self.timeout_seconds = min(max(settings.brain_timeout_seconds, 0.001), 4.0)
        self.max_iters = min(max(settings.brain_max_iters, 1), 4)
        self._agent_factory = agent_factory
        self._fallback = fallback or RuleBasedLLMClient()
        self._mode = "fallback" if not settings.llm_configured else "agentscope"
        self._last_error: str | None = None
        self._team_used = False

    async def decide(
        self,
        event: Envelope,
        soul: SoulConfig,
        memories: list[str],
    ) -> LLMDecision:
        self._team_used = False
        self._last_error = None
        if not self.settings.llm_configured and self._agent_factory is None:
            return await self._use_fallback(event, soul, memories, "llm_not_configured")

        try:
            decision = await asyncio.wait_for(
                self._run_decision(event, soul, memories),
                timeout=self.timeout_seconds,
            )
        except TimeoutError:
            return await self._use_fallback(event, soul, memories, "brain_timeout")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            return await self._use_fallback(event, soul, memories, error)

        self._mode = "agentscope"
        return decision

    async def _run_decision(
        self,
        event: Envelope,
        soul: SoulConfig,
        memories: list[str],
    ) -> LLMDecision:
        prompt = self._build_prompt(event, soul, memories)
        if self._should_use_team(event):
            self._team_used = True
            recommendations = await asyncio.gather(
                *(self._run_agent(system_prompt, prompt) for system_prompt in SPECIALIST_PROMPTS)
            )
            editor_prompt = json.dumps(
                {
                    "request": json.loads(prompt),
                    "specialist_recommendations": recommendations,
                    "output_contract": self._output_contract(),
                },
                ensure_ascii=False,
            )
            output = await self._run_agent(EDITOR_SYSTEM_PROMPT, editor_prompt)
        else:
            output = await self._run_agent(PRIMARY_SYSTEM_PROMPT, prompt)
        return LLMDecision.model_validate(self._parse_json_object(output))

    async def _run_agent(self, system_prompt: str, prompt: str) -> str:
        from agentscope.message import Msg, TextBlock

        agent = self._create_agent(system_prompt)
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
        return output

    def _create_agent(self, system_prompt: str) -> StreamingAgent:
        if self._agent_factory is not None:
            return self._agent_factory(system_prompt)

        from agentscope.agent import Agent, ReActConfig
        from agentscope.credential import OpenAICredential
        from agentscope.model import OpenAIChatModel
        from agentscope.tool import Toolkit

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
            name="naobot-brain",
            system_prompt=system_prompt,
            model=model,
            toolkit=Toolkit(),
            react_config=ReActConfig(max_iters=self.max_iters),
        )

    def _should_use_team(self, event: Envelope) -> bool:
        name = str(event.payload.get("name", ""))
        if name in SAFETY_EVENTS or not self.settings.brain_team_enabled:
            return False
        if event.payload.get("requires_team") is True:
            return True
        return int(event.payload.get("complexity", 0) or 0) >= 7

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
            "timeout_seconds": self.timeout_seconds,
            "last_error": self._last_error,
            "tools_enabled": False,
        }

    def _build_prompt(self, event: Envelope, soul: SoulConfig, memories: list[str]) -> str:
        return json.dumps(
            {
                "soul": soul.model_dump(),
                "event": event.model_dump(),
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
