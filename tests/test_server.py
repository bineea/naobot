import asyncio
import threading

from fastapi.testclient import TestClient

from naobot.agent import NaobotAgent
from naobot.llm import RuleBasedLLMClient
from naobot.models import Envelope, LLMDecision, MessageType
from naobot.server import create_app
from naobot.settings import Settings


class SlowRuleBasedLLM(RuleBasedLLMClient):
    async def decide(self, event, soul, memories):
        await asyncio.sleep(0.15)
        return await super().decide(event, soul, memories)


class FirstCallGateLLM(RuleBasedLLMClient):
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.event_names = []

    async def decide(self, event, soul, memories):
        name = event.payload["name"]
        self.event_names.append(name)
        if len(self.event_names) == 1:
            self.started.set()
            await asyncio.to_thread(self.release.wait, 2)
        return LLMDecision(goal=name, text=name)


def receive_type(websocket, message_type: str, max_messages: int = 10):
    for _ in range(max_messages):
        message = websocket.receive_json()
        if message["type"] == message_type:
            return message
    raise AssertionError(f"未在 {max_messages} 条消息内收到 {message_type}")


def make_client(tmp_path) -> TestClient:
    settings = Settings(runtime_dir=tmp_path)
    agent = NaobotAgent(settings, llm=RuleBasedLLMClient())
    return TestClient(create_app(settings, agent))


def test_health_and_status(tmp_path) -> None:
    client = make_client(tmp_path)
    assert client.get("/health").json() == {"status": "ok"}
    status = client.get("/api/status").json()
    assert status["robot"]["battery_pct"] == 100
    assert status["llm_configured"] is False


def test_dashboard_contains_brain_runtime_observability(tmp_path) -> None:
    client = make_client(tmp_path)

    html = client.get("/").text

    assert 'id="brainRuntime"' in html
    assert 'id="brainMode"' in html
    assert 'id="brainTeam"' in html


def test_debug_event_touch_head(tmp_path) -> None:
    client = make_client(tmp_path)
    event = Envelope(
        type=MessageType.EVENT,
        seq=1,
        session_id="api",
        payload={"name": "touch_head", "battery_pct": 80, "posture": "upright"},
    )
    response = client.post("/api/debug/event", json=event.model_dump())
    assert response.status_code == 200
    assert response.json()["response"]["type"] == "intent"


def test_action_api_rejects_unsafe_action(tmp_path) -> None:
    client = make_client(tmp_path)
    response = client.post("/api/actions/test", json={"name": "flip", "args": {}})
    assert response.status_code == 403


def test_websocket_touch_head_to_intent(tmp_path) -> None:
    client = make_client(tmp_path)
    event = Envelope(
        type=MessageType.EVENT,
        seq=1,
        session_id="ws",
        payload={"name": "touch_head", "battery_pct": 80, "posture": "upright"},
    )
    with client.websocket_connect("/ws/kt2") as websocket:
        websocket.send_json(event.model_dump())
        response = receive_type(websocket, "intent")
        assert response["type"] == "intent"
        assert response["payload"]["actions"][0]["name"] == "set_expression"
        websocket.send_json(
            Envelope(
                type=MessageType.ACK,
                seq=2,
                session_id="ws",
                payload={"intent_id": response["id"], "status": "accepted"},
            ).model_dump()
        )


def test_stop_api_sends_intent_to_connected_robot(tmp_path) -> None:
    client = make_client(tmp_path)
    with client.websocket_connect("/ws/kt2") as websocket:
        response = client.post("/api/stop")
        assert response.status_code == 200
        assert response.json()["robot_sent"] is True
        intent = receive_type(websocket, "intent")
        assert intent["type"] == "intent"
        assert intent["payload"]["actions"][0]["name"] == "stop"


def test_robot_websocket_receives_host_heartbeat(tmp_path) -> None:
    client = make_client(tmp_path)
    with client.websocket_connect("/ws/kt2") as websocket:
        heartbeat = websocket.receive_json()
        assert heartbeat["type"] == "heartbeat"
        assert heartbeat["payload"]["source"] == "host"
        assert "host_ts_ms" in heartbeat["payload"]


def test_host_heartbeat_continues_while_brain_is_thinking(tmp_path) -> None:
    settings = Settings(runtime_dir=tmp_path, host_heartbeat_interval_ms=40)
    agent = NaobotAgent(settings, llm=SlowRuleBasedLLM())
    client = TestClient(create_app(settings, agent))
    event = Envelope(
        type=MessageType.EVENT,
        seq=1,
        session_id="slow",
        payload={"name": "touch_head", "battery_pct": 80, "posture": "upright"},
    )

    with client.websocket_connect("/ws/kt2") as websocket:
        first = websocket.receive_json()
        assert first["type"] == "heartbeat"
        websocket.send_json(event.model_dump())
        messages = []
        while len(messages) < 10 and not any(message["type"] == "intent" for message in messages):
            messages.append(websocket.receive_json())

    assert any(message["type"] == "heartbeat" for message in messages)
    assert any(message["type"] == "intent" for message in messages)
    intent_index = next(index for index, message in enumerate(messages) if message["type"] == "intent")
    assert any(message["type"] == "heartbeat" for message in messages[:intent_index])


def test_websocket_event_queue_rejects_low_priority_and_evicts_for_high_priority(tmp_path) -> None:
    llm = FirstCallGateLLM()
    settings = Settings(
        runtime_dir=tmp_path,
        event_queue_capacity=1,
        host_heartbeat_interval_ms=40,
    )
    agent = NaobotAgent(settings, llm=llm)
    client = TestClient(create_app(settings, agent))

    def queued_event(name: str, priority: int) -> dict:
        return Envelope(
            type=MessageType.EVENT,
            session_id="queue",
            priority=priority,
            payload={"name": name, "battery_pct": 80, "posture": "upright"},
        ).model_dump()

    with client.websocket_connect("/ws/kt2") as websocket:
        assert websocket.receive_json()["type"] == "heartbeat"
        websocket.send_json(queued_event("active", 5))
        assert llm.started.wait(1)

        websocket.send_json(queued_event("queued-low", 2))
        websocket.send_json(queued_event("rejected-lower", 1))
        error = receive_type(websocket, "error", max_messages=20)
        assert error["payload"]["code"] == "EVENT_QUEUE_FULL"

        websocket.send_json(queued_event("queued-high", 9))
        evicted_error = receive_type(websocket, "error", max_messages=20)
        assert evicted_error["payload"]["code"] == "EVENT_EVICTED"
        llm.release.set()
        intents = [receive_type(websocket, "intent", max_messages=30) for _ in range(2)]

    assert [intent["payload"]["goal"] for intent in intents] == ["active", "queued-high"]
    assert llm.event_names == ["active", "queued-high"]
    assert any(log["kind"] == "event_evicted" for log in agent.logs)
