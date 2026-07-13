from __future__ import annotations

import asyncio

from agentscope.state import AgentState

from ..settings import Settings
from .persistence import RuntimePersistence, scrub_agent_state_for_storage


class RuntimeRegistry:
    def __init__(
        self,
        settings: Settings,
        persistence: RuntimePersistence | None = None,
    ) -> None:
        self.settings = settings
        self.persistence = persistence or RuntimePersistence(settings)
        self._locks: dict[str, asyncio.Lock] = {}
        self._cache: dict[tuple[str, str], AgentState] = {}
        self._guest_cache: dict[tuple[str, str], AgentState] = {}

    def _lock_for(self, person_id: str) -> asyncio.Lock:
        lock = self._locks.get(person_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[person_id] = lock
        return lock

    async def load_state(
        self,
        person_id: str,
        agent_role: str,
        *,
        is_guest: bool = False,
    ) -> AgentState:
        await self.persistence.initialize()
        cache = self._guest_cache if is_guest else self._cache
        key = (person_id, agent_role)
        async with self._lock_for(person_id):
            cached = cache.get(key)
            if cached is not None:
                return cached.model_copy(deep=True)
            if is_guest:
                state = AgentState()
                cache[key] = state
                return state.model_copy(deep=True)
            state = await self.persistence.load_agent_runtime(person_id, agent_role)
            if state is None:
                state = AgentState()
            cache[key] = state
            return state.model_copy(deep=True)

    async def save_state(
        self,
        person_id: str,
        agent_role: str,
        state: AgentState,
        *,
        is_guest: bool = False,
    ) -> int:
        await self.persistence.initialize()
        key = (person_id, agent_role)
        async with self._lock_for(person_id):
            copied = state.model_copy(deep=True)
            if is_guest:
                self._guest_cache[key] = copied
                return 0
            self._cache[key] = scrub_agent_state_for_storage(copied)
            return await self.persistence.save_agent_runtime(person_id, agent_role, copied)

    async def reset_person_runtime(self, person_id: str) -> None:
        async with self._lock_for(person_id):
            self._cache = {
                key: value for key, value in self._cache.items() if key[0] != person_id
            }
            self._guest_cache = {
                key: value for key, value in self._guest_cache.items() if key[0] != person_id
            }
            await self.persistence.delete_person_runtime(person_id)

    async def destroy_guest_runtime(self, person_id: str) -> None:
        async with self._lock_for(person_id):
            self._guest_cache = {
                key: value for key, value in self._guest_cache.items() if key[0] != person_id
            }
