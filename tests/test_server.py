import asyncio
import sys
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.status import HTTP_403_FORBIDDEN
from starlette.websockets import WebSocketDisconnect

from naobot.agent import NaobotAgent
from naobot.llm import RuleBasedLLMClient
from naobot.media.backends import (
    OpenAICompatibleASR,
    OpenAICompatibleTTS,
    OpenAICompatibleVisionProvider,
)
from naobot.media.service import (
    LocalVisionSummaryProvider,
    MediaService,
    NullASRProvider,
    NullTTSProvider,
)
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


class FakeMediaService:
    def __init__(self) -> None:
        self.people = [{"person_id": "person-1", "display_name": "阿一"}]
        self.reset_calls = []
        self.delete_calls = []
        self.cancelled = False

    def status(self) -> dict:
        return {
            "connections": 1,
            "fps": 9.5,
            "queue": 2,
            "dropped": 0,
            "listening": True,
            "speaking": False,
            "current_person": "person-1",
            "current_session": "person-1",
            "session_trigger": "touch",
            "enrollment": {"state": "idle"},
            "provider_status": {"status": "degraded"},
        }

    async def list_people(self):
        return self.people

    async def reset_person_runtime(self, person_id: str):
        self.reset_calls.append(person_id)

    async def delete_person(self, person_id: str):
        self.delete_calls.append(person_id)
        self.people = [item for item in self.people if item["person_id"] != person_id]

    async def cancel_enrollment(self):
        self.cancelled = True

    async def route_touch_event(self, *, name: str, person_id: str | None = None) -> bool:
        return False

    async def list_sessions(self):
        return [{"session_id": "person-1", "person_id": "person-1"}]


def receive_type(websocket, message_type: str, max_messages: int = 10):
    for _ in range(max_messages):
        message = websocket.receive_json()
        if message["type"] == message_type:
            return message
    raise AssertionError(f"未在 {max_messages} 条消息内收到 {message_type}")


def make_client(tmp_path, *, media_service=None) -> TestClient:
    settings = Settings(runtime_dir=tmp_path)
    agent = NaobotAgent(settings, llm=RuleBasedLLMClient())
    return TestClient(create_app(settings, agent, media_service=media_service))


def test_health_and_status(tmp_path) -> None:
    client = make_client(tmp_path, media_service=FakeMediaService())
    assert client.get("/health").json() == {"status": "ok"}
    status = client.get("/api/status").json()
    assert status["robot"]["battery_pct"] is None
    assert status["llm_configured"] is False
    assert status["media"]["current_person"] == "person-1"
    assert status["media"]["session_trigger"] == "touch"


def test_dashboard_restores_full_workbench_navigation(tmp_path) -> None:
    client = make_client(tmp_path, media_service=FakeMediaService())

    html = client.get("/").text

    for page in (
        "home",
        "actions",
        "agent",
        "soul",
        "memory",
        "routines",
        "people",
        "diagnostics",
    ):
        assert f'id="{page}"' in html
        assert f'data-page="{page}"' in html

    assert 'id="stopButton"' in html
    assert "'/api/stop'" in html
    assert '<link rel="icon" href="data:,">' in html


def test_dashboard_contains_complete_home_and_diagnostics_observability(tmp_path) -> None:
    client = make_client(tmp_path, media_service=FakeMediaService())

    html = client.get("/").text

    for element_id in (
        "mode",
        "link",
        "robotOnline",
        "heartbeatAge",
        "lastHeartbeat",
        "battery",
        "llm",
        "brainRuntime",
        "brainMode",
        "routeMode",
        "routeReasons",
        "brainTeam",
        "currentPerson",
        "currentSession",
        "sessionTrigger",
        "runtimeLoaded",
        "authority",
        "reflex",
        "motion",
        "lastReflex",
        "hostMediaFps",
        "hostAudioQueue",
        "hostMediaDropped",
        "temporalSummary",
        "cameraFps",
        "audioState",
        "firmwareMediaQueue",
        "firmwareMediaDropped",
        "psramFree",
        "providerHealth",
        "logs",
        "statusDump",
    ):
        assert f'id="{element_id}"' in html
    assert "robot.battery_pct == null ? '-'" in html


