import httpx
import pytest

from naobot import llm
from naobot.llm import OpenAICompatibleLLMClient
from naobot.models import Envelope, MessageType, SoulConfig
from naobot.settings import Settings


class FailingAsyncClient:
    def __init__(self, timeout: int) -> None:
        self.timeout = timeout

    async def __aenter__(self) -> "FailingAsyncClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(self, *args, **kwargs):
        raise httpx.ConnectError("测试用连接失败")


@pytest.mark.asyncio
async def test_openai_compatible_client_falls_back_to_rules(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(llm.httpx, "AsyncClient", FailingAsyncClient)
    settings = Settings(
        runtime_dir=tmp_path,
        llm_base_url="http://127.0.0.1:1/v1",
        llm_api_key="test",
        llm_model="test-model",
    )
    event = Envelope(type=MessageType.EVENT, payload={"name": "touch_head"})

    decision = await OpenAICompatibleLLMClient(settings).decide(event, SoulConfig(), [])

    assert decision.actions[0].name == "set_face"
