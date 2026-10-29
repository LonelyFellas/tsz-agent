import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from pydantic import AnyHttpUrl, SecretStr
from sqlalchemy import select

from tsz_agent import worker
from tsz_agent.clients.tsz import TaskError, TszClient
from tsz_agent.models import opencode
from tsz_agent.schemas import CreateRun, ModelSelection, RunResult
from tsz_agent.storage import ModelCall


@pytest.mark.integration
@pytest.mark.parametrize(
    "provider,model_id,expected_caps,expected_job_timeout",
    [
        ("opencode", "flint-40b-free", [32768, 32768], 1800),
        ("other", "flint-40b-free", [3000, 2000], 45),
    ],
)
async def test_worker_uses_pinned_flint_budget_and_timeout_and_keeps_unknown_reservations(
    store, settings, monkeypatch, provider, model_id, expected_caps, expected_job_timeout
):
    choice = ModelSelection(source="opencode", provider_id=provider, model_id=model_id)
    await store.set_model_selection(choice)
    run = await store.create(CreateRun(external_request_id=uuid4()), uuid4())
    # Switching the global selection must not give the old task another model's budget.
    await store.set_model_selection(
        ModelSelection(
            source="opencode",
            provider_id="opencode" if provider == "other" else "other",
            model_id="flint-40b-free",
        )
    )
    settings = settings.model_copy(
        update={
            "opencode_server_url": AnyHttpUrl("http://127.0.0.1:4096"),
            "opencode_server_password": SecretStr("fixture-" + "x" * 32),
            "output_token_limit": 5000,
            "job_timeout_seconds": 45,
            "request_timeout_seconds": 37,
        }
    )
    caps, durations, timeouts = [], [], []

    async def respond(request):
        if request.url.path == "/api/info":
            return httpx.Response(200, json={"version": "2.1.4"})
        if request.url.path == "/api/plugin":
            return httpx.Response(
                200, json={"data": [{"id": opencode.BUDGET_PLUGIN, "state": {"status": "active"}}]}
            )
        if request.method == "DELETE":
            return httpx.Response(204)
        body = json.loads(request.content)
        if request.url.path == "/api/session":
            assert body["model"]["providerID"] == provider
            caps.append(body["metadata"]["tszAgent"]["maxOutputTokens"])
            return httpx.Response(200, json={"data": {"id": f"ses_budget{len(caps)}", **body}})
        timeouts.append(request.extensions["timeout"]["read"])
        return httpx.Response(
            200, json={"data": {"text": '{"verdict":"no_obvious_issue","issues":[]}'}}
        )

    def client(settings):
        return httpx.AsyncClient(
            base_url="http://fixture.test",
            transport=httpx.MockTransport(respond),
            timeout=settings.request_timeout_seconds,
        )

    original_timeout = asyncio.timeout

    def timeout(seconds):
        durations.append(seconds)
        return original_timeout(seconds)

    async def execute(run, store, tsz, get_model, save, **kwargs):
        model = get_model()
        for _ in range(2):
            assert (await model.review({})).verdict == "no_obvious_issue"
        with pytest.raises(TaskError, match="model_budget_exceeded"):
            await model.review({})
        return RunResult(outcome="insufficient_candidates", reasons=["offline fixture"])

    monkeypatch.setattr(opencode, "client", client)
    monkeypatch.setattr(worker, "execute", execute)
    monkeypatch.setattr(worker.asyncio, "timeout", timeout)
    async with httpx.AsyncClient() as http:
        assert await worker.run_once(settings, store, TszClient(http))
    result = await store.get(run.id)
    assert result.state == "succeeded" and result.usage.model == choice.usage_model()
    assert result.usage.requests == 2 and result.usage.cost is None
    assert caps == expected_caps
    assert expected_job_timeout in durations
    assert (45 if expected_job_timeout == 1800 else 1800) not in durations
    assert timeouts == [900 if provider == "opencode" else 37] * 2
    async with store.sessions() as session:
        calls = list(
            (
                await session.scalars(
                    select(ModelCall)
                    .where(ModelCall.run_id == run.id)
                    .order_by(ModelCall.created_at)
                )
            ).all()
        )
    assert [c.reserved_output_tokens for c in calls] == expected_caps
    assert all(
        c.input_tokens is None and c.output_tokens is None and c.recorded_at is None for c in calls
    )
