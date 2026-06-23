from __future__ import annotations

import asyncio
import json

import websockets

from .models import Envelope, MessageType, new_id, now_ms


class RobotSimulator:
    def __init__(self, url: str = "ws://127.0.0.1:8765/ws/kt2") -> None:
        self.url = url
        self.received: list[Envelope] = []

    async def send_event(self, name: str, battery_pct: int = 80) -> Envelope:
        event = Envelope(
            type=MessageType.EVENT,
            id=new_id("evt"),
            seq=1,
            ts_ms=now_ms(),
            session_id="simulator",
            payload={
                "name": name,
                "source": "simulator",
                "battery_pct": battery_pct,
                "posture": "upright",
            },
        )
        async with websockets.connect(self.url) as websocket:
            await websocket.send(json.dumps(event.model_dump(), ensure_ascii=False))
            response = Envelope.model_validate(json.loads(await websocket.recv()))
            self.received.append(response)
            if response.type == MessageType.INTENT:
                ack = Envelope(
                    type=MessageType.ACK,
                    seq=2,
                    session_id="simulator",
                    payload={"intent_id": response.id, "status": "accepted"},
                )
                await websocket.send(json.dumps(ack.model_dump(), ensure_ascii=False))
            return response


async def main() -> None:
    simulator = RobotSimulator()
    await simulator.send_event("touch_head")


if __name__ == "__main__":
    asyncio.run(main())
