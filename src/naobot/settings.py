from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    robot_id: str = "naobot"
    device_token: str | None = None
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
    session_idle_ms: int = 30000
    tts_resume_delay_ms: int = 200
    video_fps: int = 10
    video_event_fps: int = 15
    media_video_window_ms: int = 10000
    media_audio_window_ms: int = 15000
    media_video_queue_limit: int = 20
    media_audio_queue_limit: int = 100
    local_vad_enabled: bool = True
    vad_rms_threshold: float = 500.0
    vad_end_silence_ms: int = 400
    local_phrase_model: str | None = None
    temporal_summary_interval_ms: int = 1000
    identity_match_threshold: float = 0.78
    identity_match_interval_ms: int = 1000
    identity_enrollment_similarity_threshold: float = 0.8
    asr_endpoint: str | None = None
    asr_model: str | None = None
    asr_api_key: str | None = None
    tts_endpoint: str | None = None
    tts_model: str | None = None
    tts_api_key: str | None = None
    tts_voice: str = "alloy"
    vision_endpoint: str | None = None
    vision_model: str | None = None
    vision_api_key: str | None = None
    wake_model_path: str | None = None
    identity_model_path: str | None = None
    sherpa_onnx_model_path: str | None = None
    sherpa_onnx_tokens_path: str | None = None
    sherpa_onnx_lexicon_path: str | None = None
    sherpa_onnx_data_dir: str | None = None
    sherpa_onnx_rule_fsts: str | None = None
    sherpa_onnx_num_threads: int = 2
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
            device_token=os.getenv("NAOBOT_DEVICE_TOKEN"),
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
            session_idle_ms=int(os.getenv("NAOBOT_SESSION_IDLE_MS", "30000")),
            tts_resume_delay_ms=int(os.getenv("NAOBOT_TTS_RESUME_DELAY_MS", "200")),
            video_fps=int(os.getenv("NAOBOT_VIDEO_FPS", "10")),
            video_event_fps=int(os.getenv("NAOBOT_VIDEO_EVENT_FPS", "15")),
            media_video_window_ms=int(os.getenv("NAOBOT_MEDIA_VIDEO_WINDOW_MS", "10000")),
            media_audio_window_ms=int(os.getenv("NAOBOT_MEDIA_AUDIO_WINDOW_MS", "15000")),
            media_video_queue_limit=int(os.getenv("NAOBOT_MEDIA_VIDEO_QUEUE_LIMIT", "20")),
            media_audio_queue_limit=int(os.getenv("NAOBOT_MEDIA_AUDIO_QUEUE_LIMIT", "100")),
            local_vad_enabled=os.getenv("NAOBOT_LOCAL_VAD_ENABLED", "true").lower()
            in {"1", "true", "yes", "on"},
            vad_rms_threshold=float(os.getenv("NAOBOT_VAD_RMS_THRESHOLD", "500")),
            vad_end_silence_ms=int(os.getenv("NAOBOT_VAD_END_SILENCE_MS", "400")),
            local_phrase_model=os.getenv("NAOBOT_LOCAL_PHRASE_MODEL"),
            temporal_summary_interval_ms=int(
                os.getenv("NAOBOT_TEMPORAL_SUMMARY_INTERVAL_MS", "1000")
            ),
            identity_match_threshold=float(
                os.getenv("NAOBOT_IDENTITY_MATCH_THRESHOLD", "0.78")
            ),
            identity_match_interval_ms=int(
                os.getenv("NAOBOT_IDENTITY_MATCH_INTERVAL_MS", "1000")
            ),
            identity_enrollment_similarity_threshold=float(
                os.getenv("NAOBOT_IDENTITY_ENROLLMENT_SIMILARITY_THRESHOLD", "0.8")
            ),
            asr_endpoint=os.getenv("NAOBOT_ASR_ENDPOINT"),
            asr_model=os.getenv("NAOBOT_ASR_MODEL"),
            asr_api_key=os.getenv("NAOBOT_ASR_API_KEY"),
            tts_endpoint=os.getenv("NAOBOT_TTS_ENDPOINT"),
            tts_model=os.getenv("NAOBOT_TTS_MODEL"),
            tts_api_key=os.getenv("NAOBOT_TTS_API_KEY"),
            tts_voice=os.getenv("NAOBOT_TTS_VOICE", "alloy"),
            vision_endpoint=os.getenv("NAOBOT_VISION_ENDPOINT"),
            vision_model=os.getenv("NAOBOT_VISION_MODEL"),
            vision_api_key=os.getenv("NAOBOT_VISION_API_KEY"),
            wake_model_path=os.getenv("NAOBOT_WAKE_MODEL_PATH"),
            identity_model_path=os.getenv("NAOBOT_IDENTITY_MODEL_PATH"),
            sherpa_onnx_model_path=os.getenv("NAOBOT_SHERPA_ONNX_MODEL_PATH"),
            sherpa_onnx_tokens_path=os.getenv("NAOBOT_SHERPA_ONNX_TOKENS_PATH"),
            sherpa_onnx_lexicon_path=os.getenv("NAOBOT_SHERPA_ONNX_LEXICON_PATH"),
            sherpa_onnx_data_dir=os.getenv("NAOBOT_SHERPA_ONNX_DATA_DIR"),
            sherpa_onnx_rule_fsts=os.getenv("NAOBOT_SHERPA_ONNX_RULE_FSTS"),
            sherpa_onnx_num_threads=int(os.getenv("NAOBOT_SHERPA_ONNX_NUM_THREADS", "2")),
            data_key=os.getenv("NAOBOT_DATA_KEY"),
        )

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_model)

    @property
    def brain_timeout_seconds(self) -> float:
        return self.brain_single_timeout_seconds
