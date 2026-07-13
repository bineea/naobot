from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

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
    _generation: int
    _persistence_generation: int

    async def save(self, state: AgentState) -> int:
        return await asyncio.shield(
            self._registry._save(
                self.person_id,
                self.agent_role,
                state,
                is_guest=self.is_guest,
                generation=self._generation,
                persistence_generation=self._persistence_generation,
            )
        )


@dataclass
class _PersonActivityGate:
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    active_roles: int = 0
    reset_active: bool = False
    waiting_resets: int = 0

    @asynccontextmanager
    async def role_operation(self):
        async with self.condition:
            await self.condition.wait_for(
                lambda: not self.reset_active and self.waiting_resets == 0
            )
            self.active_roles += 1
        try:
            yield
        finally:
            async with self.condition:
                self.active_roles -= 1
                self.condition.notify_all()

    @asynccontextmanager
    async def reset_operation(self):
        async with self.condition:
            self.waiting_resets += 1
            try:
                await self.condition.wait_for(
                    lambda: self.active_roles == 0 and not self.reset_active
                )
                self.reset_active = True
            finally:
                self.waiting_resets -= 1
        try:
            yield
        finally:
            async with self.condition:
                self.reset_active = False
                self.condition.notify_all()


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
        self._person_gates: dict[str, _PersonActivityGate] = {}
        self._generations: dict[str, int] = {}
        self._cache: dict[tuple[str, str], AgentState] = {}
        self._guest_cache: dict[tuple[str, str], AgentState] = {}

    def _person_lock_for(self, person_id: str) -> asyncio.Lock:
        lock = self._person_locks.get(person_id)
        if lock is None:
            lock = asyncio.Lock()
            self._person_locks[person_id] = lock
        return lock

    def _person_gate_for(self, person_id: str) -> _PersonActivityGate:
        gate = self._person_gates.get(person_id)
        if gate is None:
            gate = _PersonActivityGate()
            self._person_gates[person_id] = gate
        return gate

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
        persistence_generation: int | None,
    ) -> int:
        cache = self._cache_for(is_guest)
        key = (person_id, agent_role)
        copied = state.model_copy(deep=True)
        if is_guest:
            cache[key] = copied
            return 0
        scrubbed_payload = scrub_agent_state_for_storage(copied)
        version = await self.persistence.save_agent_runtime(
            person_id,
            agent_role,
            copied,
            expected_generation=persistence_generation,
        )
        if version == 0:
            return 0
        cache[key] = AgentState.model_validate(scrubbed_payload)
        return version

    async def _save(
        self,
        person_id: str,
        agent_role: str,
        state: AgentState,
        *,
        is_guest: bool,
        generation: int | None = None,
        persistence_generation: int | None = None,
    ) -> int:
        async with self._person_lock_for(person_id):
            if generation is not None and generation != self._generations.get(person_id, 0):
                return 0
            return await self._save_locked(
                person_id,
                agent_role,
                state,
                is_guest=is_guest,
                persistence_generation=persistence_generation,
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
            async with self._person_gate_for(person_id).role_operation():
                async with self._person_lock_for(person_id):
                    generation = self._generations.get(person_id, 0)
                    persistence_generation = self.persistence.person_generation(person_id)
                    state = await asyncio.shield(
                        self._load_locked(person_id, agent_role, is_guest=is_guest)
                    )
                yield RuntimeSession(
                    person_id=person_id,
                    agent_role=agent_role,
                    is_guest=is_guest,
                    state=state,
                    _registry=self,
                    _generation=generation,
                    _persistence_generation=persistence_generation,
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
            async with self._person_gate_for(person_id).role_operation():
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
            async with self._person_gate_for(person_id).role_operation():
                persistence_generation = self.persistence.person_generation(person_id)
                return await asyncio.shield(
                    self._save(
                        person_id,
                        agent_role,
                        state,
                        is_guest=is_guest,
                        persistence_generation=persistence_generation,
                    )
                )

    async def reset_person_runtime(self, person_id: str) -> None:
        await asyncio.shield(self.persistence.initialize())
        async with self._person_gate_for(person_id).reset_operation():
            async with self._person_lock_for(person_id):
                self._generations[person_id] = self._generations.get(person_id, 0) + 1
                self._cache = {
                    key: value for key, value in self._cache.items() if key[0] != person_id
                }
                self._guest_cache = {
                    key: value
                    for key, value in self._guest_cache.items()
                    if key[0] != person_id
                }
                await asyncio.shield(self.persistence.delete_person_runtime(person_id))

    async def destroy_guest_runtime(self, person_id: str) -> None:
        await asyncio.shield(self.persistence.initialize())
        async with self._person_gate_for(person_id).reset_operation():
            async with self._person_lock_for(person_id):
                self._generations[person_id] = self._generations.get(person_id, 0) + 1
                self._guest_cache = {
                    key: value
                    for key, value in self._guest_cache.items()
                    if key[0] != person_id
                }

    def loaded_count(self) -> int:
        return len(self._cache) + len(self._guest_cache)
