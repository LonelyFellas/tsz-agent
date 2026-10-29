import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from test_chatgpt_auth import save_account
from test_validators import proposal

from tsz_agent.chatgpt_auth import PLAN_SCOPE, AuthStore
from tsz_agent.clients.tsz import TaskError
from tsz_agent.models import chatgpt, probes
from tsz_agent.models.budget import MemoryBudget
from tsz_agent.models.chatgpt import ChatGPTModel
from tsz_agent.schemas import CreateRun, ModelSelection, Usage


def terminal(text, kind="response.completed", usage=True, **changes):
    response = {
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        **changes,
    }
    if usage:
        response["usage"] = {
            "input_tokens": 100,
            "output_tokens": 20,
            "input_tokens_details": {"cached_tokens": 40},
        }
    return {"type": kind, "response": response}


def sse(event):
    return ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode()


class Chunks(httpx.AsyncByteStream):
    def __init__(self, content):
        self.content = content

    async def __aiter__(self):
        # Deliberately split Chinese UTF-8 and SSE line boundaries.
        for i in range(0, len(self.content), 7):
            yield self.content[i : i + 7]


async def setup_model(settings, tmp_path, respond):
    settings = settings.model_copy(update={"chatgpt_enabled": True, "chatgpt_auth_dir": tmp_path})
    auth = AuthStore(tmp_path)
    await save_account(auth)
    choice = ModelSelection(
        source="chatgpt", provider_id="oaiapp_fixture", model_id="fixture-model"
    )
    budget = MemoryBudget(Usage(model=choice.usage_model()), 6, 6000)
    model = ChatGPTModel(settings, choice, budget)
    await model.close()
    model.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    return model, budget


async def test_generate_review_and_real_receipts_use_only_authorized_scoped_references(
    settings, tmp_path, generation_data, pool
):
    requests = []

    async def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert str(request.url) == "https://api.openai.com/v1/responses"
        assert request.headers["authorization"] == "Bearer fixture-access"
        assert set(body) == {"model", "instructions", "input", "store", "stream"}
        assert (
            body["store"] is False and body["stream"] is True and body["model"] == "fixture-model"
        )
        value = (
            {"proposal": proposal(["c01", "c02", "c03"]).model_dump(mode="json")}
            if len(requests) == 1
            else {"verdict": "no_obvious_issue", "issues": []}
        )
        return httpx.Response(
            200, stream=Chunks(sse(terminal(json.dumps(value, ensure_ascii=False))))
        )

    model, budget = await setup_model(settings, tmp_path, respond)
    try:
        result = await model.generate(generation_data)
        assert [x.candidate_ref for x in result.distractors] == [x.candidate_ref for x in pool]
        assert (await model.review({})).verdict == "no_obvious_issue"
        assert model.usage().input_tokens == 200 and model.usage().output_tokens == 40
        assert model.usage().cost is None and not budget.pending
        assert [m.phase for m in budget.metrics.values()] == ["generate", "review"]
        assert all(
            m.cache_hit_tokens == 40 and m.cache_miss_tokens == 60 for m in budget.metrics.values()
        )
        assert str(pool[0].source.publication_id) not in json.dumps(requests)
    finally:
        await model.close()


