from __future__ import annotations

import asyncio
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from .protocol import MediaFrame

T = TypeVar("T")


@dataclass(slots=True)
class AudioChunk:
    frame: MediaFrame
    is_speech: bool | None = None

    def __post_init__(self) -> None:
        if self.is_speech is None:
            self.is_speech = self.frame.is_speech


class TimestampWindow(Generic[T]):
    def __init__(self, window_ms: int, timestamp_getter: Callable[[T], int]) -> None:
        if window_ms <= 0:
            raise ValueError("window_ms must be positive")
        self._window_ms = window_ms
        self._timestamp_getter = timestamp_getter
        self._items: deque[T] = deque()
        self._last_timestamp_ms: int | None = None

    def append(self, item: T) -> bool:
        timestamp_ms = self._timestamp_getter(item)
        if self._last_timestamp_ms is not None and timestamp_ms < self._last_timestamp_ms:
            return False
        self._items.append(item)
        self._last_timestamp_ms = timestamp_ms
        self.trim(timestamp_ms)
        return True

    def trim(self, current_timestamp_ms: int) -> None:
        cutoff = current_timestamp_ms - self._window_ms
        while self._items and self._timestamp_getter(self._items[0]) < cutoff:
            self._items.popleft()

    def items(self) -> list[T]:
        return list(self._items)


class MediaQueue(Generic[T]):
    def __init__(self) -> None:
        self._items: deque[T] = deque()

    def append(self, item: T) -> None:
        self._items.append(item)

    def popleft(self) -> T | None:
        if not self._items:
            return None
        return self._items.popleft()

    def drop_oldest(self) -> T | None:
        return self.popleft()

    def drop_first_matching(self, predicate: Callable[[T], bool]) -> T | None:
        for index, item in enumerate(self._items):
            if predicate(item):
                del self._items[index]
                return item
        return None

    def items(self) -> list[T]:
        return list(self._items)

    def __len__(self) -> int:
        return len(self._items)


class MediaIngressQueue:
    def __init__(self, maxsize: int) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        self.maxsize = maxsize
        self._items: deque[MediaFrame] = deque()
        self._available = asyncio.Event()
        self._dropped_by_kind: Counter[str] = Counter()
        self._dropped_by_reason: Counter[str] = Counter()

    @property
    def dropped(self) -> dict[str, object]:
        return {
            "total": sum(self._dropped_by_kind.values()),
            "by_kind": dict(self._dropped_by_kind),
            "by_reason": dict(self._dropped_by_reason),
        }

    def put_nowait(self, frame: MediaFrame) -> bool:
        if len(self._items) >= self.maxsize:
            dropped = self._drop_first(lambda item: item.kind.name == "JPEG")
            reason = "evicted_oldest_jpeg"
            if dropped is None:
                dropped = self._drop_first(
                    lambda item: item.kind.name == "AUDIO_PCM16"
                    and not item.is_speech
                    and not item.is_end_of_utterance
                )
                reason = "evicted_oldest_non_speech_audio"
            if dropped is None:
                self._record_drop(frame, "queue_full_protected")
                return False
            self._record_drop(dropped, reason)
        self._items.append(frame)
        self._available.set()
        return True

    async def get(self) -> MediaFrame:
        while not self._items:
            self._available.clear()
            await self._available.wait()
        return self.get_nowait()

    def get_nowait(self) -> MediaFrame:
        if not self._items:
            raise asyncio.QueueEmpty
        frame = self._items.popleft()
        if not self._items:
            self._available.clear()
        return frame

    def qsize(self) -> int:
        return len(self._items)

    def empty(self) -> bool:
        return not self._items

    def clear(self) -> None:
        self._items.clear()
        self._available.clear()

    def _drop_first(self, predicate: Callable[[MediaFrame], bool]) -> MediaFrame | None:
        for index, item in enumerate(self._items):
            if predicate(item):
                del self._items[index]
                return item
        return None

    def _record_drop(self, frame: MediaFrame, reason: str) -> None:
        self._dropped_by_kind[frame.kind.name] += 1
        self._dropped_by_reason[reason] += 1
