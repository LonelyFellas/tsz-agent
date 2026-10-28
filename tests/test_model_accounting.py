import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text
from test_review_regressions import completion, mocked_model

from tsz_agent.clients.tsz import TaskError
from tsz_agent.models.budget import StoreBudget
from tsz_agent.schemas import CreateRun, RunResult, Usage
from tsz_agent.storage import ModelCall

pytestmark = pytest.mark.integration


async def claim(store):
    run = await store.create(CreateRun(external_request_id=uuid4()), uuid4())
    lease = await store.claim(60, 3)
    return run, lease.lease_token


async def test_cancel_retains_late_known_usage_and_receipt_is_idempotent(store, settings):
    run, token = await claim(store)

    async def respond(request):
        await store.cancel(run.id)
        return completion(output_tokens=100)

    budget = StoreBudget(store, run.id, token, Usage(), 4, 6000)
    model = await mocked_model(settings, respond, budget=budget)
    try:
        await model.review({"original_prompt": "开始"})
        assert not await store.complete(
            run.id, token, RunResult(outcome="insufficient_candidates", reasons=["不应写入终态"])
        )
        current = await store.get(run.id)
        assert current.state == "cancelled" and current.result is None
        assert current.usage.requests == 1
        assert current.usage.input_tokens == 50
        assert current.usage.output_tokens == 100
        async with store.sessions() as session:
            receipt = await session.scalar(select(ModelCall).where(ModelCall.run_id == run.id))
        await store.record_model_usage(run.id, receipt.id, 50, 100)
        assert (await store.get(run.id)).usage.output_tokens == 100
        with pytest.raises(TaskError, match="usage_receipt_conflict"):
            await store.record_model_usage(run.id, receipt.id, 50, 200)
    finally:
        await model.close()


async def test_unknown_usage_reserves_output_across_lease_transfer(store, settings):
    run, old_token = await claim(store)
    calls = []

    async def timeout(request):
        calls.append(json.loads(request.content)["max_tokens"])
        raise httpx.ReadTimeout("synthetic", request=request)

    old_budget = StoreBudget(store, run.id, old_token, Usage(), 4, 4000)
    old = await mocked_model(settings, timeout, budget=old_budget)
    try:
        with pytest.raises(TaskError, match="model_unavailable"):
            await old.review({"original_prompt": "开始"})
    finally:
        await old.close()
    async with store.sessions.begin() as session:
        await session.execute(text("UPDATE agent_runs SET lease_until=now()-interval '1 second'"))
    new_lease = await store.claim(60, 3)
    new_budget = StoreBudget(
        store, run.id, new_lease.lease_token, (await store.get(run.id)).usage, 4, 4000
    )
    new = await mocked_model(settings, timeout, budget=new_budget)
    try:
        with pytest.raises(TaskError, match="model_unavailable"):
            await new.review({"original_prompt": "开始"})
        with pytest.raises(TaskError, match="model_budget_exceeded"):
            await new.review({"original_prompt": "开始"})
        assert calls == [3000, 1000]
        current = await store.get(run.id)
        assert current.usage.requests == 2 and current.usage.output_tokens == 0
        assert current.usage.cost is None
        metrics = await store.model_metrics(run.id)
        assert metrics.observed_calls == 2 and metrics.cache_observed_calls == 0
        assert metrics.cache_hit_rate is None
        assert all(call.metrics.outcome == "error" for call in metrics.calls)
    finally:
        await new.close()


async def test_timeout_request_counter_persists_across_retries(store, settings):
    run, token = await claim(store)
    budget = StoreBudget(store, run.id, token, Usage(), 1, 6000)

    async def timeout(request):
        raise httpx.ReadTimeout("synthetic", request=request)

    model = await mocked_model(settings, timeout, budget=budget)
    try:
        with pytest.raises(TaskError):
            await model.review({"original_prompt": "开始"})
    finally:
        await model.close()
    await store.fail(run.id, token, "model_unavailable", True, 3)
    async with store.sessions.begin() as session:
        await session.execute(text("UPDATE agent_runs SET next_attempt_at=now()"))
    next_lease = await store.claim(60, 3)
    with pytest.raises(TaskError, match="model_budget_exceeded"):
        await store.reserve_model_call(run.id, next_lease.lease_token, uuid4(), 3000, 1, 6000)
    assert (await store.get(run.id)).usage.requests == 1


async def test_late_old_worker_receipt_survives_new_worker_checkpoint(store):
    run, old_token = await claim(store)
    old_call, new_call = uuid4(), uuid4()
    await store.reserve_model_call(run.id, old_token, old_call, 3000, 4, 6000)
    async with store.sessions.begin() as session:
        await session.execute(text("UPDATE agent_runs SET lease_until=now()-interval '1 second'"))
    new = await store.claim(60, 3)
    await store.reserve_model_call(run.id, new.lease_token, new_call, 3000, 4, 6000)
    await store.record_model_usage(run.id, old_call, 200, 100)
    assert await store.checkpoint(run.id, new.lease_token, "generated", {})
    await store.record_model_usage(run.id, new_call, 300, 200)
    assert await store.complete(
        run.id,
        new.lease_token,
        RunResult(outcome="insufficient_candidates", reasons=["无合适候选"]),
    )
    current = await store.get(run.id)
    assert current.usage.requests == 2
    assert current.usage.input_tokens == 500 and current.usage.output_tokens == 300


async def test_cancelled_job_cannot_reserve_new_call(store):
    run, token = await claim(store)
    await store.cancel(run.id)
    with pytest.raises(TaskError, match="lease_lost"):
        await store.reserve_model_call(run.id, token, uuid4(), 3000, 4, 6000)
    assert (await store.get(run.id)).usage.requests == 0


async def test_concurrent_reservations_cannot_exceed_one_remaining_request(store):
    run, token = await claim(store)
    outcomes = await asyncio.gather(
        store.reserve_model_call(run.id, token, uuid4(), 3000, 1, 6000),
        store.reserve_model_call(run.id, token, uuid4(), 3000, 1, 6000),
        return_exceptions=True,
    )
    assert sum(isinstance(outcome, tuple) for outcome in outcomes) == 1
    assert (
        sum(
            isinstance(outcome, TaskError) and outcome.code == "model_budget_exceeded"
            for outcome in outcomes
        )
        == 1
    )
    assert (await store.get(run.id)).usage.requests == 1
