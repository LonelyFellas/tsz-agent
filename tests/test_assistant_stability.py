"""Offline fault injection through the real OpenAI SDK; no model or database calls."""

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr
from test_form_candidates import payload, request
from test_lexicon_assistant import FakeLedger, completion

from tsz_agent import form_candidates as candidates
from tsz_agent import lexicon_assistant as assistant
from tsz_agent.app import create_app

TEXT = "这段建议会在供应商流结束之前先交付给消费者，请核对资料来源。"
USAGE = {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}
BODY = {"messages": [{"role": "user", "content": "检查 bank 的草稿"}]}
PATH = "/v1/lexicon-assistant/"
MODES = ["step", "step/stream", "form-candidates"]


@pytest.fixture
def configured(settings):
    return settings.model_copy(
        update={"deepseek_api_key": SecretStr("synthetic-stability-key"), "chatgpt_enabled": False}
    )


def frame(delta, finish=None, usage=None):
    value = {
        "id": "stability-stream",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "deepseek-flash",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage is not None:
        value["usage"] = usage
    return ("data: " + json.dumps(value) + "\n\n").encode()


class ProviderStream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, failure=None, stall=False):
        self.chunks = chunks
        self.failure = failure
        self.stall = stall
        self.waiting = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.stall:
            self.waiting.set()
            await asyncio.Event().wait()
        if self.failure is not None:
            raise self.failure

    async def aclose(self):
        self.closed = True


def streaming_response(stream):
    return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)


def install_provider(monkeypatch, respond):
    def client(**kwargs):
        assert kwargs["max_retries"] == 0
        return AsyncOpenAI(
            **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        )

    monkeypatch.setattr(assistant, "AsyncOpenAI", client)
    monkeypatch.setattr(candidates, "AsyncOpenAI", client)


@asynccontextmanager
async def api_client(configured, ledger):
    app = create_app(configured, ledger)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app, raise_app_exceptions=False),
            base_url="http://agent.test",
            headers={"Authorization": "Bearer " + configured.api_token.get_secret_value()},
        ) as client,
    ):
        yield client


def input_for(mode):
    return request().model_dump() if mode == "form-candidates" else BODY


def successful_response(mode):
    if mode == "step/stream":
        return streaming_response(
            ProviderStream([frame({"content": TEXT}), frame({}, "stop", USAGE)])
        )
    text = json.dumps(payload()) if mode == "form-candidates" else TEXT
    return httpx.Response(200, json=completion({"role": "assistant", "content": text}))


@pytest.mark.parametrize("failure", [httpx.ReadError, httpx.ReadTimeout])
async def test_interrupted_provider_stream_has_terminal_failed_event(
    configured, monkeypatch, failure
):
    stream = ProviderStream(
        [frame({"content": TEXT}, usage=USAGE)],
        failure=failure("synthetic-private-provider-failure"),
    )
    install_provider(monkeypatch, lambda req: streaming_response(stream))
    ledger = FakeLedger()
    async with api_client(configured, ledger) as client:
        response = await client.post(PATH + "step/stream", json=BODY)
    events = [json.loads(line) for line in response.text.splitlines()]
    assert response.status_code == 200
    assert stream.closed
    assert ledger.settled[0][:2] == (50, 10)
    assert ledger.settled[0][2].outcome == "error"
    assert "synthetic-private-provider-failure" not in response.text
    assert events[0] == {"event": "delta", "text": TEXT}
    assert events[-1] == {"event": "failed"}, events


@pytest.mark.parametrize("mode", MODES)
async def test_provider_http_429_fails_safely_without_retry(configured, monkeypatch, caplog, mode):
    sent = []

    def respond(req):
        sent.append(req)
        return httpx.Response(429, json={"error": {"message": "synthetic-private-provider-error"}})

    install_provider(monkeypatch, respond)
    ledger = FakeLedger()
    async with api_client(configured, ledger) as client:
        response = await client.post(PATH + mode, json=input_for(mode))
    assert len(sent) == 1
    assert ledger.settled[0][:2] == (None, None)
    assert ledger.settled[0][2].outcome == "error"
    if mode == "step/stream":
        assert response.status_code == 200
        assert response.json() == {"event": "failed"}
    else:
        assert response.status_code == 503
        assert response.json()["code"] in {"model_unavailable", "model_output_invalid"}
    assert "synthetic-private-provider-error" not in response.text + caplog.text


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("fault", ["reserve", "settle"])
async def test_ledger_exceptions_keep_reserve_closed_and_settle_deliverable(
    configured, monkeypatch, caplog, mode, fault
):
    class FaultLedger(FakeLedger):
        async def settle_service_call(self, *args):
            raise RuntimeError("synthetic-private-ledger-error")

    ledger = FakeLedger(fail_reserve=True) if fault == "reserve" else FaultLedger()
    sent = []

    def respond(req):
        sent.append(req)
        return successful_response(mode)

    install_provider(monkeypatch, respond)
    async with api_client(configured, ledger) as client:
        response = await client.post(PATH + mode, json=input_for(mode))
    if fault == "reserve":
        assert sent == []
        assert ledger.reserved == [] and ledger.settled == []
        if mode == "step/stream":
            assert response.json() == {"event": "failed"}
        else:
            assert response.status_code == 503
            assert response.json()["code"] == "ledger_unavailable"
    else:
        assert len(sent) == len(ledger.reserved) == 1
        assert ledger.settled == []
        assert response.status_code == 200
        if mode == "step/stream":
            assert json.loads(response.text.splitlines()[-1])["event"] == "complete"
        elif mode == "step":
            assert response.json()["content"] == TEXT
        else:
            assert response.json()["pos"] == payload()["pos"]
        assert "assistant_ledger_settle_failed" in caplog.text
    assert "synthetic-private-ledger-error" not in response.text + caplog.text


