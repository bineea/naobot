from __future__ import annotations

import asyncio
import json
from typing import Any

import click
import httpx
import uvicorn
import websockets
from rich.console import Console

from .models import Envelope, MessageType, new_id, now_ms
from .settings import Settings

console = Console()


@click.group()
def main() -> None:
    """naobot 开发工具。"""


@main.command()
@click.option("--host", default=None, help="监听地址")
@click.option("--port", default=None, type=int, help="监听端口")
def serve(host: str | None, port: int | None) -> None:
    """启动 FastAPI Dashboard 和 Agent。"""
    settings = Settings.from_env()
    uvicorn.run(
        "naobot.server:app",
        host=host or settings.host,
        port=port or settings.port,
        reload=False,
    )


@main.command()
@click.option("--url", default="ws://127.0.0.1:8765/ws/kt2", help="KT2 WebSocket 地址")
@click.option("--event", default="touch_head", help="要发送的事件名")
@click.option("--battery", default=78, type=int, help="模拟电量")
def simulate(url: str, event: str, battery: int) -> None:
    """启动一次性 CPython 机器人模拟器。"""
    asyncio.run(_simulate_once(url, event, battery))


async def _simulate_once(url: str, event: str, battery: int) -> None:
    envelope = Envelope(
        type=MessageType.EVENT,
        id=new_id("evt"),
        seq=1,
        ts_ms=now_ms(),
        session_id="simulator",
        payload={"name": event, "source": "simulator", "battery_pct": battery, "posture": "upright"},
    )
    async with websockets.connect(url) as websocket:
        await websocket.send(json.dumps(envelope.model_dump(), ensure_ascii=False))
        raw = await websocket.recv()
        console.print_json(raw)
        response = json.loads(raw)
        if response.get("type") == "intent":
            ack = Envelope(
                type=MessageType.ACK,
                seq=2,
                session_id="simulator",
                payload={"intent_id": response["id"], "status": "accepted"},
            )
            await websocket.send(json.dumps(ack.model_dump(), ensure_ascii=False))


@main.command("send-event")
@click.option("--url", default="http://127.0.0.1:8765/api/debug/event", help="debug event API")
@click.option("--event", default="touch_head", help="事件名")
def send_event(url: str, event: str) -> None:
    """通过 HTTP debug API 发送事件。"""
    envelope = Envelope(
        type=MessageType.EVENT,
        id=new_id("evt"),
        seq=1,
        session_id="cli",
        payload={"name": event, "source": "cli", "battery_pct": 80, "posture": "upright"},
    )
    response = httpx.post(url, json=envelope.model_dump(), timeout=10)
    response.raise_for_status()
    console.print_json(data=response.json())


@main.command("validate-config")
def validate_config() -> None:
    """验证环境变量和运行时配置。"""
    settings = Settings.from_env()
    data: dict[str, Any] = {
        "host": settings.host,
        "port": settings.port,
        "runtime_dir": str(settings.runtime_dir),
        "llm_configured": settings.llm_configured,
        "llm_base_url": settings.llm_base_url,
        "llm_model": settings.llm_model,
    }
    console.print_json(data=data)


if __name__ == "__main__":
    main()
