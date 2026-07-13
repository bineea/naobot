from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    robot_id: str = "naobot"
    runtime_dir: Path = Path("runtime")
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    robot_heartbeat_timeout_ms: int = 7000
    host_heartbeat_interval_ms: int = 2000
    event_queue_capacity: int = 32
    brain_single_timeout_seconds: float = 6.0
    brain_team_timeout_seconds: float = 15.0
    brain_max_iters: int = 4
    brain_team_enabled: bool = True
    brain_debug_force_team_override: bool = False
    data_key: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        default_single_timeout = os.getenv(
            "NAOBOT_BRAIN_SINGLE_TIMEOUT_SECONDS",
            os.getenv("NAOBOT_BRAIN_TIMEOUT_SECONDS", "6.0"),
        )
        return cls(
            host=os.getenv("NAOBOT_HOST", "127.0.0.1"),
            port=int(os.getenv("NAOBOT_PORT", "8765")),
            robot_id=os.getenv("NAOBOT_ROBOT_ID", "naobot"),
            runtime_dir=Path(os.getenv("NAOBOT_RUNTIME_DIR", "runtime")),
            llm_base_url=os.getenv("NAOBOT_LLM_BASE_URL"),
            llm_api_key=os.getenv("NAOBOT_LLM_API_KEY"),
            llm_model=os.getenv("NAOBOT_LLM_MODEL"),
            robot_heartbeat_timeout_ms=int(os.getenv("NAOBOT_ROBOT_HEARTBEAT_TIMEOUT_MS", "7000")),
            host_heartbeat_interval_ms=int(os.getenv("NAOBOT_HOST_HEARTBEAT_INTERVAL_MS", "2000")),
            event_queue_capacity=int(os.getenv("NAOBOT_EVENT_QUEUE_CAPACITY", "32")),
            brain_single_timeout_seconds=float(default_single_timeout),
            brain_team_timeout_seconds=float(
                os.getenv("NAOBOT_BRAIN_TEAM_TIMEOUT_SECONDS", "15.0")
            ),
            brain_max_iters=int(os.getenv("NAOBOT_BRAIN_MAX_ITERS", "4")),
            brain_team_enabled=os.getenv("NAOBOT_BRAIN_TEAM_ENABLED", "true").lower()
            in {"1", "true", "yes", "on"},
            brain_debug_force_team_override=os.getenv(
                "NAOBOT_BRAIN_DEBUG_FORCE_TEAM_OVERRIDE",
                "false",
            ).lower()
            in {"1", "true", "yes", "on"},
            data_key=os.getenv("NAOBOT_DATA_KEY"),
        )

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_model)

    @property
    def brain_timeout_seconds(self) -> float:
        return self.brain_single_timeout_seconds
