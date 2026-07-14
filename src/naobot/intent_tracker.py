from __future__ import annotations

from .models import now_ms


class IntentTracker:
    """Host 侧 intent 状态机：记录 pending/accepted，终态 completed/failed/error 移除，
    deadline 超时回收。track() 对重复 intent_id 返回 False 供上层去重 log。"""

    def __init__(self, *, capacity: int = 64, default_deadline_ms: int = 4000) -> None:
        self._items: dict[str, dict] = {}
        self._order: list[str] = []
        self.capacity = capacity
        self.default_deadline_ms = default_deadline_ms

    def track(self, intent_id: str | None, deadline_ms: int | None = None, ts_ms: int | None = None) -> bool:
        """记录新 intent。返回 True=新记录，False=重复（已存在，不重置状态）。"""
        if not intent_id:
            return True
        if intent_id in self._items:
            self._touch(intent_id)
            return False
        ts = now_ms() if ts_ms is None else ts_ms
        deadline = self.default_deadline_ms if deadline_ms is None else deadline_ms
        self._items[intent_id] = {"status": "pending", "deadline_ms": ts + deadline}
        self._order.append(intent_id)
        self._evict()
        return True

    def observe_ack(self, intent_id: str | None, status: str = "accepted") -> None:
        if not intent_id or intent_id not in self._items:
            return
        if status in ("completed", "failed"):
            self._remove(intent_id)
        else:
            self._items[intent_id]["status"] = status
            self._touch(intent_id)

    def observe_error(self, intent_id: str | None) -> None:
        if intent_id and intent_id in self._items:
            self._remove(intent_id)

    def reclaim(self, current_ms: int | None = None) -> list[str]:
        """返回超时（超过 deadline）的 intent_id 列表并移除。"""
        now = now_ms() if current_ms is None else current_ms
        expired = [iid for iid, info in self._items.items() if info["deadline_ms"] < now]
        for iid in expired:
            self._remove(iid)
        return expired

    def status(self, intent_id: str | None) -> str | None:
        if not intent_id:
            return None
        info = self._items.get(intent_id)
        return info["status"] if info else None

    def __len__(self) -> int:
        return len(self._items)

    def _touch(self, intent_id: str) -> None:
        if intent_id in self._order:
            self._order.remove(intent_id)
            self._order.append(intent_id)

    def _remove(self, intent_id: str) -> None:
        self._items.pop(intent_id, None)
        if intent_id in self._order:
            self._order.remove(intent_id)

    def _evict(self) -> None:
        while len(self._items) > self.capacity and self._order:
            oldest = self._order.pop(0)
            self._items.pop(oldest, None)
