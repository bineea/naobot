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

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            host=os.getenv("NAOBOT_HOST", "127.0.0.1"),
            port=int(os.getenv("NAOBOT_PORT", "8765")),
            runtime_dir=Path(os.getenv("NAOBOT_RUNTIME_DIR", "runtime")),
            llm_base_url=os.getenv("NAOBOT_LLM_BASE_URL"),
            llm_api_key=os.getenv("NAOBOT_LLM_API_KEY"),
            llm_model=os.getenv("NAOBOT_LLM_MODEL"),
        )

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_model)