@pytest.mark.parametrize(
    ("power_payload", "expected_source", "expected_available"),
    (
        (
            {
                "battery_pct": None,
                "soc_precise": False,
                "source": "ina226_voltage_fallback",
                "pack_voltage_mv": 14100,
                "cell_voltage_mv": 3525,
                "current_ma": -85,
                "power_mw": -1199,
                "charging": True,
                "series_count": 4,
                "power_available": True,
                "power_fault": False,
                "level": "normal",
            },
            "ina226_voltage_fallback",
            True,
        ),
        (
            {
                "battery_pct": None,
                "soc_precise": False,
                "source": "none",
                "pack_voltage_mv": None,
                "cell_voltage_mv": None,
                "current_ma": None,
                "power_mw": None,
                "charging": None,
                "series_count": 4,
                "power_available": False,
                "power_fault": "power_devices_unavailable",
                "level": "unknown",
            },
            "none",
            False,
        ),
    ),
)
def test_robot_heartbeat_nullable_power_reaches_status_api(
    tmp_path, power_payload, expected_source, expected_available
) -> None:
    client = make_client(tmp_path, media_service=FakeMediaService())
    heartbeat = Envelope(type=MessageType.HEARTBEAT, seq=7, payload=power_payload)

    with client.websocket_connect("/ws/kt2") as websocket:
        assert websocket.receive_json()["type"] == "heartbeat"
        websocket.send_json(heartbeat.model_dump())
        robot = client.get("/api/status").json()["robot"]

    assert robot["battery_pct"] is None
    assert robot["soc_precise"] is False
    assert robot["power_source"] == expected_source
    assert robot["power_available"] is expected_available
    assert robot["pack_voltage_mv"] == power_payload["pack_voltage_mv"]
    assert robot["power_fault"] == power_payload["power_fault"]


def test_dashboard_preserves_management_controls(tmp_path) -> None:
    client = make_client(tmp_path, media_service=FakeMediaService())

    html = client.get("/").text

    for endpoint in (
        "/api/actions/test",
        "/api/debug/event",
        "/api/soul",
        "/api/memory/suggest",
        "/api/routines",
        "/api/people",
        "/api/people/enrollment/cancel",
    ):
        assert endpoint in html

    for element_id in (
        "soulName",
        "userCall",
        "liveliness",
        "memoryText",
        "memoryList",
        "routineList",
        "peopleList",
        "cancelEnrollmentButton",
    ):
        assert f'id="{element_id}"' in html

    assert "window.confirm" in html


def test_dashboard_uses_safe_dom_and_recovers_buttons_after_api_failures(tmp_path) -> None:
    client = make_client(tmp_path, media_service=FakeMediaService())

    html = client.get("/").text

    assert "innerHTML" not in html
    assert "outerHTML" not in html
    assert "document.createElement" in html
    assert ".textContent" in html
    assert "encodeURIComponent" in html
    assert "sessionStorage" in html
    assert "finally" in html
    assert ".disabled" in html
    assert 'id="operationStatus"' in html


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


def test_robot_websocket_requires_device_token_when_configured(tmp_path) -> None:
    settings = Settings(runtime_dir=tmp_path, device_token="secret-token")
    agent = NaobotAgent(settings, llm=RuleBasedLLMClient())
    client = TestClient(create_app(settings, agent, media_service=FakeMediaService()))

    with pytest.raises(WebSocketDisconnect) as missing:
        with client.websocket_connect("/ws/kt2"):
            pass
    assert missing.value.code == 1008

    with pytest.raises(WebSocketDisconnect) as wrong:
        with client.websocket_connect(
            "/ws/kt2", headers={"X-Naobot-Token": "wrong-token"}
        ):
            pass
    assert wrong.value.code == 1008

    with client.websocket_connect(
        "/ws/kt2", headers={"X-Naobot-Token": "secret-token"}
    ) as websocket:
        assert websocket.receive_json()["type"] == "heartbeat"