@pytest.mark.parametrize(
    "failure,expected,receipt",
    [
        ("interrupted", "chatgpt_stream_interrupted", False),
        ("incomplete", "chatgpt_response_incomplete", True),
        ("limited", "chatgpt_usage_limit_exceeded", True),
        ("invalid", "model_output_invalid", True),
        ("empty", "model_response_empty", True),
        ("tool", "model_output_invalid", True),
        ("overspend", "model_budget_exceeded", True),
        ("http_limit", "chatgpt_usage_limit_exceeded", False),
        ("unauthorized", "chatgpt_login_required", False),
    ],
)
async def test_failures_preserve_known_usage_and_never_retry_or_fallback(
    settings, tmp_path, failure, expected, receipt
):
    calls = []

    def respond(request):
        calls.append(request)
        if failure == "http_limit":
            return httpx.Response(
                429,
                json={
                    "error": {
                        "code": "subscription_sharing_usage_limit_exceeded",
                        "message": "sensitive",
                    }
                },
            )
        if failure == "unauthorized":
            return httpx.Response(401, json={"detail": "sensitive"})
        event = terminal('{"verdict":"no_obvious_issue","issues":[]}')
        if failure == "interrupted":
            return httpx.Response(
                200,
                content=sse(
                    {"type": "response.output_text.delta", "delta": "untrusted partial output"}
                ),
            )
        if failure == "incomplete":
            event["type"] = "response.incomplete"
        if failure == "limited":
            event["type"] = "response.failed"
            event["response"]["error"] = {
                "code": "subscription_sharing_usage_limit_exceeded",
                "message": "sensitive",
            }
        if failure == "invalid":
            event = terminal("not json")
        if failure == "empty":
            event = terminal(" ")
        if failure == "tool":
            event["response"]["output"] = [
                {"type": "function_call", "name": "shell", "arguments": "do not execute"}
            ]
        if failure == "overspend":
            event["response"]["usage"]["output_tokens"] = 7000
        return httpx.Response(200, content=sse(event))

    model, budget = await setup_model(settings, tmp_path, respond)
    try:
        with pytest.raises(TaskError, match=expected) as raised:
            await model.review({})
        assert "sensitive" not in str(raised.value)
        assert len(calls) == 1 and model.usage().requests == 1
        assert bool(budget.receipts) == receipt and bool(budget.pending) != receipt
        assert model.usage().cost is None
        if failure == "overspend":
            assert model.usage().output_tokens == 7000
    finally:
        await model.close()


async def test_unknown_receipts_and_cancellation_hold_reservations(settings, tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                200,
                content=sse(terminal('{"verdict":"no_obvious_issue","issues":[]}', usage=False)),
            )
        entered.set()
        await release.wait()
        raise AssertionError("cancelled call must not complete")

    model, budget = await setup_model(settings, tmp_path, respond)
    try:
        await model.review({})
        task = asyncio.create_task(model.review({}))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(TaskError, match="model_budget_exceeded"):
            await model.review({})
        assert len(calls) == 2 and len(budget.pending) == 2 and not budget.receipts
        assert [m.outcome for m in budget.metrics.values()] == ["response", "cancelled"]
    finally:
        await model.close()


async def test_missing_plan_permission_stops_before_reserving_or_sending(settings, tmp_path):
    model, budget = await setup_model(settings, tmp_path, lambda _: pytest.fail("must not send"))
    async with model.auth.locked():
        data = model.auth.read()
        data.accounts["oaiapp_fixture"].scopes.remove(PLAN_SCOPE)
        model.auth.write(data)
    try:
        with pytest.raises(TaskError, match="chatgpt_plan_not_authorized"):
            await model.review({})
        assert not budget.pending and model.usage().requests == 0
    finally:
        await model.close()


async def test_catalog_is_account_specific_and_filters_hidden_models(tmp_path, monkeypatch):
    auth = AuthStore(tmp_path)
    await save_account(auth)
    original_client = httpx.AsyncClient

    def respond(request):
        assert str(request.url) == "https://api.openai.com/v1/models"
        assert request.headers["authorization"] == "Bearer fixture-access"
        return httpx.Response(
            200,
            json={
                "models": [
                    {"slug": "model-b", "display_name": "B", "visibility": "list"},
                    {"slug": "hidden", "display_name": "Hidden", "visibility": "hidden"},
                    {"slug": "model-a", "display_name": "A", "visibility": "list"},
                ]
            },
        )

    monkeypatch.setattr(
        chatgpt.httpx,
        "AsyncClient",
        lambda **_: original_client(transport=httpx.MockTransport(respond)),
    )
    assert await chatgpt.catalog(auth, "oaiapp_fixture") == [
        {"model_id": "model-b", "name": "B"},
        {"model_id": "model-a", "name": "A"},
    ]


async def test_expired_chatgpt_access_does_not_hide_other_platforms(
    settings, tmp_path, monkeypatch
):
    settings = settings.model_copy(update={"chatgpt_enabled": True, "chatgpt_auth_dir": tmp_path})
    auth = AuthStore(tmp_path)
    await save_account(auth)

    async def unavailable(*_):
        raise TaskError("chatgpt_login_required")

    monkeypatch.setattr(chatgpt, "catalog", unavailable)
    models, _ = await probes.catalog(settings)
    assert len(models) == 1 and models[0].source == "direct"


