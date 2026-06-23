from __future__ import annotations

from .llm import LLMClient, OpenAICompatibleLLMClient
from .models import Envelope, MessageType, RobotMode, RobotState, new_id, now_ms
from .policy import PolicyGuard
from .settings import Settings
from .storage import MemoryStore, RoutineStore, SoulStore


class NaobotAgent:
    def __init__(self, settings: Settings, llm: LLMClient | None = None) -> None:
        self.settings = settings
        self.state = RobotState()
        self.policy = PolicyGuard()
        self.soul = SoulStore(settings.runtime_dir)
        self.memory = MemoryStore(settings.runtime_dir)
        self.routines = RoutineStore(settings.runtime_dir)
        self.llm = llm or OpenAICompatibleLLMClient(settings)
        self.logs: list[dict] = []
        self.last_intent: Envelope | None = None

    def log(self, kind: str, data: dict) -> None:
        self.logs.append({"ts_ms": now_ms(), "kind": kind, "data": data})
        self.logs = self.logs[-200:]

    def update_state_from_envelope(self, envelope: Envelope) -> None:
        payload = envelope.payload
        if envelope.type == MessageType.EVENT:
            self.state.last_event = payload.get("name")
            self.state.battery_pct = int(payload.get("battery_pct", self.state.battery_pct))
            self.state.posture = payload.get("posture", self.state.posture)
            if payload.get("name") == "battery_low":
                self.state.mode = RobotMode.LOW_BATTERY
            elif payload.get("name") == "fall_detected":
                self.state.mode = RobotMode.FAULT
        elif envelope.type == MessageType.STATUS:
            self.state.battery_pct = int(payload.get("battery_pct", self.state.battery_pct))
            self.state.posture = payload.get("posture", self.state.posture)
            if payload.get("mode"):
                self.state.mode = RobotMode(payload["mode"])

    async def handle_robot_message(self, envelope: Envelope) -> Envelope | None:
        self.log("robot_rx", envelope.model_dump())
        self.update_state_from_envelope(envelope)
        if envelope.type == MessageType.EVENT:
            return await self.create_intent(envelope)
        if envelope.type == MessageType.ACK:
            self.log("ack", envelope.payload)
        if envelope.type == MessageType.ERROR:
            self.log("robot_error", envelope.payload)
        return None

    async def create_intent(self, event: Envelope) -> Envelope:
        soul = self.soul.get()
        memories = [item.text for item in self.memory.list(confirmed=True)]
        decision = await self.llm.decide(event, soul, memories)

        suggestion = decision.memory_suggestion
        if suggestion.get("type") == "suggest" and suggestion.get("text"):
            self.memory.suggest(str(suggestion["text"]), source="llm")

        intent = Envelope(
            type=MessageType.INTENT,
            id=new_id("int"),
            seq=event.seq + 1,
            ts_ms=now_ms(),
            session_id=event.session_id,
            priority=4,
            deadline_ms=4000,
            payload={
                "actions": [action.model_dump() for action in decision.actions],
                "text": decision.text,
            },
        )
        result = self.policy.validate_intent(intent, self.state)
        if not result.accepted:
            intent = Envelope(
                type=MessageType.ERROR,
                id=new_id("err"),
                seq=event.seq + 1,
                ts_ms=now_ms(),
                session_id=event.session_id,
                priority=8,
                payload={"code": "POLICY_REJECTED", "message": result.reason},
            )
        else:
            self.last_intent = intent
        self.log("agent_tx", intent.model_dump())
        return intent

    def status(self) -> dict:
        return {
            "robot": self.state.model_dump(),
            "soul": self.soul.get().model_dump(),
            "llm_configured": self.settings.llm_configured,
            "last_intent": self.last_intent.model_dump() if self.last_intent else None,
            "logs": self.logs[-50:],
        }
