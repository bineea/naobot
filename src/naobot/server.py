from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from .agent import NaobotAgent
from .models import Action, Envelope, MessageType, Routine, SoulConfig, new_id, now_ms
from .policy import PolicyGuard
from .settings import Settings


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

    def connect(self, websocket: WebSocket) -> None:
        self.websocket = websocket

    def disconnect(self, websocket: WebSocket) -> None:
        if self.websocket is websocket:
            self.websocket = None

    async def send_envelope(self, envelope: Envelope) -> bool:
        if self.websocket is None:
            return False
        try:
            await self.websocket.send_json(envelope.model_dump())
        except RuntimeError:
            self.websocket = None
            return False
        return True

    async def send_intent(self, intent: Envelope) -> bool:
        return await self.send_envelope(intent)


def create_app(settings: Settings | None = None, agent: NaobotAgent | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    agent = agent or NaobotAgent(settings)
    hub = DashboardHub()
    robot_hub = RobotHub()
    app = FastAPI(title="naobot", version="0.1.0")
    app.state.agent = agent
    app.state.dashboard_hub = hub
    app.state.robot_hub = robot_hub

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        html_path = Path(__file__).with_name("web") / "index.html"
        return html_path.read_text(encoding="utf-8")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/status")
    async def api_status() -> dict[str, Any]:
        return agent.status()

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
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(websocket.receive_text(), timeout=2.0)
                except TimeoutError:
                    heartbeat = agent.host_heartbeat()
                    if not await robot_hub.send_envelope(heartbeat):
                        raise WebSocketDisconnect from None
                    agent.refresh_link_state()
                    await hub.broadcast({"kind": "heartbeat_tick", "payload": agent.status()})
                    continue
                try:
                    envelope = Envelope.model_validate(json.loads(raw))
                except (json.JSONDecodeError, ValidationError) as exc:
                    error = Envelope(
                        type=MessageType.ERROR,
                        priority=8,
                        payload={"code": "INVALID_PROTOCOL", "message": str(exc).splitlines()[0]},
                    )
                    await websocket.send_json(error.model_dump())
                    continue
                response = await agent.handle_robot_message(envelope)
                await hub.broadcast({"kind": "robot_rx", "payload": envelope.model_dump()})
                if response:
                    await websocket.send_json(response.model_dump())
                    await hub.broadcast({"kind": "agent_tx", "payload": response.model_dump()})
        except WebSocketDisconnect:
            robot_hub.disconnect(websocket)
            agent.state.agent_connected = False
            agent.state.link_state = "disconnected"
            agent.state.last_robot_seen_ms = None
            agent.log("robot_disconnected", {})
            await hub.broadcast({"kind": "robot_disconnected", "payload": agent.status()})

    return app


app = create_app()