def test_second_robot_control_connection_is_rejected_without_replacing_first(tmp_path) -> None:
    settings = Settings(
        runtime_dir=tmp_path,
        device_token="secret-token",
        host_heartbeat_interval_ms=40,
    )
    agent = NaobotAgent(settings, llm=RuleBasedLLMClient())
    client = TestClient(create_app(settings, agent, media_service=FakeMediaService()))
    headers = {"X-Naobot-Token": "secret-token"}

    with client.websocket_connect("/ws/kt2", headers=headers) as first:
        assert first.receive_json()["type"] == "heartbeat"
        with pytest.raises(WebSocketDisconnect) as duplicate:
            with client.websocket_connect("/ws/kt2", headers=headers) as second:
                second.receive_json()
        assert duplicate.value.code == 1013
        assert first.receive_json()["type"] == "heartbeat"


def test_dashboard_websocket_requires_token_when_configured(tmp_path) -> None:
    settings = Settings(runtime_dir=tmp_path, device_token="secret-token")
    agent = NaobotAgent(settings, llm=RuleBasedLLMClient())
    client = TestClient(
        create_app(settings, agent, media_service=FakeMediaService()),
        client=("192.0.2.1", 50000),
    )

    with pytest.raises(WebSocketDisconnect) as missing:
        with client.websocket_connect("/ws/dashboard"):
            pass
    assert missing.value.code == 1008

    with client.websocket_connect("/ws/dashboard?token=secret-token") as dashboard:
        assert dashboard.receive_json()["kind"] == "status"


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


def test_people_management_apis_delegate_to_media_service(tmp_path) -> None:
    media_service = FakeMediaService()
    client = make_client(tmp_path, media_service=media_service)

    people = client.get("/api/people")
    reset = client.post("/api/people/person-1/runtime/reset")
    delete = client.delete("/api/people/person-1")
    cancel = client.post("/api/people/enrollment/cancel")

    assert people.status_code == 200
    assert people.json()[0]["person_id"] == "person-1"
    assert reset.status_code == 200
    assert delete.status_code == 200
    assert cancel.status_code == 200
    assert media_service.reset_calls == ["person-1"]
    assert media_service.delete_calls == ["person-1"]
    assert media_service.cancelled is True


@pytest.mark.parametrize(
    ("method", "path", "payload", "headers"),
    [
        ("GET", "/api/status", None, {"Authorization": "Bearer secret-token"}),
        (
            "POST",
            "/api/actions/test",
            {"name": "blink", "args": {}},
            {"X-Naobot-Token": "secret-token"},
        ),
        (
            "PUT",
            "/api/soul",
            {"name": "鉴权测试"},
            {"Authorization": "Bearer secret-token"},
        ),
        ("GET", "/api/memory", None, {"X-Naobot-Token": "secret-token"}),
        (
            "POST",
            "/api/debug/event",
            {
                "type": "event",
                "seq": 1,
                "session_id": "api-auth",
                "payload": {"name": "touch_head", "battery_pct": 80, "posture": "upright"},
            },
            {"Authorization": "Bearer secret-token"},
        ),
        ("GET", "/api/people", None, {"X-Naobot-Token": "secret-token"}),
    ],
    ids=["status", "action", "soul", "memory", "debug", "people"],
)
def test_management_api_requires_token_when_device_token_configured(
    tmp_path,
    method: str,
    path: str,
    payload: dict | None,
    headers: dict[str, str],
) -> None:
    settings = Settings(runtime_dir=tmp_path, device_token="secret-token")
    agent = NaobotAgent(settings, llm=RuleBasedLLMClient())
    media_service = FakeMediaService()
    client = TestClient(create_app(settings, agent, media_service=media_service))

    assert client.request(method, path, json=payload).status_code == HTTP_403_FORBIDDEN
    assert client.request(method, path, json=payload, headers=headers).status_code == 200


def test_management_api_without_device_token_only_allows_loopback(tmp_path) -> None:
    settings = Settings(runtime_dir=tmp_path)
    agent = NaobotAgent(settings, llm=RuleBasedLLMClient())
    app = create_app(settings, agent, media_service=FakeMediaService())

    assert TestClient(app).get("/api/status").status_code == 200
    remote_client = TestClient(app, client=("192.0.2.1", 50000))
    assert remote_client.get("/").status_code == 200
    assert remote_client.get("/api/status").status_code == HTTP_403_FORBIDDEN


