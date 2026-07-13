from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    runtime_dir: Path = Path("runtime")
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    robot_heartbeat_timeout_ms: int = 7000
    host_heartbeat_interval_ms: int = 2000
    event_queue_capacity: int = 32
    brain_timeout_seconds: float = 4.0
    brain_max_iters: int = 4
    brain_team_enabled: bool = True

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            host=os.getenv("NAOBOT_HOST", "127.0.0.1"),
            port=int(os.getenv("NAOBOT_PORT", "8765")),
            runtime_dir=Path(os.getenv("NAOBOT_RUNTIME_DIR", "runtime")),
            llm_base_url=os.getenv("NAOBOT_LLM_BASE_URL"),
            llm_api_key=os.getenv("NAOBOT_LLM_API_KEY"),
            llm_model=os.getenv("NAOBOT_LLM_MODEL"),
            robot_heartbeat_timeout_ms=int(os.getenv("NAOBOT_ROBOT_HEARTBEAT_TIMEOUT_MS", "7000")),
            host_heartbeat_interval_ms=int(os.getenv("NAOBOT_HOST_HEARTBEAT_INTERVAL_MS", "2000")),
            event_queue_capacity=int(os.getenv("NAOBOT_EVENT_QUEUE_CAPACITY", "32")),
            brain_timeout_seconds=float(os.getenv("NAOBOT_BRAIN_TIMEOUT_SECONDS", "4.0")),
            brain_max_iters=int(os.getenv("NAOBOT_BRAIN_MAX_ITERS", "4")),
            brain_team_enabled=os.getenv("NAOBOT_BRAIN_TEAM_ENABLED", "true").lower()
            in {"1", "true", "yes", "on"},
        )

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_model)
