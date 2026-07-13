from __future__ import annotations

import time
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MessageType(StrEnum):
    EVENT = "event"
    INTENT = "intent"
    ACK = "ack"
    STATUS = "status"
    ERROR = "error"
    HEARTBEAT = "heartbeat"


class RobotMode(StrEnum):
    BOOT = "boot"
    READY_LOCAL = "ready_local"
    AGENT_CONNECTED = "agent_connected"
    EXECUTING = "executing"
    PAUSED = "paused"
    LOW_BATTERY = "low_battery"
    FAULT = "fault"


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class Action(BaseModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class ExpressionIntent(BaseModel):
    emotion: str = "idle"
    valence: float = 0.0
    arousal: float = 0.3
    eye_open: float = 0.8
    pupil_offset_x: float = 0.0
    blink_rate: float = 0.2
    duration_ms: int = 1200


class SkillIntent(BaseModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class SemanticIntentPayload(BaseModel):
    text: str = ""
    goal: str = ""
    expression: ExpressionIntent | None = None
    skills: list[SkillIntent] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)


class Envelope(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    type: MessageType
    id: str = Field(default_factory=lambda: new_id("msg"))
    seq: int = 0
    ts_ms: int = Field(default_factory=now_ms)
    session_id: str = "default"
    priority: int = 3
    deadline_ms: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("priority")
    @classmethod
    def priority_range(cls, value: int) -> int:
        if value < 0 or value > 10:
            raise ValueError("priority must be between 0 and 10")
        return value

    def is_expired(self, current_ms: int | None = None) -> bool:
        if self.deadline_ms is None:
            return False
        current_ms = now_ms() if current_ms is None else current_ms
        return current_ms > self.ts_ms + self.deadline_ms


class RobotState(BaseModel):
    mode: RobotMode = RobotMode.READY_LOCAL
    battery_pct: int = 100
    posture: str = "upright"
    agent_connected: bool = False
    last_event: str | None = None
    control_authority: str = "idle"
    reflex_state: str = "none"
    motion_state: str = "idle"
    last_reflex: str | None = None
    link_state: Literal["disconnected", "connected", "stale"] = "disconnected"
    last_robot_seen_ms: int | None = None
    last_heartbeat_ms: int | None = None
    heartbeat_age_ms: int | None = None
    heartbeat_seq: int = 0
    remote_heartbeat_ts_ms: int | None = None
    remote_uptime_ms: int | None = None
    camera_fps: int = Field(default=0, ge=0)
    audio_state: str = "unavailable"
    media_queue: int = Field(default=0, ge=0)
    media_dropped: int = Field(default=0, ge=0)
    psram_free: int = Field(default=0, ge=0)
    local_loop_interval_ms: int = Field(default=0, ge=0)
    local_loop_overrun_ms: int = Field(default=0, ge=0)

    @field_validator("battery_pct")
    @classmethod
    def battery_range(cls, value: int) -> int:
        if value < 0 or value > 100:
            raise ValueError("battery_pct must be between 0 and 100")
        return value


class LLMDecision(BaseModel):
    text: str = ""
    goal: str = ""
    expression: ExpressionIntent | None = None
    skills: list[SkillIntent] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)
    memory_suggestion: dict[str, Any] = Field(default_factory=lambda: {"type": "none"})
    confidence: float = 1.0
    needs_team: bool = False
    escalation_reason: str | None = None


class BrainInput(BaseModel):
    event: dict[str, Any]
    person_id: str | None = None
    transcript: str = ""
    vision_summary: str = ""
    media_refs: list[str] = Field(default_factory=list)


class RouteDecision(BaseModel):
    mode: Literal["single", "team", "deterministic"] = "single"
    score: int = 0
    reasons: list[str] = Field(default_factory=list)
    self_escalated: bool = False


class SoulConfig(BaseModel):
    name: str = "小龟"
    user_call: str = "老板"
    language: str = "zh-CN"
    liveliness: int = 60
    talkativeness: int = 35
    humor: int = 40
    clinginess: int = 30
    quiet_start: str = "22:00"
    quiet_end: str = "08:00"
    proactive_enabled: bool = True
    cooldown_minutes: int = 45


class MemoryItem(BaseModel):
    id: str = Field(default_factory=lambda: new_id("mem"))
    text: str
    confirmed: bool = False
    source: str = "suggested"
    created_at_ms: int = Field(default_factory=now_ms)


class Routine(BaseModel):
    id: str = Field(default_factory=lambda: new_id("routine"))
    name: str
    trigger: str
    actions: list[Action]
    enabled: bool = False
    created_by: Literal["user_confirmed", "suggested"] = "suggested"
