"""Check task ownership and scheduling under independent process clocks."""

from datetime import datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from tsz_agent import storage
from tsz_agent.clients.tsz import TaskError
from tsz_agent.schemas import CreateRun, RunResult
from tsz_agent.storage import ModelCall, Run

pytestmark = pytest.mark.integration


async def test_new_queue_item_is_claimable_when_api_clock_is_ahead(store, monkeypatch):
    async with store.sessions() as session:
        database_time = await session.scalar(select(func.clock_timestamp()))
    clock = Mock(wraps=datetime)
    clock.now.return_value = database_time + timedelta(seconds=5)
    monkeypatch.setattr(storage, "datetime", clock)
    run = await store.create(CreateRun(external_request_id=uuid4()), uuid4())

    claim = await store.claim(60, 3)

    assert claim is not None, "new queued task is hidden until the database clock catches up"
    assert claim.id == run.id


async def test_worker_behind_database_does_not_allow_immediate_lease_takeover(store, monkeypatch):
    run = await store.create(CreateRun(external_request_id=uuid4()), uuid4())
    async with store.sessions.begin() as session:
        await session.execute(update(Run).values(next_attempt_at=func.now()))
    async with store.sessions() as session:
        database_time = await session.scalar(select(func.clock_timestamp()))
    monkeypatch.setattr(storage, "now", lambda: database_time - timedelta(seconds=65))

    original = await store.claim(60, 3)
    assert original is not None
    takeover = await store.claim(60, 3)

    assert takeover is None, "a newly granted lease is already expired by the database clock"
    assert original.id == run.id


@pytest.mark.parametrize("operation", ["complete", "renew", "checkpoint", "fail", "reserve"])
async def test_expired_worker_cannot_write_with_a_slow_clock(store, monkeypatch, operation):
    run = await store.create(CreateRun(external_request_id=uuid4()), uuid4())
    owner = await store.claim(60, 3)
    assert owner is not None
    async with store.sessions() as session:
        database_time = await session.scalar(select(func.clock_timestamp()))
    async with store.sessions.begin() as session:
        await session.execute(update(Run).values(lease_until=database_time - timedelta(seconds=1)))
    monkeypatch.setattr(storage, "now", lambda: database_time - timedelta(seconds=5))

    if operation == "complete":
        assert not await store.complete(
            run.id,
            owner.lease_token,
            RunResult(outcome="insufficient_candidates", reasons=["test fixture"]),
        )
    elif operation == "renew":
        assert not await store.renew(run.id, owner.lease_token, 60)
    elif operation == "checkpoint":
        assert not await store.checkpoint(run.id, owner.lease_token, "generated", {"stale": True})
    elif operation == "fail":
        await store.fail(run.id, owner.lease_token, "stale_failure", True, 3)
    else:
        with pytest.raises(TaskError, match="lease_lost"):
            await store.reserve_model_call(run.id, owner.lease_token, uuid4(), 100, 6, 6000)

    current = await store.get(run.id)
    assert current.state == "running" and current.stage == "queued"
    assert current.error_code is None and current.result is None
    assert current.usage.requests == 0
    async with store.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ModelCall)) == 0


@pytest.mark.parametrize("offset", [-65, 65])
async def test_valid_worker_keeps_lease_and_retry_delay_despite_clock_skew(
    store, monkeypatch, offset
):
    run = await store.create(CreateRun(external_request_id=uuid4()), uuid4())
    owner = await store.claim(60, 3)
    assert owner is not None
    async with store.sessions() as session:
        before = await session.scalar(select(func.clock_timestamp()))
    monkeypatch.setattr(storage, "now", lambda: before + timedelta(seconds=offset))

    assert await store.renew(run.id, owner.lease_token, 60)
    assert await store.claim(60, 3) is None
    assert await store.checkpoint(run.id, owner.lease_token, "generated", {"saved": True})
    await store.reserve_model_call(run.id, owner.lease_token, uuid4(), 100, 6, 6000)
    await store.fail(run.id, owner.lease_token, "model_unavailable", True, 3)
    assert await store.claim(60, 3) is None

    async with store.sessions() as session:
        current = await session.get(Run, run.id)
        after = await session.scalar(select(func.clock_timestamp()))
        assert (
            before + timedelta(seconds=10)
            <= current.next_attempt_at
            <= after + timedelta(seconds=10)
        )
        assert current.state == "queued" and current.lease_token is None
        assert current.checkpoint == {"saved": True}
        assert current.usage["requests"] == 1
