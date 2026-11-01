import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr, ValidationError

from tsz_agent import lexicon_assistant as assistant
from tsz_agent.app import create_app
from tsz_agent.clients.tsz import TaskError
from tsz_agent.schemas import ModelCallMetrics
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


class FakeLedger:
    def __init__(self, fail_reserve=False):
        self.fail_reserve = fail_reserve
        self.reserved = []
        self.settled = []

    async def reserve_service_call(self, purpose, model, reserved_output):
        if self.fail_reserve:
            raise RuntimeError("database down")
        self.reserved.append((purpose, model, reserved_output))
        return uuid4()

    async def settle_service_call(self, call_id, input_tokens, output_tokens, metrics):
        self.settled.append((input_tokens, output_tokens, metrics))


def completion(message, finish="stop", usage=None):
    return {
        "id": "synthetic-completion",
        "object": "chat.completion",
        "created": 1,
        "model": "deepseek-flash",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": usage
        or {
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "total_tokens": 150,
            "prompt_cache_hit_tokens": 100,
            "prompt_cache_miss_tokens": 20,
        },
    }


def mock_provider(monkeypatch, respond):
    monkeypatch.setattr(
        assistant,
        "AsyncOpenAI",
        lambda **kwargs: AsyncOpenAI(
            **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        ),
    )


async def test_rust_tool_list_is_offered_and_enforced(settings, monkeypatch):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=completion(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "rules-1",
                            "type": "function",
                            "function": {"name": "read_filling_rules", "arguments": "{}"},
                        }
                    ],
                },
                "tool_calls",
            ),
        )

    mock_provider(monkeypatch, respond)
    config = settings.model_copy(update={"deepseek_api_key": SecretStr("synthetic-key")})
    body = assistant.AssistantStepInput.model_validate(
        {
            "messages": [{"role": "user", "content": "还缺什么？"}],
            "tools": ["search_lexicon", "find_entries", "read_lexicon", "lookup_dictionary"],
        }
    )
    with pytest.raises(assistant.AssistantUnavailable):
        await assistant.step(config, body)
    names = [tool["function"]["name"] for tool in requests[0]["tools"]]
    assert names == ["search_lexicon", "find_entries", "read_lexicon", "lookup_dictionary"]
    assert requests[0]["tools"][1]["function"]["parameters"]["required"] == ["spelling"]
    assert "lexicon-filling-v3" in requests[0]["messages"][0]["content"]
    with pytest.raises(ValidationError):
        assistant.AssistantStepInput.model_validate(
            {"messages": [{"role": "user", "content": "x"}], "tools": ["find_entries"] * 2}
        )


async def test_every_call_is_reserved_before_dispatch_and_settled_with_cache(settings, monkeypatch):
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(
            200, json=completion({"role": "assistant", "content": "按校验结果补名词原形。"})
        )

    mock_provider(monkeypatch, respond)
    config = settings.model_copy(update={"deepseek_api_key": SecretStr("synthetic-key")})
    body = assistant.AssistantStepInput.model_validate(
        {"messages": [{"role": "user", "content": "还缺什么？"}]}
    )
    ledger = FakeLedger()
    await assistant.step(config, body, ledger)
    assert ledger.reserved == [("assistant", config.deepseek_model, 1200)]
    input_tokens, output_tokens, metrics = ledger.settled[0]
    assert (input_tokens, output_tokens) == (120, 30)
    assert metrics.phase == "assistant" and metrics.outcome == "response"
    assert (metrics.cache_hit_tokens, metrics.cache_miss_tokens) == (100, 20)
    with pytest.raises(assistant.AssistantUnavailable):
        await assistant.step(config, body, FakeLedger(fail_reserve=True))
    assert len(sent) == 1


async def test_stream_failure_settles_as_unknown_error(settings, monkeypatch):
    def respond(request):
        return httpx.Response(500, json={"error": {"message": "upstream"}})

    mock_provider(monkeypatch, respond)
    config = settings.model_copy(update={"deepseek_api_key": SecretStr("synthetic-key")})
    body = assistant.AssistantStepInput.model_validate(
        {"messages": [{"role": "user", "content": "还缺什么？"}]}
    )
    ledger = FakeLedger()
    events = [json.loads(event) async for event in assistant.stream_step(config, body, ledger)]
    assert events == [{"event": "failed"}]
    assert ledger.settled[0][:2] == (None, None)
    assert ledger.settled[0][2].outcome == "error"


