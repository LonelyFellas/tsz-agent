import asyncio
from uuid import uuid4

import httpx
import pytest
from test_review_regressions import completion, mocked_model

from tsz_agent.app import create_app
from tsz_agent.clients.tsz import TaskError
from tsz_agent.models.budget import MemoryBudget, StoreBudget
from tsz_agent.schemas import CreateRun, ModelCallMetrics, Usage


@pytest.mark.parametrize(
    ("cache", "expected"),
    [
        ({"prompt_cache_hit_tokens": 30, "prompt_cache_miss_tokens": 20}, (30, 20)),
        ({"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 50}, (0, 50)),
        ({}, (None, None)),
        ({"prompt_cache_hit_tokens": 30}, (None, None)),
        ({"prompt_cache_hit_tokens": -1, "prompt_cache_miss_tokens": 51}, (None, None)),
        ({"prompt_cache_hit_tokens": 30, "prompt_cache_miss_tokens": 50}, (None, None)),
        ({"prompt_cache_hit_tokens": True, "prompt_cache_miss_tokens": 49}, (None, None)),
    ],
)
async def test_provider_cache_presence_and_exact_request_duration(
    settings, monkeypatch, cache, expected
):
    clock = iter([10.0, 10.125])
    monkeypatch.setattr("tsz_agent.models.budget.perf_counter", lambda: next(clock))

    async def respond(request):
        body = completion().json()
        body["usage"].update(cache)
        return httpx.Response(200, json=body)

    budget = MemoryBudget(Usage(), 4, 6000)
    model = await mocked_model(settings, respond, budget=budget)
    try:
        await model.review({"original_prompt": "开始"})
        metrics = next(iter(budget.metrics.values()))
        assert (metrics.cache_hit_tokens, metrics.cache_miss_tokens) == expected
        assert metrics.duration_ms == 125
        assert metrics.phase == "review" and metrics.outcome == "response"
        assert model.usage().input_tokens == 50
    finally:
        await model.close()


async def test_repair_retry_has_separate_metrics(settings, monkeypatch):
    clock = iter([10.0, 10.125, 11.0, 11.25])
    monkeypatch.setattr("tsz_agent.models.budget.perf_counter", lambda: next(clock))
    attempts = 0

    async def respond(request):
        nonlocal attempts
        attempts += 1
        body = completion({"invalid": True} if attempts == 1 else None).json()
        body["usage"].update(
            prompt_cache_hit_tokens=10 * attempts, prompt_cache_miss_tokens=50 - 10 * attempts
        )
        return httpx.Response(200, json=body)

    budget = MemoryBudget(Usage(), 4, 6000)
    model = await mocked_model(settings, respond, budget=budget)
    try:
        await model.review({"original_prompt": "开始"})
        observations = list(budget.metrics.values())
        assert [item.duration_ms for item in observations] == [125, 250]
        assert [item.cache_hit_tokens for item in observations] == [10, 20]
        assert model.usage().requests == 2 and model.usage().input_tokens == 100
    finally:
        await model.close()


@pytest.mark.parametrize("outcome", ["error", "cancelled", "missing_usage"])
async def test_incomplete_calls_keep_budget_and_record_time(settings, monkeypatch, outcome):
    clock = iter([10.0, 10.5])
    monkeypatch.setattr("tsz_agent.models.budget.perf_counter", lambda: next(clock))

    async def respond(request):
        if outcome == "error":
            raise httpx.ReadTimeout("synthetic", request=request)
        if outcome == "cancelled":
            raise asyncio.CancelledError
        body = completion().json()
        del body["usage"]
        return httpx.Response(200, json=body)

    budget = MemoryBudget(Usage(), 4, 3000)
    model = await mocked_model(settings, respond, budget=budget)
    try:
        if outcome == "missing_usage":
            with pytest.warns(match="Some responses omitted usage information"):
                await model.review({"original_prompt": "开始"})
        else:
            with pytest.raises(asyncio.CancelledError if outcome == "cancelled" else TaskError):
                await model.review({"original_prompt": "开始"})
        metrics = next(iter(budget.metrics.values()))
        assert metrics.duration_ms == 500
        assert metrics.outcome == ("response" if outcome == "missing_usage" else outcome)
        assert metrics.cache_hit_tokens is None and metrics.cache_miss_tokens is None
        assert budget.usage().requests == 1 and budget.usage().cost is None
        with pytest.raises(TaskError, match="model_budget_exceeded"):
            await model.review({"original_prompt": "开始"})
        assert len(budget.metrics) == 1
    finally:
        await model.close()


@pytest.mark.integration
async def test_metrics_api_weighted_rate_unknowns_auth_and_idempotence(store, settings):
    run = await store.create(CreateRun(external_request_id=uuid4()), uuid4())
    lease = await store.claim(60, 3)
    path = f"/v1/runs/{run.id}/model-metrics"
    headers = {"Authorization": "Bearer " + settings.api_token.get_secret_value()}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app(settings, store)), base_url="http://agent.test"
    ) as client:
        assert (await client.get(path)).status_code == 401
        assert (
            await client.get(f"/v1/runs/{uuid4()}/model-metrics", headers=headers)
        ).status_code == 404
        empty = (await client.get(path, headers=headers)).json()
        assert empty["reserved_calls"] == 0 and empty["calls"] == []
        assert empty["cache_hit_rate"] is None and empty["total_duration_ms"] is None
        ids = [uuid4() for _ in range(4)]
        for call_id in ids:
            await store.reserve_model_call(run.id, lease.lease_token, call_id, 1000, 4, 6000)
        observations = [
            ModelCallMetrics(
                phase="generate",
                outcome="response",
                duration_ms=100,
                cache_hit_tokens=20,
                cache_miss_tokens=30,
            ),
            ModelCallMetrics(
                phase="review",
                outcome="response",
                duration_ms=200,
                cache_hit_tokens=160,
                cache_miss_tokens=40,
            ),
            ModelCallMetrics(phase="review", outcome="error", duration_ms=600),
        ]
        # Metrics must survive a cancelled job and duplicate late delivery.
        await store.cancel(run.id)
        for call_id, metrics in zip(ids, observations, strict=False):
            await store.record_model_metrics(run.id, call_id, metrics)
            await store.record_model_metrics(run.id, call_id, metrics)
        with pytest.raises(TaskError, match="usage_receipt_conflict"):
            await store.record_model_metrics(run.id, ids[0], observations[1])
        with pytest.raises(TaskError, match="usage_receipt_invalid"):
            await store.record_model_metrics(uuid4(), ids[0], observations[0])
        result = (await client.get(path, headers=headers)).json()
        assert result["reserved_calls"] == 4 and result["observed_calls"] == 3
        assert result["cache_observed_calls"] == 2
        assert result["cache_hit_tokens"] == 180 and result["cache_miss_tokens"] == 70
        assert result["cache_hit_rate"] == 0.72
        assert result["total_duration_ms"] == 900 and result["average_duration_ms"] == 300
        assert sum(call["metrics"] is None for call in result["calls"]) == 1
        # The existing strict run response retains its original usage shape.
        usage = (await client.get(f"/v1/runs/{run.id}", headers=headers)).json()["usage"]
        assert set(usage) == {"model", "requests", "input_tokens", "output_tokens", "cost"}


