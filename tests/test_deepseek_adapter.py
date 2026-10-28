import json

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.deepseek import DeepSeekProvider
from test_validators import proposal

from tsz_agent.clients.tsz import TaskError
from tsz_agent.models.deepseek import DeepSeekModel
from tsz_agent.schemas import Usage


async def test_real_sdk_serialization_typed_output_and_usage_without_network(settings):
    calls = []

    async def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        content = (
            {"proposal": proposal(["c2", "c3", "c4"]).model_dump(mode="json")}
            if len(calls) == 1
            else {"verdict": "needs_revision", "issues": ["题干缺少语境"]}
        )
        return httpx.Response(
            200,
            json={
                "id": "mock-completion",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": json.dumps(content)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 40, "completion_tokens": 60, "total_tokens": 100},
            },
        )

    config = settings.model_copy(update={"deepseek_api_key": SecretStr("test-key-no-network")})
    model = DeepSeekModel(config, Usage())
    assert model.provider.client.max_retries == 0
    await model.close()
    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    client = AsyncOpenAI(
        api_key="test-key-no-network", base_url="https://deepseek.test", http_client=http
    )
    model.provider = DeepSeekProvider(openai_client=client)
    model.model = OpenAIChatModel(
        "deepseek-flash", provider=model.provider, profile=model.model.profile
    )
    try:
        generated = await model.generate({"candidate_pool": ["c2", "c3", "c4"]})
        reviewed = await model.review({"original_prompt": "开始"})
        assert len(generated.distractors) == 3
        assert reviewed.verdict == "needs_revision"
        assert model.usage().requests == 2
        assert model.usage().input_tokens == 80
        assert model.usage().output_tokens == 120
        assert model.usage().cost is None
        assert [item.phase for item in model.budget.metrics.values()] == ["generate", "review"]
        assert all(c["model"] == "deepseek-flash" for c in calls)
        assert all(c["thinking"]["type"] == "disabled" for c in calls)
        assert all("reasoning_effort" not in c for c in calls)
    finally:
        await model.close()


async def test_transport_error_is_retryable_without_hidden_sdk_retries(settings):
    calls = []

    async def unavailable(request):
        calls.append(request.url.path)
        raise httpx.ConnectError("synthetic connection failure", request=request)

    config = settings.model_copy(update={"deepseek_api_key": SecretStr("test-key-no-network")})
    model = DeepSeekModel(config, Usage())
    await model.close()
    client = AsyncOpenAI(
        api_key="test-key-no-network",
        base_url="https://deepseek.test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(unavailable)),
    )
    model.provider = DeepSeekProvider(openai_client=client)
    model.model = OpenAIChatModel(
        "deepseek-flash", provider=model.provider, profile=model.model.profile
    )
    try:
        with pytest.raises(TaskError) as caught:
            await model.generate({"candidate_pool": []})
        assert caught.value.code == "model_unavailable"
        assert caught.value.retryable
        assert len(calls) == 1
    finally:
        await model.close()
