import json
from uuid import uuid4

import httpx
import pytest
from pydantic import AnyHttpUrl, SecretStr
from test_validators import proposal

from tsz_agent.app import create_app
from tsz_agent.clients.tsz import TaskError
from tsz_agent.models.budget import MemoryBudget
from tsz_agent.models.opencode import TOOL_PERMISSIONS, OpenCodeModel
from tsz_agent.schemas import CreateRun, ModelSelection, Usage
from tsz_agent.storage import Store


async def test_gateway_uses_selected_model_scoped_refs_and_unknown_usage(
    settings, generation_data, pool
):
    settings = settings.model_copy(
        update={
            "opencode_server_url": AnyHttpUrl("http://127.0.0.1:4096"),
            "opencode_server_password": SecretStr("test-opencode-" + "x" * 32),
        }
    )
    choice = ModelSelection(source="opencode", provider_id="anthropic", model_id="claude-test")
    budget = MemoryBudget(Usage(model=choice.usage_model()), 4, 6000)
    calls = []

    async def respond(request):
        if request.url.path == "/api/info":
            return httpx.Response(200, json={"version": "2.1.4"})
        if request.method == "DELETE":
            return httpx.Response(204)
        body = json.loads(request.content)
        if request.url.path == "/api/session":
            assert body["model"] == {
                "providerID": "anthropic",
                "id": "claude-test",
                "variant": "default",
            }
            assert body["permissions"] == TOOL_PERMISSIONS
            return httpx.Response(200, json={"data": {"id": "ses_fixture", **body}})
        calls.append(body)
        assert request.url.path == "/api/session/ses_fixture/generate"
        output = (
            {"proposal": proposal(["c01", "c02", "c03"]).model_dump(mode="json")}
            if len(calls) == 1
            else {"verdict": "no_obvious_issue", "issues": []}
        )
        return httpx.Response(200, json={"data": {"text": json.dumps(output)}})

    model = OpenCodeModel(settings, choice, budget)
    await model.close()
    model.http = httpx.AsyncClient(
        base_url="http://127.0.0.1:4096", transport=httpx.MockTransport(respond)
    )
    try:
        result = await model.generate(generation_data)
        assert [c.candidate_ref for c in result.distractors] == [c.candidate_ref for c in pool]
        assert (await model.review({})).verdict == "no_obvious_issue"
        assert model.usage().requests == 2 and model.usage().cost is None
        assert len(budget.pending) == 2 and not budget.receipts
        assert [m.phase for m in budget.metrics.values()] == ["generate", "review"]
        outbound = json.dumps(calls)
        assert str(pool[0].source.publication_id) not in outbound
        with pytest.raises(TaskError, match="model_budget_exceeded"):
            await model.review({})
        assert len(calls) == 2
    finally:
        await model.close()


@pytest.mark.integration
async def test_selection_persists_and_only_affects_new_jobs(store, settings):
    old_body, key = CreateRun(external_request_id=uuid4()), uuid4()
    old = await store.create(old_body, key)
    selection = ModelSelection(source="opencode", provider_id="provider", model_id="model/one")
    await store.set_model_selection(selection)
    fresh = Store(settings.database_url.get_secret_value())
    try:
        assert await fresh.model_selection() == selection
        assert (
            (await fresh.create(old_body, key)).usage.model == old.usage.model == "deepseek-flash"
        )
        new = await fresh.create(CreateRun(external_request_id=uuid4()), uuid4())
        assert new.usage.model == "opencode/provider/model/one"
        await fresh.set_model_selection(ModelSelection())
        assert (await fresh.get(new.id)).usage.model == new.usage.model
    finally:
        await fresh.close()


@pytest.mark.integration
async def test_selection_api_auth_validation_and_no_silent_fallback(store, settings, monkeypatch):
    async def unavailable(*args):
        raise TaskError("model_unavailable")

    monkeypatch.setattr(OpenCodeModel, "validate_selection", unavailable)
    app = create_app(settings, store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://agent.test"
    ) as http:
        assert (await http.get("/v1/model-selection")).status_code == 401
        headers = {"Authorization": "Bearer " + settings.api_token.get_secret_value()}
        selection = {"source": "opencode", "provider_id": "test", "model_id": "removed"}
        assert (
            await http.put("/v1/model-selection", headers=headers, json=selection)
        ).status_code == 422
        assert (await http.get("/v1/model-selection", headers=headers)).json()["source"] == "direct"
        assert (
            await http.put(
                "/v1/model-selection",
                headers=headers,
                json={**selection, "api_key": "must-not-be-stored"},
            )
        ).status_code == 422


