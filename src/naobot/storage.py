from __future__ import annotations

import json
from pathlib import Path

from .actions import validate_action
from .models import MemoryItem, Routine, SoulConfig


class JsonFileStore:
    def __init__(self, path: Path, default: object) -> None:
        self.path = path
        self.default = default
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def read(self) -> object:
        if not self.path.exists():
            self.write(self.default)
            return self.default
        return json.loads(self.path.read_text(encoding="utf-8"))

    def write(self, data: object) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class SoulStore:
    def __init__(self, runtime_dir: Path) -> None:
        self.store = JsonFileStore(runtime_dir / "config" / "soul.json", SoulConfig().model_dump())

    def get(self) -> SoulConfig:
        return SoulConfig.model_validate(self.store.read())

    def save(self, soul: SoulConfig) -> SoulConfig:
        self.store.write(soul.model_dump())
        return soul


class MemoryStore:
    def __init__(self, runtime_dir: Path) -> None:
        self.store = JsonFileStore(runtime_dir / "memory" / "memory.json", [])

    def list(self, confirmed: bool | None = None) -> list[MemoryItem]:
        items = [MemoryItem.model_validate(item) for item in self.store.read()]
        if confirmed is None:
            return items
        return [item for item in items if item.confirmed == confirmed]

    def suggest(self, text: str, source: str = "suggested") -> MemoryItem:
        item = MemoryItem(text=text, confirmed=False, source=source)
        items = self.list()
        items.append(item)
        self.store.write([entry.model_dump() for entry in items])
        return item

    def confirm(self, item_id: str) -> MemoryItem:
        items = self.list()
        for item in items:
            if item.id == item_id:
                item.confirmed = True
                self.store.write([entry.model_dump() for entry in items])
                return item
        raise KeyError(item_id)

    def delete(self, item_id: str) -> None:
        items = [item for item in self.list() if item.id != item_id]
        self.store.write([entry.model_dump() for entry in items])


class RoutineStore:
    def __init__(self, runtime_dir: Path) -> None:
        self.store = JsonFileStore(runtime_dir / "routines" / "routines.json", [])

    def list(self) -> list[Routine]:
        return [Routine.model_validate(item) for item in self.store.read()]

    def suggest(self, routine: Routine) -> Routine:
        for action in routine.actions:
            result = validate_action(action.name, action.args)
            if not result.accepted:
                raise ValueError(result.reason)
        routines = self.list()
        routines.append(routine)
        self.store.write([entry.model_dump() for entry in routines])
        return routine

    def confirm(self, routine_id: str) -> Routine:
        routines = self.list()
        for routine in routines:
            if routine.id == routine_id:
                routine.enabled = True
                routine.created_by = "user_confirmed"
                self.store.write([entry.model_dump() for entry in routines])
                return routine
        raise KeyError(routine_id)

    def delete(self, routine_id: str) -> None:
        routines = [routine for routine in self.list() if routine.id != routine_id]
        self.store.write([entry.model_dump() for entry in routines])