@pytest.mark.integration
async def test_worker_budget_persists_metrics_after_cancellation(store, settings, monkeypatch):
    clock = iter([10.0, 10.125])
    monkeypatch.setattr("tsz_agent.models.budget.perf_counter", lambda: next(clock))
    run = await store.create(CreateRun(external_request_id=uuid4()), uuid4())
    lease = await store.claim(60, 3)
    budget = StoreBudget(store, run.id, lease.lease_token, Usage(), 4, 6000)

    async def respond(request):
        await store.cancel(run.id)
        body = completion().json()
        body["usage"].update(prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=50)
        return httpx.Response(200, json=body)

    model = await mocked_model(settings, respond, budget=budget)
    try:
        await model.review({"original_prompt": "开始"})
        result = await store.model_metrics(run.id)
        assert result.cache_hit_rate == 0 and result.average_duration_ms == 125
        assert (await store.get(run.id)).usage.input_tokens == 50
    finally:
        await model.close()


@pytest.mark.integration
async def test_unobserved_and_zero_input_calls_have_no_hit_rate(store):
    run = await store.create(CreateRun(external_request_id=uuid4()), uuid4())
    lease = await store.claim(60, 3)
    call_id = uuid4()
    await store.reserve_model_call(run.id, lease.lease_token, call_id, 1000, 4, 6000)
    unobserved = await store.model_metrics(run.id)
    assert unobserved.reserved_calls == 1 and unobserved.observed_calls == 0
    assert unobserved.cache_hit_tokens is None and unobserved.cache_hit_rate is None
    assert unobserved.average_duration_ms is None
    await store.record_model_metrics(
        run.id,
        call_id,
        ModelCallMetrics(
            phase="review",
            outcome="response",
            duration_ms=0,
            cache_hit_tokens=0,
            cache_miss_tokens=0,
        ),
    )
    measured = await store.model_metrics(run.id)
    assert measured.observed_calls == 1 and measured.cache_observed_calls == 1
    assert measured.cache_hit_tokens == 0 and measured.cache_miss_tokens == 0
    assert measured.cache_hit_rate is None and measured.average_duration_ms == 0
