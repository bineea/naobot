from __future__ import annotations

from collections import deque
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
