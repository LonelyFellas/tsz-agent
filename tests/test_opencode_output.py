import json

import httpx
import pytest
from pydantic import AnyHttpUrl, SecretStr

from tsz_agent.clients.tsz import TaskError
from tsz_agent.models.budget import MemoryBudget
from tsz_agent.models.opencode import BUDGET_PLUGIN, SESSION_PROTOCOL, OpenCodeModel, generate_text
from tsz_agent.schemas import ModelSelection, Usage

CHOICE = ModelSelection(source="opencode", provider_id="fixture", model_id="text")


@pytest.mark.parametrize(
    "plugins",
    [
        [],
        [{"id": BUDGET_PLUGIN, "state": {"status": "failed"}}],
        [{"id": "tsz.agent.session-budget-v1", "state": {"status": "active"}}],
    ],
)
async def test_missing_budget_plugin_stops_before_session_or_inference(plugins):
    calls = []

    async def respond(request):
        calls.append(request.url.path)
        if request.url.path == "/api/info":
            return httpx.Response(200, json={"version": "2.1.4"})
        assert request.url.path == "/api/plugin"
        return httpx.Response(200, json={"data": plugins})

    async with httpx.AsyncClient(
        base_url="http://fixture.test", transport=httpx.MockTransport(respond)
    ) as http:
        with pytest.raises(TaskError, match="opencode_budget_not_configured"):
            await generate_text(http, CHOICE, "private input")
    assert calls == ["/api/info", "/api/plugin"]


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize(
    "choice,total,expected_caps",
    [
        (CHOICE, 3500, [3000, 500]),
        (
            ModelSelection(source="opencode", provider_id="opencode", model_id="flint-40b-free"),
            65536,
            [32768, 32768],
        ),
        (
            ModelSelection(source="opencode", provider_id="opencode", model_id="flint-40b-free"),
            32891,
            [32768, 123],
        ),
    ],
    ids=["other", "flint", "flint-remaining"],
)
async def test_remaining_output_budget_reaches_native_session_and_unknown_usage_is_held(
    settings, empty, choice, total, expected_caps
):
    settings = settings.model_copy(
        update={
            "opencode_server_url": AnyHttpUrl("http://127.0.0.1:4096"),
            "opencode_server_password": SecretStr("fixture-" + "x" * 32),
        }
    )
    budget = MemoryBudget(Usage(model=choice.usage_model()), 4, total)
    caps, deleted, generated = [], [], []

    async def respond(request):
        if request.url.path == "/api/info":
            return httpx.Response(200, json={"version": "2.1.4"})
        if request.url.path == "/api/plugin":
            return httpx.Response(
                200, json={"data": [{"id": BUDGET_PLUGIN, "state": {"status": "active"}}]}
            )
        if request.method == "DELETE":
            deleted.append(request.url.path)
            return httpx.Response(204)
        body = json.loads(request.content)
        if request.url.path == "/api/session":
            assert body["metadata"]["tszAgent"]["protocol"] == SESSION_PROTOCOL
            caps.append(body["metadata"]["tszAgent"]["maxOutputTokens"])
            return httpx.Response(200, json={"data": {"id": f"ses_budget{len(caps)}", **body}})
        generated.append(request.url.path)
        text = " \n\t" if empty else '{"verdict":"no_obvious_issue","issues":[]}'
        return httpx.Response(200, json={"data": {"text": text}})

    model = OpenCodeModel(settings, choice, budget)
    await model.close()
    model.http = httpx.AsyncClient(
        base_url="http://fixture.test", transport=httpx.MockTransport(respond)
    )
    try:
        for _ in range(2):
            if empty:
                with pytest.raises(TaskError, match="model_response_empty") as error:
                    await model.review({})
                assert error.value.retryable is False
            else:
                assert (await model.review({})).verdict == "no_obvious_issue"
        with pytest.raises(TaskError, match="model_budget_exceeded"):
            await model.review({})
    finally:
        await model.close()
    assert caps == expected_caps
    assert len(generated) == len(deleted) == 2
    assert list(budget.pending.values()) == caps and not budget.receipts
    assert model.usage().requests == 2 and model.usage().cost is None
    assert [m.outcome for m in budget.metrics.values()] == ["response", "response"]


@pytest.mark.parametrize(
    "provider,model_id,expected_timeout",
    [
        ("opencode", "flint-40b-free", 900),
        ("fixture", "flint-40b-free", 37),
        ("opencode", "flint-40b", 37),
    ],
)
async def test_extended_request_timeout_is_only_for_the_exact_free_flint(
    settings, provider, model_id, expected_timeout
):
    settings = settings.model_copy(
        update={
            "opencode_server_url": AnyHttpUrl("http://127.0.0.1:4096"),
            "opencode_server_password": SecretStr("fixture-" + "x" * 32),
            "request_timeout_seconds": 37,
        }
    )
    choice = ModelSelection(source="opencode", provider_id=provider, model_id=model_id)
    model = OpenCodeModel(
        settings, choice, MemoryBudget(Usage(model=choice.usage_model()), 4, 65536)
    )
    try:
        assert model.http.timeout.read == expected_timeout
    finally:
        await model.close()


@pytest.mark.parametrize(
    "provider,model_id,cap",
    [
        ("opencode", "flint-40b-free", 32769),
        ("other", "flint-40b-free", 32768),
        ("opencode", "flint-40b", 3001),
    ],
)
async def test_oversized_allowance_is_rejected_before_creating_a_session(provider, model_id, cap):
    calls = []

    async def respond(request):
        calls.append(request.url.path)
        if request.url.path == "/api/info":
            return httpx.Response(200, json={"version": "2.1.4"})
        assert request.url.path == "/api/plugin"
        return httpx.Response(
            200, json={"data": [{"id": BUDGET_PLUGIN, "state": {"status": "active"}}]}
        )

    choice = ModelSelection(source="opencode", provider_id=provider, model_id=model_id)
    async with httpx.AsyncClient(
        base_url="http://fixture.test", transport=httpx.MockTransport(respond)
    ) as http:
        with pytest.raises(TaskError, match="model_budget_exceeded"):
            await generate_text(http, choice, "private fixture", max_output_tokens=cap)
    assert calls == ["/api/info", "/api/plugin"]