async def test_reauthorization_invalidates_probe_but_token_rotation_does_not(
    settings, tmp_path, monkeypatch
):
    settings = settings.model_copy(update={"chatgpt_enabled": True, "chatgpt_auth_dir": tmp_path})
    auth = AuthStore(tmp_path)
    await save_account(auth)

    async def catalog(*_):
        return [{"model_id": "fixture-model", "name": "Fixture"}]

    monkeypatch.setattr(chatgpt, "catalog", catalog)
    _, original = await probes.catalog(settings)
    async with auth.locked():
        data = auth.read()
        data.accounts["oaiapp_fixture"].access_token = SecretStr("new-access")
        auth.write(data)
    assert (await probes.catalog(settings))[1] == original
    await save_account(auth)
    assert (await probes.catalog(settings))[1] != original


async def test_slow_chatgpt_catalog_does_not_block_other_platforms(settings, tmp_path, monkeypatch):
    settings = settings.model_copy(update={"chatgpt_enabled": True, "chatgpt_auth_dir": tmp_path})
    await save_account(AuthStore(tmp_path))

    async def slow_catalog(*_):
        await asyncio.sleep(1)
        return []

    monkeypatch.setattr(probes, "CHATGPT_CATALOG_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(chatgpt, "catalog", slow_catalog)
    models, _ = await probes.catalog(settings)
    assert len(models) == 1 and models[0].source == "direct"


@pytest.mark.integration
async def test_api_requires_probe_and_pins_account_model_for_worker(
    store, settings, tmp_path, monkeypatch
):
    from tsz_agent import worker
    from tsz_agent.app import create_app
    from tsz_agent.clients.tsz import TszClient
    from tsz_agent.models.probes import ProbeService
    from tsz_agent.schemas import RunResult

    settings = settings.model_copy(update={"chatgpt_enabled": True, "chatgpt_auth_dir": tmp_path})
    auth = AuthStore(tmp_path)
    await save_account(auth)

    async def catalog(*_):
        return [{"model_id": "fixture-model", "name": "Fixture"}]

    monkeypatch.setattr(chatgpt, "catalog", catalog)
    choices, context = await probes.catalog(settings)
    choice = next(m for m in choices if m.source == "chatgpt")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app(settings, store)), base_url="http://agent.test"
    ) as http:
        headers = {"Authorization": "Bearer " + settings.api_token.get_secret_value()}
        body = {
            "source": choice.source,
            "provider_id": choice.provider_id,
            "model_id": choice.model_id,
        }
        assert (await http.put("/v1/model-selection", json=body)).status_code == 401
        assert (
            await http.put("/v1/model-selection", headers=headers, json=body)
        ).status_code == 422
        packet = {
            "context": context,
            "deadline": "2099-01-01T00:00:00+00:00",
            "report": {
                "batch_id": str(uuid4()),
                "state": "completed",
                "models": [
                    choice.model_copy(update={"status": "available"}).model_dump(mode="json")
                ],
            },
        }
        await store.start_probe(packet)
        assert (
            await http.put("/v1/model-selection", headers=headers, json=body)
        ).status_code == 200
    run = await store.create(CreateRun(external_request_id=uuid4()), uuid4())
    await store.set_model_selection(ModelSelection())
    await save_account(auth, "oaiapp_second")

    async def execute(run, store, tsz, get_model, save, **kwargs):
        model = get_model()
        assert isinstance(model, ChatGPTModel)
        assert (
            model.selection.provider_id == "oaiapp_fixture"
            and model.selection.model_id == "fixture-model"
        )
        await model.close()
        model.http = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, content=sse(terminal('{"verdict":"no_obvious_issue","issues":[]}'))
                )
            )
        )
        assert (await model.review({})).verdict == "no_obvious_issue"
        return RunResult(outcome="insufficient_candidates", reasons=["offline fixture"])

    monkeypatch.setattr(worker, "execute", execute)
    async with httpx.AsyncClient() as http:
        assert await worker.run_once(settings, store, TszClient(http))
    saved = await store.get(run.id)
    assert saved.state == "succeeded" and saved.usage.model == choice.usage_model()
    assert saved.usage.input_tokens == 100 and saved.usage.cost is None
    assert not (await ProbeService(settings, store).report()).models[-1].status == "available"
