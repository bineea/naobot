from __future__ import annotations

import asyncio
import json
import secrets
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError

from .agent import NaobotAgent
from .media.service import MediaService
from .models import Action, Envelope, MessageType, Routine, SoulConfig, new_id, now_ms
from .policy import PolicyGuard
from .settings import Settings


@dataclass(frozen=True)
class QueuePutResult:
    accepted: bool
    evicted: Envelope | None = None


class BoundedPriorityEventQueue:
    """有界优先级事件队列；高优先级优先，同优先级保持 FIFO。"""

    def __init__(self, capacity: int = 32) -> None:
        if capacity < 1:
            raise ValueError("event queue capacity must be positive")
        self.capacity = capacity
        self._buckets: list[deque[Envelope]] = [deque() for _ in range(11)]
        self._size = 0
        self._condition = asyncio.Condition()

    async def put(self, envelope: Envelope) -> QueuePutResult:
        async with self._condition:
            evicted = None
            if self._size >= self.capacity:
                lowest = next((index for index, bucket in enumerate(self._buckets) if bucket), None)
                if lowest is None or envelope.priority <= lowest:
                    return QueuePutResult(False)
                evicted = self._buckets[lowest].popleft()
                self._size -= 1
            self._buckets[envelope.priority].append(envelope)
            self._size += 1
            self._condition.notify()
            return QueuePutResult(True, evicted)

    async def get(self) -> Envelope:
        async with self._condition:
            while self._size == 0:
                await self._condition.wait()
            for priority in range(10, -1, -1):
                if self._buckets[priority]:
                    self._size -= 1
                    return self._buckets[priority].popleft()
        raise RuntimeError("event queue size is inconsistent")


class DashboardHub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.clients.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self.clients.discard(websocket)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for client in self.clients:
            try:
                await client.send_json(payload)
            except RuntimeError:
                dead.append(client)
        for client in dead:
            self.disconnect(client)


class RobotHub:
    def __init__(self) -> None:
        self.websocket: WebSocket | None = None
        self._send_lock = asyncio.Lock()

    def connect(self, websocket: WebSocket) -> None:
        self.websocket = websocket

    def disconnect(self, websocket: WebSocket) -> None:
        if self.websocket is websocket:
            self.websocket = None

    async def send_envelope(self, envelope: Envelope) -> bool:
        async with self._send_lock:
            if self.websocket is None:
                return False
            try:
                await self.websocket.send_json(envelope.model_dump())
            except (RuntimeError, WebSocketDisconnect):
                self.websocket = None
                return False
            return True

    async def send_intent(self, intent: Envelope) -> bool:
        return await self.send_envelope(intent)