@pytest.mark.integration
async def test_worker_dispatches_the_pinned_model_after_global_switch(store, settings, monkeypatch):
    from pydantic import AnyHttpUrl

    from tsz_agent import worker
    from tsz_agent.clients.tsz import TszClient
    from tsz_agent.models import opencode
    from tsz_agent.schemas import RunResult

    choice = ModelSelection(source="opencode", provider_id="paid-provider", model_id="model-a")
    await store.set_model_selection(choice)
    run = await store.create(CreateRun(external_request_id=uuid4()), uuid4())
    await store.set_model_selection(ModelSelection())
    settings = settings.model_copy(
        update={
            "opencode_server_url": AnyHttpUrl("http://127.0.0.1:4096"),
            "opencode_server_password": SecretStr("test-opencode-" + "x" * 32),
        }
    )
    calls, clients = [], []

    async def respond(request):
        if request.url.path == "/api/info":
            return httpx.Response(200, json={"version": "2.1.4"})
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.url.path == "/api/session":
            body = json.loads(request.content)
            calls.append(body["model"])
            return httpx.Response(200, json={"data": {"id": "ses_fixture", **body}})
        assert request.url.path == "/api/session/ses_fixture/generate"
        return httpx.Response(
            200, json={"data": {"text": json.dumps({"verdict": "no_obvious_issue", "issues": []})}}
        )

    def make_client(_):
        http = httpx.AsyncClient(
            base_url="http://127.0.0.1:4096", transport=httpx.MockTransport(respond)
        )
        clients.append(http)
        return http

    async def execute(run, store, tsz, get_model, save, **kwargs):
        assert (await get_model().review({})).verdict == "no_obvious_issue"
        return RunResult(outcome="insufficient_candidates", reasons=["isolated fixture"])

    monkeypatch.setattr(opencode, "client", make_client)
    monkeypatch.setattr(worker, "execute", execute)
    async with httpx.AsyncClient() as http:
        assert await worker.run_once(settings, store, TszClient(http))
    assert calls == [{"providerID": "paid-provider", "id": "model-a", "variant": "default"}]
    assert all(http.is_closed for http in clients)
    result = await store.get(run.id)
    assert result.state == "succeeded" and result.usage.model == choice.usage_model()
    ledger = await store.model_ledger(1, 10, run.id)
    assert ledger.items[0].input_tokens is None
    assert ledger.items[0].output_tokens is None


@pytest.mark.integration
@pytest.mark.parametrize("newer_receipt", [False, True])
async def test_unsupported_version_revokes_old_receipt_but_preserves_new_probe(
    store, settings, monkeypatch, newer_receipt
):
    import asyncio
    from datetime import UTC, datetime

    from tsz_agent import worker
    from tsz_agent.clients.tsz import TszClient
    from tsz_agent.models import opencode, probes

    settings = settings.model_copy(
        update={
            "opencode_server_url": AnyHttpUrl("http://127.0.0.1:4096"),
            "opencode_server_password": SecretStr("test-opencode-" + "x" * 32),
        }
    )
    choice = ModelSelection(source="opencode", provider_id="fixture", model_id="text")
    candidate = probes.ModelProbe(**choice.model_dump(), name="Fixture", enabled=True)

    async def catalog(_):
        # An upgrade need not change the model directory, connection, or saved configuration.
        return [candidate], "unchanged-context"

    async def check(_, selected):
        return selected.model_copy(update={"status": "available", "checked_at": datetime.now(UTC)})

    monkeypatch.setattr(probes, "catalog", catalog)
    monkeypatch.setattr(probes, "probe_one", check)
    service = probes.ProbeService(settings, store)
    await service.start()
    await asyncio.gather(*service.tasks)
    context = await service.require_available(choice)
    await store.set_model_selection(choice, probe_context=context)
    run = await store.create(CreateRun(external_request_id=uuid4()), uuid4())
    calls = []

    async def respond(request):
        calls.append((request.method, request.url.path))
        assert calls == [("GET", "/api/info")]
        if newer_receipt:
            # A delayed rejection from the old server arrives after a fresh successful probe.
            await service.start()
            await asyncio.gather(*service.tasks)
        return httpx.Response(200, json={"version": "2.2.0"})

    monkeypatch.setattr(
        opencode,
        "client",
        lambda _: httpx.AsyncClient(
            base_url="http://opencode.test", transport=httpx.MockTransport(respond)
        ),
    )

    async def execute(run, store, tsz, get_model, save, **kwargs):
        await get_model().review({})
        pytest.fail("unsupported OpenCode version must fail before generation")

    monkeypatch.setattr(worker, "execute", execute)
    async with httpx.AsyncClient() as http:
        assert await worker.run_once(settings, store, TszClient(http))
    failed = await store.get(run.id)
    assert failed.state == "failed" and failed.error_code == "opencode_version_unsupported"
    assert calls == [("GET", "/api/info")]
    result = (await service.report()).models[0]
    if newer_receipt:
        assert result.status == "available" and result.error is None
        assert await service.require_available(choice) == context
    else:
        assert result.status == "unavailable" and result.error == "runtime_failed"
        with pytest.raises(TaskError, match="model_not_verified"):
            await service.require_available(choice)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(create_app(settings, store)), base_url="http://agent.test"
        ) as http:
            response = await http.put(
                "/v1/model-selection",
                headers={"Authorization": "Bearer " + settings.api_token.get_secret_value()},
                json=choice.model_dump(),
            )
        assert response.status_code == 422 and response.json()["code"] == "model_not_verified"
    assert await store.model_selection() == choice
