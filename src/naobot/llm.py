from __future__ import annotations

import json
from typing import Any

import httpx

from .models import Action, Envelope, ExpressionIntent, LLMDecision, SkillIntent, SoulConfig
from .settings import Settings


class LLMClient:
    async def decide(
        self,
        event: Envelope,
        soul: SoulConfig,
        memories: list[str],
    ) -> LLMDecision:
        raise NotImplementedError


class RuleBasedLLMClient(LLMClient):
    async def decide(
        self,
        event: Envelope,
        soul: SoulConfig,
        memories: list[str],
    ) -> LLMDecision:
        name = event.payload.get("name", "unknown")
        if name == "touch_head":
            return LLMDecision(
                text=f"我在呢，{soul.user_call}。",
                goal="回应用户摸头并友好打招呼",
                expression=ExpressionIntent(emotion="happy", valence=0.8, arousal=0.4, eye_open=0.75),
                skills=[SkillIntent(name="wave", args={"level": 1})],
                actions=[
                    Action(name="set_face", args={"face": "happy"}),
                    Action(name="wave", args={"level": 1}),
                ],
            )
        if name == "battery_low":
            return LLMDecision(
                text=f"{soul.user_call}，我电量有点低，先别让我跑了。",
                goal="提醒用户低电并进入省电状态",
                expression=ExpressionIntent(emotion="sleepy", valence=-0.2, arousal=0.1, eye_open=0.35),
                skills=[SkillIntent(name="chirp", args={"tone": "low_battery"})],
                actions=[
                    Action(name="set_face", args={"face": "sleepy"}),
                    Action(name="chirp", args={"tone": "low_battery"}),
                ],
            )
        if name == "fall_detected":
            return LLMDecision(
                text="我好像摔倒了，先进入安全模式。",
                goal="解释本地安全反射并等待用户处理",
                expression=ExpressionIntent(emotion="alert", valence=-0.5, arousal=0.9, eye_open=1.0),
                skills=[SkillIntent(name="chirp", args={"tone": "alert"})],
                actions=[
                    Action(name="set_face", args={"face": "alert"}),
                    Action(name="chirp", args={"tone": "alert"}),
                ],
            )
        return LLMDecision(
            text=f"{soul.name} 收到事件 {name}。",
            goal="轻量确认收到事件",
            expression=ExpressionIntent(emotion="curious", valence=0.2, arousal=0.4, eye_open=0.85),
            actions=[Action(name="blink")],
        )


class OpenAICompatibleLLMClient(LLMClient):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def decide(
        self,
        event: Envelope,
        soul: SoulConfig,
        memories: list[str],
    ) -> LLMDecision:
        if not self.settings.llm_configured:
            return await RuleBasedLLMClient().decide(event, soul, memories)

        prompt = self._build_prompt(event, soul, memories)
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"

        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"  # type: ignore[union-attr]
        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是 KT2 宿主机 Agent。只能输出 JSON，不允许裸舵机角度。"
                        "优先输出 goal、expression、skills，并同时给出兼容 actions。"
                        "技能和动作必须来自白名单：set_expression, set_face, blink, wave, "
                        "small_step_forward, turn_left, turn_right, gentle_nudge, sit, chirp, sleep, stop。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.4,
            "response_format": {"type": "json_object"},
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(url, headers=headers, json=payload)
                response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            return LLMDecision.model_validate(json.loads(content))
        except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError, ValueError):
            return await RuleBasedLLMClient().decide(event, soul, memories)

    def _build_prompt(self, event: Envelope, soul: SoulConfig, memories: list[str]) -> str:
        return json.dumps(
            {
                "soul": soul.model_dump(),
                "event": event.model_dump(),
                "confirmed_memories": memories,
                "output_schema": {
                    "text": "string",
                    "goal": "string",
                    "expression": {
                        "emotion": "idle|happy|sad|dizzy|sleepy|alert|curious|confused|proud|shy",
                        "valence": -1.0,
                        "arousal": 0.0,
                        "eye_open": 0.0,
                        "pupil_offset_x": 0.0,
                        "blink_rate": 0.0,
                        "duration_ms": 1200,
                    },
                    "skills": [{"name": "string", "args": {}}],
                    "actions": [{"name": "string", "args": {}}],
                    "memory_suggestion": {"type": "none|suggest", "text": "string"},
                },
            },
            ensure_ascii=False,
        )