async def test_service_calls_enter_cost_totals_but_not_run_ledger(store, settings):
    call_id = await store.reserve_service_call("assistant", "deepseek-flash", 1200)
    await store.settle_service_call(
        call_id,
        120,
        30,
        ModelCallMetrics(
            phase="assistant",
            outcome="response",
            duration_ms=10,
            cache_hit_tokens=100,
            cache_miss_tokens=20,
        ),
    )
    unknown = await store.reserve_service_call("form_candidates", "deepseek-flash", 3500)
    overview = await store.model_cost_overview()
    assert overview.total.total_calls == 2
    assert overview.total.unknown_calls == 1
    ledger = await store.model_ledger(1, 20, None)
    assert ledger.total == 0 and ledger.items == []
    with pytest.raises(TaskError):
        await store.settle_service_call(
            call_id,
            1,
            1,
            ModelCallMetrics(phase="assistant", outcome="response", duration_ms=1),
        )
    assert unknown != call_id
    extended = await store.model_ledger(1, 20, None, service_calls=True)
    assert extended.total == 2 and all(item.run_id is None for item in extended.items)
    assert {item.purpose for item in extended.items} == {"assistant", "form_candidates"}
    filtered = await store.model_cost_ledger(1, 20, None, service_calls=True, purpose="assistant")
    assert filtered.ledger.total == 1 and filtered.cost_summary.priced_calls == 1
    overview = await store.model_cost_overview(service_calls=True)
    assert {item.purpose: item.total.total_calls for item in overview.by_purpose} == {
        "assistant": 1,
        "form_candidates": 1,
    }
    await store.ready()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app(settings, store)), base_url="http://agent.test"
    ) as client:
        headers = {"Authorization": "Bearer " + settings.api_token.get_secret_value()}
        legacy = (await client.get("/v1/model-costs", headers=headers)).json()
        assert legacy["ledger"]["items"] == []
        overview = (await client.get("/v1/model-costs/overview", headers=headers)).json()
        assert "by_purpose" not in overview
        headers["x-tsz-ledger-version"] = "2"
        page = (await client.get("/v1/model-costs?purpose=assistant", headers=headers)).json()
        assert page["ledger"]["total"] == 1
        assert page["ledger"]["items"][0]["run_id"] is None
        assert page["ledger"]["items"][0]["purpose"] == "assistant"


async def test_stream_close_preserves_already_observed_usage(settings, monkeypatch):
    class ProviderStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            chunk = {
                "id": "close-fixture",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "这段输出用于在用户主动停止之前交付已知用量。"},
                        "finish_reason": None,
                    }
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
            }
            yield ("data: " + json.dumps(chunk) + "\n\n").encode()
            await asyncio.Event().wait()

    mock_provider(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, stream=ProviderStream()
        ),
    )
    config = settings.model_copy(update={"deepseek_api_key": SecretStr("synthetic-key")})
    body = assistant.AssistantStepInput(
        messages=[assistant.AssistantMessage(role="user", content="检查")]
    )
    ledger = FakeLedger()
    events = assistant.stream_step(config, body, ledger)
    assert json.loads(await anext(events))["event"] == "delta"
    await events.aclose()
    assert ledger.settled[0][:2] == (50, 10)
    assert ledger.settled[0][2].outcome == "cancelled"


async def test_cancelled_nonstream_request_records_unknown_receipt(settings, monkeypatch):
    sent = asyncio.Event()

    async def respond(request):
        sent.set()
        await asyncio.Event().wait()

    mock_provider(monkeypatch, respond)
    config = settings.model_copy(update={"deepseek_api_key": SecretStr("synthetic-key")})
    ledger = FakeLedger()
    task = asyncio.create_task(
        assistant.step(
            config,
            assistant.AssistantStepInput(
                messages=[assistant.AssistantMessage(role="user", content="检查")]
            ),
            ledger,
        )
    )
    await sent.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ledger.settled[0][:2] == (None, None)
    assert ledger.settled[0][2].outcome == "cancelled"