@pytest.mark.parametrize("arguments", ['{"q":', '["bank"]'])
async def test_invalid_tool_arguments_fail_without_complete(configured, monkeypatch, arguments):
    stream = ProviderStream(
        [
            frame(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "lookup-1",
                            "type": "function",
                            "function": {"name": "lookup_dictionary", "arguments": arguments},
                        }
                    ]
                }
            ),
            frame({}, "tool_calls", USAGE),
        ]
    )
    install_provider(monkeypatch, lambda req: streaming_response(stream))
    ledger = FakeLedger()
    async with api_client(configured, ledger) as client:
        response = await client.post(PATH + "step/stream", json=BODY)
    assert response.json() == {"event": "failed"}
    assert stream.closed
    assert ledger.settled[0][:2] == (50, 10)
    assert ledger.settled[0][2].outcome == "response"


@pytest.mark.parametrize("mode", MODES)
async def test_length_limited_output_cannot_complete(configured, monkeypatch, mode):
    if mode == "step/stream":
        upstream = streaming_response(
            ProviderStream([frame({"content": TEXT}), frame({}, "length", USAGE)])
        )
    else:
        text = json.dumps(payload()) if mode == "form-candidates" else TEXT
        upstream = httpx.Response(
            200, json=completion({"role": "assistant", "content": text}, finish="length")
        )
    install_provider(monkeypatch, lambda req: upstream)
    ledger = FakeLedger()
    async with api_client(configured, ledger) as client:
        response = await client.post(PATH + mode, json=input_for(mode))
    if mode == "step/stream":
        events = [json.loads(line) for line in response.text.splitlines()]
        assert events[-1] == {"event": "failed"}
        assert not any(event["event"] == "complete" for event in events)
    else:
        assert response.status_code == 503
        assert response.json()["code"] == "model_output_incomplete"
    assert len(ledger.settled) == 1
    assert ledger.settled[0][2].outcome == "response"


async def test_cancel_during_slow_provider_read_closes_and_settles(configured, monkeypatch):
    stream = ProviderStream([frame({"content": TEXT})], stall=True)
    install_provider(monkeypatch, lambda req: streaming_response(stream))
    ledger = FakeLedger()
    events = assistant.stream_step(
        configured, assistant.AssistantStepInput.model_validate(BODY), ledger
    )
    assert json.loads(await anext(events))["event"] == "delta"
    pending = asyncio.create_task(anext(events))
    await asyncio.wait_for(stream.waiting.wait(), timeout=1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pending, timeout=1)
    assert stream.closed
    assert len(ledger.settled) == 1
    assert ledger.settled[0][:2] == (None, None)
    assert ledger.settled[0][2].outcome == "cancelled"


