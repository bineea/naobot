from fastapi.testclient import TestClient

from naobot.agent import NaobotAgent
from naobot.llm import RuleBasedLLMClient
from naobot.models import Envelope, MessageType
from naobot.server import create_app
from naobot.settings import Settings


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
        response = websocket.receive_json()
        assert response["type"] == "intent"
        assert response["payload"]["actions"][0]["name"] == "set_face"
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
        intent = websocket.receive_json()
        assert intent["type"] == "intent"
        assert intent["payload"]["actions"][0]["name"] == "stop"