def create_app(
    settings: Settings | None = None,
    agent: NaobotAgent | None = None,
    media_service: MediaService | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    agent = agent or NaobotAgent(settings)
    media_service = media_service or MediaService(settings=settings, agent=agent)
    hub = DashboardHub()
    robot_hub = RobotHub()
    attach = getattr(media_service, "attach", None)
    if callable(attach):
        attach(robot_hub=robot_hub, dashboard_hub=hub)
    app = FastAPI(title="naobot", version="0.1.0")
    app.state.agent = agent
    app.state.dashboard_hub = hub
    app.state.robot_hub = robot_hub
    app.state.media_service = media_service

    def _is_loopback_client(request: Request) -> bool:
        host = request.client.host if request.client is not None else ""
        return host in {"127.0.0.1", "::1", "localhost", "testclient"}

    def _is_management_api_authorized(request: Request) -> bool:
        if settings.device_token is None:
            return _is_loopback_client(request)
        auth_header = request.headers.get("Authorization", "")
        candidate = ""
        if auth_header.startswith("Bearer "):
            candidate = auth_header[7:]
        if not candidate:
            candidate = request.headers.get("X-Naobot-Token", "")
        return bool(candidate) and secrets.compare_digest(candidate, settings.device_token)

    @app.middleware("http")
    async def require_management_api_auth(request: Request, call_next):
        if request.url.path.startswith("/api/") and not _is_management_api_authorized(request):
            return JSONResponse(status_code=403, content={"detail": {"code": "FORBIDDEN"}})
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        html_path = Path(__file__).with_name("web") / "index.html"
        return html_path.read_text(encoding="utf-8")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/status")
    async def api_status() -> dict[str, Any]:
        status = agent.status()
        status["media"] = media_service.status()
        return status

    @app.get("/api/people")
    async def api_people() -> list[dict[str, Any]]:
        return await media_service.list_people()

    @app.post("/api/people/{person_id}/runtime/reset")
    async def api_reset_person_runtime(person_id: str) -> dict[str, str]:
        await media_service.reset_person_runtime(person_id)
        return {"status": "reset"}

    @app.delete("/api/people/{person_id}")
    async def api_delete_person(person_id: str) -> dict[str, str]:
        await media_service.delete_person(person_id)
        return {"status": "deleted"}

    @app.post("/api/people/enrollment/cancel")
    async def api_cancel_enrollment() -> dict[str, Any]:
        result = await media_service.cancel_enrollment()
        return result if isinstance(result, dict) else {"status": "cancelled"}

    @app.post("/api/actions/test")
    async def api_action_test(action: Action) -> dict[str, Any]:
        result = PolicyGuard().validate_actions([action], agent.state)
        if not result.accepted:
            raise HTTPException(status_code=403, detail={"code": "POLICY_DENIED", "message": result.reason})
        intent = Envelope(
            type=MessageType.INTENT,
            id=new_id("manual"),
            ts_ms=now_ms(),
            priority=4,
            payload={"actions": [action.model_dump()], "text": "manual dashboard action"},
        )
        agent.last_intent = intent
        agent.log("manual_intent", intent.model_dump())
        robot_sent = await robot_hub.send_intent(intent)
        await hub.broadcast({"kind": "manual_intent", "payload": intent.model_dump()})
        return {"status": "accepted", "robot_sent": robot_sent, "intent": intent.model_dump()}

    @app.post("/api/stop")
    async def api_stop() -> dict[str, Any]:
        intent = Envelope(
            type=MessageType.INTENT,
            id=new_id("stop"),
            priority=10,
            payload={"actions": [{"name": "stop", "args": {}}], "text": "emergency stop"},
        )
        agent.last_intent = intent
        agent.log("stop", intent.model_dump())
        robot_sent = await robot_hub.send_intent(intent)
        await hub.broadcast({"kind": "stop", "payload": intent.model_dump()})
        return {"status": "accepted", "robot_sent": robot_sent, "intent": intent.model_dump()}

    @app.get("/api/soul")
    async def get_soul() -> dict[str, Any]:
        return agent.soul.get().model_dump()

    @app.put("/api/soul")
    async def put_soul(soul: SoulConfig) -> dict[str, Any]:
        saved = agent.soul.save(soul)
        agent.log("soul_saved", saved.model_dump())
        await hub.broadcast({"kind": "soul_saved", "payload": saved.model_dump()})
        return saved.model_dump()

    @app.get("/api/memory")
    async def get_memory() -> list[dict[str, Any]]:
        return [item.model_dump() for item in agent.memory.list()]

    @app.post("/api/memory/suggest")
    async def suggest_memory(payload: dict[str, str]) -> dict[str, Any]:
        text = payload.get("text")
        if not text:
            raise HTTPException(status_code=422, detail={"code": "EMPTY_MEMORY"})
        return agent.memory.suggest(text, source="dashboard").model_dump()

    @app.post("/api/memory/{item_id}/confirm")
    async def confirm_memory(item_id: str) -> dict[str, Any]:
        try:
            return agent.memory.confirm(item_id).model_dump()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"}) from exc

    @app.delete("/api/memory/{item_id}")
    async def delete_memory(item_id: str) -> dict[str, str]:
        agent.memory.delete(item_id)
        return {"status": "deleted"}

    @app.get("/api/routines")
    async def get_routines() -> list[dict[str, Any]]:
        return [routine.model_dump() for routine in agent.routines.list()]

    @app.post("/api/routines/suggest")
    async def suggest_routine(routine: Routine) -> dict[str, Any]:
        try:
            return agent.routines.suggest(routine).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=403, detail={"code": "POLICY_DENIED", "message": str(exc)}) from exc

    @app.post("/api/routines/{routine_id}/confirm")
    async def confirm_routine(routine_id: str) -> dict[str, Any]:
        try:
            return agent.routines.confirm(routine_id).model_dump()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"}) from exc

    @app.delete("/api/routines/{routine_id}")
    async def delete_routine(routine_id: str) -> dict[str, str]:
        agent.routines.delete(routine_id)
        return {"status": "deleted"}

    @app.post("/api/debug/event")
    async def debug_event(envelope: Envelope) -> dict[str, Any]:
        if envelope.type != MessageType.EVENT:
            raise HTTPException(status_code=422, detail={"code": "EXPECTED_EVENT"})
        response = await agent.handle_robot_message(envelope)
        await hub.broadcast({"kind": "debug_event", "payload": envelope.model_dump()})
        return {"response": response.model_dump() if response else None}

    @app.websocket("/ws/dashboard")
    async def ws_dashboard(websocket: WebSocket) -> None:
        await hub.connect(websocket)
        try:
            await websocket.send_json({"kind": "status", "payload": agent.status()})
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            hub.disconnect(websocket)

    @app.websocket("/ws/kt2")
    async def ws_kt2(websocket: WebSocket) -> None:
        await websocket.accept()
        robot_hub.connect(websocket)
        agent.state.agent_connected = True
        agent.state.link_state = "connected"
        agent.state.last_robot_seen_ms = now_ms()
        agent.log("robot_connected", {})
        await hub.broadcast({"kind": "robot_connected", "payload": agent.status()})
        event_queue = BoundedPriorityEventQueue(settings.event_queue_capacity)

        async def event_worker() -> None:
            while True:
                event = await event_queue.get()
                response = await agent.create_intent(event)
                if not await robot_hub.send_envelope(response):
                    return
                await hub.broadcast({"kind": "agent_tx", "payload": response.model_dump()})

        async def heartbeat_worker() -> None:
            while True:
                heartbeat = agent.host_heartbeat()
                if not await robot_hub.send_envelope(heartbeat):
                    return
                agent.refresh_link_state()
                await hub.broadcast({"kind": "heartbeat_tick", "payload": agent.status()})
                await asyncio.sleep(settings.host_heartbeat_interval_ms / 1000)

        workers = [asyncio.create_task(event_worker()), asyncio.create_task(heartbeat_worker())]
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    envelope = Envelope.model_validate(json.loads(raw))
                except (json.JSONDecodeError, ValidationError) as exc:
                    error = Envelope(
                        type=MessageType.ERROR,
                        priority=8,
                        payload={"code": "INVALID_PROTOCOL", "message": str(exc).splitlines()[0]},
                    )
                    await robot_hub.send_envelope(error)
                    continue
                agent.observe_robot_message(envelope)
                await hub.broadcast({"kind": "robot_rx", "payload": envelope.model_dump()})
                if envelope.type == MessageType.EVENT:
                    result = await event_queue.put(envelope)
                    if result.evicted is not None:
                        agent.log("event_evicted", {"event_id": result.evicted.id})
                        await robot_hub.send_envelope(
                            Envelope(
                                type=MessageType.ERROR,
                                priority=7,
                                session_id=result.evicted.session_id,
                                payload={
                                    "code": "EVENT_EVICTED",
                                    "message": "Host 大脑事件被更高优先级事件替换",
                                    "event_id": result.evicted.id,
                                },
                            )
                        )
                    if not result.accepted:
                        error = Envelope(
                            type=MessageType.ERROR,
                            priority=7,
                            session_id=envelope.session_id,
                            payload={
                                "code": "EVENT_QUEUE_FULL",
                                "message": "Host 大脑事件队列已满",
                                "event_id": envelope.id,
                            },
                        )
                        await robot_hub.send_envelope(error)
        except WebSocketDisconnect:
            pass
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            robot_hub.disconnect(websocket)
            agent.state.agent_connected = False
            agent.state.link_state = "disconnected"
            agent.state.last_robot_seen_ms = None
            agent.log("robot_disconnected", {})
            await hub.broadcast({"kind": "robot_disconnected", "payload": agent.status()})

    @app.websocket("/ws/media")
    async def ws_media(websocket: WebSocket) -> None:
        await media_service.handle_websocket(websocket)

    return app


app = create_app()