async def test_asgi_consumer_disconnect_closes_provider_and_preserves_usage(
    configured, monkeypatch
):
    stream = ProviderStream([frame({"content": TEXT}, usage=USAGE)], stall=True)
    install_provider(monkeypatch, lambda req: streaming_response(stream))
    ledger = FakeLedger()
    app = create_app(configured, ledger)
    disconnected = asyncio.Event()
    received = False
    messages = []

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": json.dumps(BODY).encode(), "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.body" and b'"event": "delta"' in message["body"]:
            disconnected.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": PATH + "step/stream",
        "raw_path": (PATH + "step/stream").encode(),
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/json"),
            (b"authorization", ("Bearer " + configured.api_token.get_secret_value()).encode()),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("agent.test", 80),
    }
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(app(scope, receive, send), timeout=1)
    assert disconnected.is_set()
    assert stream.closed
    assert len(ledger.settled) == 1
    assert ledger.settled[0][:2] == (50, 10)
    assert ledger.settled[0][2].outcome == "cancelled"
    assert not any(b'"event": "complete"' in message.get("body", b"") for message in messages)


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("invalid_json", "json"),
        ("invalid_schema", "schema"),
        ("invalid_catalog", "catalog"),
        ("length", "incomplete"),
    ],
)
async def test_candidate_rejection_logs_safe_reason_and_request_identity(
    configured, monkeypatch, caplog, fault, reason
):
    value = payload()
    if fault == "invalid_catalog":
        value["pos"][0]["pos"] = "unconfigured"
    if fault == "invalid_schema":
        value["private-provider-field"] = "synthetic-private-model-output"
    content = '{"synthetic-private-model-output":' if fault == "invalid_json" else json.dumps(value)
    finish = "length" if fault == "length" else "stop"
    install_provider(
        monkeypatch,
        lambda req: httpx.Response(
            200,
            json=completion({"role": "assistant", "content": content}, finish=finish),
        ),
    )
    ledger = FakeLedger()
    caplog.set_level("INFO")
    async with api_client(configured, ledger) as client:
        response = await client.post(PATH + "form-candidates", json=input_for("form-candidates"))
    assert response.status_code == 503
    assert response.json()["code"] == (
        "model_output_incomplete" if fault == "length" else "model_output_invalid"
    )
    assert ledger.settled[0][:2] == (120, 30)
    assert ledger.settled[0][2].outcome == "response"
    record = next(
        item for item in caplog.records if item.getMessage().startswith("form_candidates_rejected")
    )
    assert f"reason={reason}" in record.getMessage()
    assert f"request_id={response.headers['x-request-id']}" in record.getMessage()
    assert "call_id=" in record.getMessage()
    assert "finish_reason=" in record.getMessage()
    assert "synthetic-private-model-output" not in caplog.text


@pytest.mark.parametrize("mode", ["step", "form-candidates"])
async def test_malformed_provider_message_gets_sanitized_500(configured, monkeypatch, mode):
    value = completion({"role": "assistant", "content": TEXT})
    value["choices"][0]["message"] = None
    install_provider(monkeypatch, lambda req: httpx.Response(200, json=value))
    ledger = FakeLedger()
    async with api_client(configured, ledger) as client:
        response = await client.post(PATH + mode, json=input_for(mode))
    assert ledger.settled[0][:2] == (120, 30)
    assert ledger.settled[0][2].outcome == "response"
    if mode == "form-candidates":
        assert response.status_code == 503
        assert response.json()["code"] == "model_output_invalid"
    else:
        assert response.status_code == 500
        assert response.json()["code"] == "internal_error"
    assert "AttributeError" not in response.text


async def test_malformed_stream_delta_still_has_terminal_failed_event(configured, monkeypatch):
    stream = ProviderStream([frame({"content": TEXT}), frame(None, "stop", USAGE)])
    install_provider(monkeypatch, lambda req: streaming_response(stream))
    ledger = FakeLedger()
    async with api_client(configured, ledger) as client:
        response = await client.post(PATH + "step/stream", json=BODY)
    events = [json.loads(line) for line in response.text.splitlines()]
    assert response.status_code == 200
    assert stream.closed
    assert ledger.settled[0][:2] == (50, 10)
    assert ledger.settled[0][2].outcome == "error"
    assert events[-1] == {"event": "failed"}, events


@pytest.mark.parametrize("mode", ["step", "step/stream"])
@pytest.mark.parametrize("arguments", ["{}", '{"q":123}', '{"q":"bank","save":true}'])
async def test_agent_accepts_object_arguments_beyond_advertised_schema(
    configured, monkeypatch, mode, arguments
):
    tool = {
        "id": "lookup-1",
        "type": "function",
        "function": {"name": "lookup_dictionary", "arguments": arguments},
    }
    if mode == "step/stream":
        upstream = streaming_response(
            ProviderStream(
                [frame({"tool_calls": [{"index": 0, **tool}]}), frame({}, "tool_calls", USAGE)]
            )
        )
    else:
        upstream = httpx.Response(
            200,
            json=completion({"role": "assistant", "tool_calls": [tool]}, finish="tool_calls"),
        )
    install_provider(monkeypatch, lambda req: upstream)
    ledger = FakeLedger()
    async with api_client(configured, ledger) as client:
        response = await client.post(PATH + mode, json=BODY)
    assert len(ledger.settled) == 1
    assert response.status_code == 200
    # Execution and permission checks belong to the downstream read-tool consumer.
    if mode == "step/stream":
        events = [json.loads(line) for line in response.text.splitlines()]
        assert events[-1]["event"] == "complete"
        result = events[-1]["step"]
    else:
        result = response.json()
    assert result["tool_calls"] == [
        {"id": "lookup-1", "name": "lookup_dictionary", "arguments": arguments}
    ]
