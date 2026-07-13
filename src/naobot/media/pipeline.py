from __future__ import annotations

from dataclasses import dataclass

from .buffers import AudioChunk, MediaQueue, TimestampWindow
from .protocol import MediaFrame


@dataclass(slots=True)
class PipelineStats:
    connected: bool
    current_session: str | None
    video_fps: float
    audio_queue: int
    media_dropped: int
    listening: bool
    speaking: bool
    last_transcript: str
    current_person: str | None
    session_trigger: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "connected": self.connected,
            "current_session": self.current_session,
            "video_fps": self.video_fps,
            "audio_queue": self.audio_queue,
            "media_dropped": self.media_dropped,
            "listening": self.listening,
            "speaking": self.speaking,
            "last_transcript": self.last_transcript,
            "current_person": self.current_person,
            "session_trigger": self.session_trigger,
        }


class MediaPipeline:
    def __init__(
        self,
        *,
        video_window_ms: int = 10_000,
        audio_window_ms: int = 15_000,
        video_queue_limit: int = 20,
        audio_queue_limit: int = 100,
    ) -> None:
        if video_queue_limit <= 0:
            raise ValueError("video_queue_limit must be positive")
        if audio_queue_limit <= 0:
            raise ValueError("audio_queue_limit must be positive")
        self.video_window_ms = video_window_ms
        self.audio_window_ms = audio_window_ms
        self.video_queue_limit = video_queue_limit
        self.audio_queue_limit = audio_queue_limit
        self._total_capacity = video_queue_limit + audio_queue_limit
        self._video_window = TimestampWindow(video_window_ms, lambda frame: frame.timestamp_ms)
        self._audio_window = TimestampWindow(audio_window_ms, lambda chunk: chunk.frame.timestamp_ms)
        self._video_queue: MediaQueue[MediaFrame] = MediaQueue()
        self._audio_queue: MediaQueue[AudioChunk] = MediaQueue()
        self._connected = False
        self._current_session: str | None = None
        self._current_person: str | None = None
        self._session_trigger: str | None = None
        self._listening = False
        self._speaking = False
        self._last_transcript = ""
        self._media_dropped = 0

    def update_connection(self, connected: bool) -> None:
        self._connected = connected

    def update_session(
        self,
        session_id: str | None,
        *,
        person_id: str | None,
        trigger: str | None,
    ) -> None:
        self._current_session = session_id
        self._current_person = person_id
        self._session_trigger = trigger

    def set_listening(self, listening: bool) -> None:
        self._listening = listening

    def set_speaking(self, speaking: bool) -> None:
        self._speaking = speaking

    def set_last_transcript(self, transcript: str) -> None:
        self._last_transcript = transcript

    def push_video_frame(self, frame: MediaFrame) -> bool:
        if not self._video_window.append(frame):
            self._media_dropped += 1
            return False
        if self._combined_queue_size() >= self._total_capacity:
            self._drop_oldest_video()
        if len(self._video_queue) >= self.video_queue_limit:
            self._drop_oldest_video()
        self._video_queue.append(frame)
        return True

    def push_audio_chunk(self, frame: MediaFrame) -> bool:
        chunk = AudioChunk(frame=frame)
        if not self._audio_window.append(chunk):
            self._media_dropped += 1
            return False
        if self._combined_queue_size() >= self._total_capacity:
            self._drop_oldest_video()
        if len(self._audio_queue) >= self.audio_queue_limit:
            self._drop_oldest_non_speech_audio()
        if len(self._audio_queue) >= self.audio_queue_limit:
            self._drop_oldest_speech_audio()
        self._audio_queue.append(chunk)
        return True

    def next_video_frame(self) -> MediaFrame | None:
        return self._video_queue.popleft()

    def next_audio_chunk(self) -> AudioChunk | None:
        return self._audio_queue.popleft()

    def video_window(self) -> list[MediaFrame]:
        return self._video_window.items()

    def audio_window(self) -> list[AudioChunk]:
        return self._audio_window.items()

    def video_queue(self) -> list[MediaFrame]:
        return self._video_queue.items()

    def audio_queue(self) -> list[AudioChunk]:
        return self._audio_queue.items()

    def stats(self) -> dict[str, object]:
        return PipelineStats(
            connected=self._connected,
            current_session=self._current_session,
            video_fps=self._estimate_video_fps(),
            audio_queue=len(self._audio_queue),
            media_dropped=self._media_dropped,
            listening=self._listening,
            speaking=self._speaking,
            last_transcript=self._last_transcript,
            current_person=self._current_person,
            session_trigger=self._session_trigger,
        ).as_dict()

    def _estimate_video_fps(self) -> float:
        frames = self._video_window.items()
        if len(frames) < 2:
            return float(len(frames))
        duration_ms = frames[-1].timestamp_ms - frames[0].timestamp_ms
        if duration_ms <= 0:
            return float(len(frames))
        return round(((len(frames) - 1) * 1000.0) / duration_ms, 2)

    def _combined_queue_size(self) -> int:
        return len(self._video_queue) + len(self._audio_queue)

    def _drop_oldest_video(self) -> None:
        if self._video_queue.drop_oldest() is not None:
            self._media_dropped += 1

    def _drop_oldest_non_speech_audio(self) -> None:
        dropped = self._audio_queue.drop_first_matching(lambda item: item.is_speech is False)
        if dropped is not None:
            self._media_dropped += 1

    def _drop_oldest_speech_audio(self) -> None:
        dropped = self._audio_queue.drop_first_matching(lambda item: item.is_speech is True)
        if dropped is not None:
            self._media_dropped += 1