def test_dashboard_token_field_exists_without_embedding_token_in_html(tmp_path) -> None:
    settings = Settings(runtime_dir=tmp_path, device_token="secret-token")
    agent = NaobotAgent(settings, llm=RuleBasedLLMClient())
    client = TestClient(create_app(settings, agent, media_service=FakeMediaService()))

    html = client.get("/").text

    assert 'type="password"' in html
    assert "secret-token" not in html
    assert "X-Naobot-Token" in html
    assert html.count("fetch(") == 1
    assert "fetch(url, { ...options, headers: authHeaders(options.headers) })" in html
    save_token_handler = html.split("byId('saveTokenButton').addEventListener", 1)[1].split(
        "byId('cancelEnrollmentButton').addEventListener", 1
    )[0]
    assert "sessionStorage.setItem" in save_token_handler
    assert "await refreshStatus();" in save_token_handler
    assert "localStorage" not in html
    assert "console." not in html


def test_create_app_default_media_service_assembles_configured_backends(tmp_path) -> None:
    settings = Settings(
        runtime_dir=tmp_path,
        asr_endpoint="https://asr.example.com/v1",
        asr_model="asr-1",
        tts_endpoint="https://tts.example.com/v1",
        tts_model="tts-1",
        vision_endpoint="https://vision.example.com/v1",
        vision_model="vision-1",
    )
    app = create_app(settings, NaobotAgent(settings, llm=RuleBasedLLMClient()))
    media_service = app.state.media_service

    assert isinstance(media_service, MediaService)
    assert isinstance(media_service.asr, OpenAICompatibleASR)
    assert isinstance(media_service.tts, OpenAICompatibleTTS)
    assert isinstance(media_service.vision, OpenAICompatibleVisionProvider)
    assert media_service.status()["provider_status"]["status"] == "degraded"


def test_create_app_default_media_service_uses_local_fallbacks_when_cloud_not_configured(tmp_path) -> None:
    settings = Settings(runtime_dir=tmp_path)
    app = create_app(settings, NaobotAgent(settings, llm=RuleBasedLLMClient()))
    media_service = app.state.media_service

    assert isinstance(media_service.asr, NullASRProvider)
    assert isinstance(media_service.tts, NullTTSProvider)
    assert isinstance(media_service.vision, LocalVisionSummaryProvider)
    assert media_service.status()["provider_status"]["status"] == "degraded"


def test_create_app_uses_local_asr_when_model_without_endpoint(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeLocalASR:
        async def transcribe(self, _frames):
            raise NotImplementedError

    monkeypatch.setattr("naobot.media.service.FasterWhisperASR", lambda model_name: FakeLocalASR())
    settings = Settings(runtime_dir=tmp_path, asr_model="base")
    app = create_app(settings, NaobotAgent(settings, llm=RuleBasedLLMClient()))

    assert app.state.media_service.asr.__class__.__name__ == "FakeLocalASR"


def test_sherpa_tts_uses_official_config_objects(monkeypatch, tmp_path) -> None:
    built = {}

    class Config:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def validate(self):
            return True

    class OfflineTts:
        def __init__(self, config):
            built["config"] = config

    fake_module = SimpleNamespace(
        OfflineTts=OfflineTts,
        OfflineTtsConfig=Config,
        OfflineTtsModelConfig=Config,
        OfflineTtsVitsModelConfig=Config,
    )
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake_module)
    settings = Settings(
        runtime_dir=tmp_path,
        sherpa_onnx_model_path="model.onnx",
        sherpa_onnx_tokens_path="tokens.txt",
        sherpa_onnx_lexicon_path="lexicon.txt",
        sherpa_onnx_rule_fsts="number.fst",
    )

    engine = MediaService._build_sherpa_engine(settings)

    assert isinstance(engine, OfflineTts)
    config = built["config"]
    assert config.kwargs["rule_fsts"] == "number.fst"
    vits = config.kwargs["model"].kwargs["vits"]
    assert vits.kwargs["model"] == "model.onnx"
    assert vits.kwargs["tokens"] == "tokens.txt"
