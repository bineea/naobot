from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass

from agentscope.state import AgentState

from ..settings import Settings
from .persistence import RuntimePersistence, scrub_agent_state_for_storage


@dataclass
class RuntimeSession:
    person_id: str
    agent_role: str
    is_guest: bool
    state: AgentState
    _registry: RuntimeRegistry

    async def save(self, state: AgentState) -> int:
        return await asyncio.shield(
            self._registry._save(
                self.person_id,
                self.agent_role,
                state,
                is_guest=self.is_guest,
            )
        )


class RuntimeRegistry:
    def __init__(
        self,
        settings: Settings,
        persistence: RuntimePersistence | None = None,
    ) -> None:
        self.settings = settings
        self.persistence = persistence or RuntimePersistence(settings)
        self._person_locks: dict[str, asyncio.Lock] = {}
        self._role_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._cache: dict[tuple[str, str], AgentState] = {}
        self._guest_cache: dict[tuple[str, str], AgentState] = {}

    def _person_lock_for(self, person_id: str) -> asyncio.Lock:
        lock = self._person_locks.get(person_id)
        if lock is None:
            lock = asyncio.Lock()
            self._person_locks[person_id] = lock
        return lock

    def _role_lock_for(self, person_id: str, agent_role: str) -> asyncio.Lock:
        key = (person_id, agent_role)
        lock = self._role_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._role_locks[key] = lock
        return lock

    def _cache_for(self, is_guest: bool) -> dict[tuple[str, str], AgentState]:
        return self._guest_cache if is_guest else self._cache

    async def _load_locked(
        self,
        person_id: str,
        agent_role: str,
        *,
        is_guest: bool,
    ) -> AgentState:
        cache = self._cache_for(is_guest)
        key = (person_id, agent_role)
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

    async def _save_locked(
        self,
        person_id: str,
        agent_role: str,
        state: AgentState,
        *,
        is_guest: bool,
    ) -> int:
        cache = self._cache_for(is_guest)
        key = (person_id, agent_role)
        copied = state.model_copy(deep=True)
        if is_guest:
            cache[key] = copied
            return 0
        scrubbed_payload = scrub_agent_state_for_storage(copied)
        version = await self.persistence.save_agent_runtime(person_id, agent_role, copied)
        cache[key] = AgentState.model_validate(scrubbed_payload)
        return version

    async def _save(
        self,
        person_id: str,
        agent_role: str,
        state: AgentState,
        *,
        is_guest: bool,
    ) -> int:
        async with self._person_lock_for(person_id):
            return await self._save_locked(
                person_id,
                agent_role,
                state,
                is_guest=is_guest,
            )

    @asynccontextmanager
    async def person_runtime(
        self,
        person_id: str,
        agent_role: str,
        *,
        is_guest: bool = False,
    ):
        await asyncio.shield(self.persistence.initialize())
        async with self._role_lock_for(person_id, agent_role):
            async with self._person_lock_for(person_id):
                state = await asyncio.shield(
                    self._load_locked(person_id, agent_role, is_guest=is_guest)
                )
            yield RuntimeSession(
                person_id=person_id,
                agent_role=agent_role,
                is_guest=is_guest,
                state=state,
                _registry=self,
            )

    async def load_state(
        self,
        person_id: str,
        agent_role: str,
        *,
        is_guest: bool = False,
    ) -> AgentState:
        await asyncio.shield(self.persistence.initialize())
        async with self._role_lock_for(person_id, agent_role):
            async with self._person_lock_for(person_id):
                return await asyncio.shield(
                    self._load_locked(person_id, agent_role, is_guest=is_guest)
                )

    async def save_state(
        self,
        person_id: str,
        agent_role: str,
        state: AgentState,
        *,
        is_guest: bool = False,
    ) -> int:
        await asyncio.shield(self.persistence.initialize())
        async with self._role_lock_for(person_id, agent_role):
            return await asyncio.shield(
                self._save(person_id, agent_role, state, is_guest=is_guest)
            )

    async def reset_person_runtime(self, person_id: str) -> None:
        await asyncio.shield(self.persistence.initialize())
        async with self._person_lock_for(person_id):
            self._cache = {
                key: value for key, value in self._cache.items() if key[0] != person_id
            }
            self._guest_cache = {
                key: value for key, value in self._guest_cache.items() if key[0] != person_id
            }
            await asyncio.shield(self.persistence.delete_person_runtime(person_id))

    async def destroy_guest_runtime(self, person_id: str) -> None:
        await asyncio.shield(self.persistence.initialize())
        async with self._person_lock_for(person_id):
            self._guest_cache = {
                key: value for key, value in self._guest_cache.items() if key[0] != person_id
            }

    def loaded_count(self) -> int:
        return len(self._cache) + len(self._guest_cache)
