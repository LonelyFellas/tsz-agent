import json

import httpx
import pytest
from pydantic import AnyHttpUrl, SecretStr

from tsz_agent.clients.tsz import TaskError
from tsz_agent.models.budget import MemoryBudget
from tsz_agent.models.opencode import BUDGET_PLUGIN, SESSION_PROTOCOL, OpenCodeModel, generate_text
from tsz_agent.schemas import ModelSelection, Usage

CHOICE = ModelSelection(source="opencode", provider_id="fixture", model_id="text")


@pytest.mark.parametrize("plugins", [[], [{"id": BUDGET_PLUGIN, "state": {"status": "failed"}}]])
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
async def test_remaining_output_budget_reaches_native_session_and_unknown_usage_is_held(
    settings, empty
):
    settings = settings.model_copy(
        update={
            "opencode_server_url": AnyHttpUrl("http://127.0.0.1:4096"),
            "opencode_server_password": SecretStr("fixture-" + "x" * 32),
        }
    )
    budget = MemoryBudget(Usage(model=CHOICE.usage_model()), 4, 3500)
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

    model = OpenCodeModel(settings, CHOICE, budget)
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
    assert caps == [3000, 500]
    assert len(generated) == len(deleted) == 2
    assert list(budget.pending.values()) == caps and not budget.receipts
    assert model.usage().requests == 2 and model.usage().cost is None
    assert [m.outcome for m in budget.metrics.values()] == ["response", "response"]
