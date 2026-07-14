from __future__ import annotations

import inspect

from agentscope.message import DataBlock

from .behavior import BehaviorRuntime
from .brain import AgentScopeBrainRuntime
from .intent_tracker import IntentTracker
from .llm import LLMClient
from .models import Envelope, MessageType, RobotMode, RobotState, new_id, now_ms
from .policy import PolicyGuard
from .runtime.registry import RuntimeRegistry
from .settings import Settings
from .storage import MemoryStore, RoutineStore, SoulStore


class NaobotAgent:
    def __init__(self, settings: Settings, llm: LLMClient | None = None) -> None:
        self.settings = settings
        self.state = RobotState()
        self.policy = PolicyGuard()
        self.behavior = BehaviorRuntime(self.policy)
        self.soul = SoulStore(settings.runtime_dir)
        self.memory = MemoryStore(settings.runtime_dir)
        self.routines = RoutineStore(settings.runtime_dir)
        self.brain = llm or AgentScopeBrainRuntime(settings)
        self.runtime_registry = getattr(self.brain, "_runtime_registry", RuntimeRegistry(settings))
        self.llm = self.brain  # 兼容现有注入和测试入口。
        self.logs: list[dict] = []
        self.last_intent: Envelope | None = None
        self.intents = IntentTracker()
        self._host_heartbeat_seq = 0

    def log(self, kind: str, data: dict) -> None:
        self.logs.append({"ts_ms": now_ms(), "kind": kind, "data": data})
        self.logs = self.logs[-200:]

    def update_state_from_envelope(self, envelope: Envelope) -> None:
        payload = envelope.payload
        received_ms = now_ms()
        self._mark_robot_seen(received_ms)
        if envelope.type == MessageType.EVENT:
            self.state.last_event = payload.get("name")
            self.state.battery_pct = int(payload.get("battery_pct", self.state.battery_pct))
            self.state.posture = payload.get("posture", self.state.posture)
            self._update_control_state(payload)
            if payload.get("name") == "battery_low":
                self.state.mode = RobotMode.LOW_BATTERY
            elif payload.get("name") == "fall_detected":
                self.state.mode = RobotMode.FAULT
        elif envelope.type in {MessageType.STATUS, MessageType.HEARTBEAT}:
            self.state.battery_pct = int(payload.get("battery_pct", self.state.battery_pct))
            self.state.posture = payload.get("posture", self.state.posture)
            self._update_control_state(payload)
            if envelope.type == MessageType.HEARTBEAT:
                self.state.last_heartbeat_ms = received_ms
                self.state.heartbeat_seq = envelope.seq
                self.state.remote_heartbeat_ts_ms = envelope.ts_ms
                if "uptime_ms" in payload:
                    self.state.remote_uptime_ms = int(payload["uptime_ms"])
            if payload.get("mode"):
                self.state.mode = RobotMode(payload["mode"])

    def _mark_robot_seen(self, received_ms: int) -> None:
        self.state.last_robot_seen_ms = received_ms
        self.state.agent_connected = True
        self.state.link_state = "connected"

    def _update_control_state(self, payload: dict) -> None:
        self.state.control_authority = payload.get("control_authority", self.state.control_authority)
        self.state.reflex_state = payload.get("reflex_state", self.state.reflex_state)
        self.state.motion_state = payload.get("motion_state", self.state.motion_state)
        self.state.last_reflex = payload.get("last_reflex", self.state.last_reflex)
        self.state.camera_fps = int(payload.get("camera_fps", self.state.camera_fps))
        self.state.audio_state = str(payload.get("audio_state", self.state.audio_state))
        self.state.media_queue = int(payload.get("media_queue", self.state.media_queue))
        self.state.media_dropped = int(payload.get("media_dropped", self.state.media_dropped))
        self.state.psram_free = int(payload.get("psram_free", self.state.psram_free))
        self.state.local_loop_interval_ms = int(
            payload.get("local_loop_interval_ms", self.state.local_loop_interval_ms)
        )
        self.state.local_loop_overrun_ms = int(
            payload.get("local_loop_overrun_ms", self.state.local_loop_overrun_ms)
        )

    async def handle_robot_message(self, envelope: Envelope) -> Envelope | None:
        self.observe_robot_message(envelope)
        if envelope.type == MessageType.EVENT:
            return await self.create_intent(envelope)
        return None

    def observe_robot_message(self, envelope: Envelope) -> None:
        """记录机器人消息并刷新状态，不触发可能耗时的大脑推理。"""
        self.log("robot_rx", envelope.model_dump())
        self.update_state_from_envelope(envelope)
        if envelope.type == MessageType.ACK:
            payload = envelope.payload
            self.intents.observe_ack(payload.get("intent_id"), payload.get("status", "accepted"))
            self.log("ack", payload)
        if envelope.type == MessageType.ERROR:
            payload = envelope.payload
            self.intents.observe_error(payload.get("intent_id"))
            self.log("robot_error", payload)

    async def create_intent(
        self,
        event: Envelope,
        media_blocks: list[DataBlock] | None = None,
    ) -> Envelope:
        soul = self.soul.get()
        memories = [item.text for item in self.memory.list(confirmed=True)]
        if media_blocks:
            try:
                signature = inspect.signature(self.brain.decide)
            except (TypeError, ValueError):
                signature = None
            if signature is None or "media_blocks" in signature.parameters:
                decision = await self.brain.decide(
                    event,
                    soul,
                    memories,
                    media_blocks=media_blocks,
                )
            else:
                decision = await self.brain.decide(event, soul, memories)
        else:
            decision = await self.brain.decide(event, soul, memories)

        suggestion = decision.memory_suggestion
        if suggestion.get("type") == "suggest" and suggestion.get("text"):
            self.memory.suggest(str(suggestion["text"]), source="llm")

        intent = self.behavior.compile(decision, event, self.state)
        if intent.type == MessageType.INTENT:
            self.last_intent = intent
            tracked = self.intents.track(
                intent.id,
                getattr(intent, "deadline_ms", None),
                getattr(intent, "ts_ms", None),
            )
            if not tracked:
                self.log("intent_dedup", {"intent_id": intent.id})
        self.log("agent_tx", intent.model_dump())
        return intent

    def reclaim_stale_intents(self, current_ms: int | None = None) -> list[str]:
        """回收超时未收到终态回执的 intent，返回被回收的 intent_id 列表。"""
        expired = self.intents.reclaim(current_ms)
        for intent_id in expired:
            self.log("intent_timeout", {"intent_id": intent_id})
        return expired

    def refresh_link_state(self, current_ms: int | None = None) -> None:
        current_ms = now_ms() if current_ms is None else current_ms
        if self.state.last_robot_seen_ms is None:
            self.state.agent_connected = False
            self.state.link_state = "disconnected"
            self.state.heartbeat_age_ms = None
            return
        if self.state.last_heartbeat_ms is not None:
            self.state.heartbeat_age_ms = max(0, current_ms - self.state.last_heartbeat_ms)
        if current_ms - self.state.last_robot_seen_ms > self.settings.robot_heartbeat_timeout_ms:
            self.state.agent_connected = False
            self.state.link_state = "stale"
        else:
            self.state.agent_connected = True
            self.state.link_state = "connected"

    def host_heartbeat(self) -> Envelope:
        self._host_heartbeat_seq = (self._host_heartbeat_seq + 1) & 0xFFFFFFFF
        return Envelope(
            type=MessageType.HEARTBEAT,
            id=new_id("host_hb"),
            seq=self._host_heartbeat_seq,
            priority=1,
            payload={
                "source": "host",
                "host_ts_ms": now_ms(),
                "agent_mode": self.state.mode,
                "last_intent_id": self.last_intent.id if self.last_intent else None,
            },
        )

    def status(self) -> dict:
        self.refresh_link_state()
        brain_status = getattr(self.brain, "status", None)
        brain = (
            brain_status()
            if callable(brain_status)
            else {
                "runtime": type(self.brain).__name__,
                "mode": "rules" if type(self.brain).__name__ == "RuleBasedLLMClient" else "injected",
            }
        )
        return {
            "robot": self.state.model_dump(),
            "soul": self.soul.get().model_dump(),
            "llm_configured": self.settings.llm_configured,
            "brain": brain,
            "runtime_loaded_count": self.runtime_registry.loaded_count(),
            "last_intent": self.last_intent.model_dump() if self.last_intent else None,
            "logs": self.logs[-50:],
        }
