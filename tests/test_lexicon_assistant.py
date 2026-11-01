import asyncio
import json

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr, ValidationError

from tsz_agent import lexicon_assistant as assistant
from tsz_agent.app import create_app
from tsz_agent.storage import Store


async def test_provider_stream_is_forwarded_before_completion_and_reports_usage(
    settings, monkeypatch
):
    finish = asyncio.Event()
    requests = []

    def chunk(delta, reason=None, usage=None):
        value = {
            "id": "stream-fixture",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "deepseek-flash",
            "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
        }
        if usage is not None:
            value["usage"] = usage
        return ("data: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode()

    class ProviderStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield chunk({"content": "这是一段在模型完成之前就应该展示给用户的建议。"})
            await finish.wait()
            yield chunk({"content": "请核对资料来源。"})
            yield chunk(
                {}, "stop", {"prompt_tokens": 50, "completion_tokens": 25, "total_tokens": 75}
            )
            yield b"data: [DONE]\n\n"

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, stream=ProviderStream()
        )

    monkeypatch.setattr(
        assistant,
        "AsyncOpenAI",
        lambda **kwargs: AsyncOpenAI(
            **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        ),
    )
    config = settings.model_copy(update={"deepseek_api_key": SecretStr("synthetic-stream-key")})
    events = assistant.stream_step(
        config,
        assistant.AssistantStepInput.model_validate(
            {"messages": [{"role": "user", "content": "如何填写？"}]}
        ),
    )
    first = json.loads(await anext(events))
    assert first["event"] == "delta" and "模型完成之前" in first["text"]
    assert not finish.is_set()
    finish.set()
    remaining = [json.loads(event) async for event in events]
    assert remaining[-1]["event"] == "complete"
    receipt = remaining[-1]["step"]
    assert receipt["input_tokens"] == 50 and receipt["output_tokens"] == 25
    assert receipt["content"] == first["text"] + remaining[0]["text"]
    assert requests[0]["stream"] is True and requests[0]["stream_options"] == {
        "include_usage": True
    }


async def test_tool_round_trip_uses_real_sdk_and_bounds_output(settings, monkeypatch):
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.headers["authorization"] == "Bearer synthetic-assistant-key"
        message = (
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "lookup-1",
                        "type": "function",
                        "function": {"name": "lookup_dictionary", "arguments": '{"q":"bank"}'},
                    }
                ],
            }
            if len(requests) == 1
            else {"role": "assistant", "content": "根据内置词典，建议区分两个义项。"}
        )
        return httpx.Response(
            200,
            json={
                "id": "synthetic-completion",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if len(requests) == 1 else "stop",
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
            },
        )

    def client(**kwargs):
        assert kwargs["max_retries"] == 0
        return AsyncOpenAI(
            **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        )

    monkeypatch.setattr(assistant, "AsyncOpenAI", client)
    config = settings.model_copy(update={"deepseek_api_key": SecretStr("synthetic-assistant-key")})
    body = assistant.AssistantStepInput.model_validate(
        {"messages": [{"role": "user", "content": "我还没保存：金融机构。查 bank 的词义。"}]}
    )
    first = await assistant.step(config, body)
    assert first.tool_calls[0].name == "lookup_dictionary"
    assert first.input_tokens == 120 and first.output_tokens == 30
    body.messages.extend(
        [
            assistant.AssistantMessage(role="assistant", tool_calls=first.tool_calls),
            assistant.AssistantMessage(role="tool", content="银行；河岸", tool_call_id="lookup-1"),
        ]
    )
    body.allow_tools = False
    second = await assistant.step(config, body)
    assert "两个义项" in second.content
    assert requests[1]["messages"][-1] == {
        "role": "tool",
        "content": "银行；河岸",
        "tool_call_id": "lookup-1",
    }
    assert requests[1]["tool_choice"] == "none"
    assert all(request["max_tokens"] == 1200 for request in requests)
    names = {tool["function"]["name"] for tool in requests[0]["tools"]}
    assert names == {"search_lexicon", "read_lexicon", "lookup_dictionary", "read_filling_rules"}
    assert "我还没保存" in requests[0]["messages"][1]["content"]


async def test_endpoint_requires_service_auth_rejects_injected_roles_and_reports_missing_model(
    settings,
):
    settings = settings.model_copy(update={"deepseek_api_key": None, "chatgpt_enabled": False})
    store = Store(settings.database_url.get_secret_value())
    app = create_app(settings, store)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://agent.test"
            ) as client,
        ):
            body = {"messages": [{"role": "user", "content": "解释词义"}]}
            path = "/v1/lexicon-assistant/step"
            assert (await client.post(path, json=body)).status_code == 401
            headers = {"Authorization": "Bearer " + settings.api_token.get_secret_value()}
            response = await client.get("/v1/lexicon-assistant/capability", headers=headers)
            assert response.json() == {"available": False}
            response = await client.post(path, json=body, headers=headers)
            assert response.status_code == 503
            assert response.json()["code"] == "model_not_configured"
            body["messages"][0]["role"] = "system"
            assert (await client.post(path, json=body, headers=headers)).status_code == 422
    finally:
        await store.close()


@pytest.mark.parametrize("name", ["save_lexicon", "execute_sql", "publish", "fetch_url"])
def test_unknown_tools_are_rejected(name):
    with pytest.raises(ValidationError):
        assistant.AssistantToolCall.model_validate({"id": "1", "name": name, "arguments": "{}"})


def test_context_limit_and_tool_roles_are_enforced():
    with pytest.raises(ValidationError):
        assistant.AssistantStepInput.model_validate(
            {"messages": [{"role": "user", "content": "x" * 50001}]}
        )
    with pytest.raises(ValidationError):
        assistant.AssistantMessage(role="tool", content="unbound tool data")
