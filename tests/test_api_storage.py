import asyncio
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from tsz_agent.app import create_app
from tsz_agent.schemas import CreateRun, RunResult
from tsz_agent.storage import Conflict

pytestmark = pytest.mark.integration


async def test_concurrent_delivery_is_one_job_and_conflicts_do_not_overwrite(store):
    body, key = CreateRun(external_request_id=uuid4()), uuid4()
    results = await asyncio.gather(*(store.create(body, key) for _ in range(5)))
    assert len({r.id for r in results}) == 1
    with pytest.raises(Conflict):
        await store.create(CreateRun(external_request_id=uuid4()), key)


async def test_parallel_workers_only_one_claim_and_cancel_fences_results(store):
    run = await store.create(CreateRun(external_request_id=uuid4()), uuid4())
    claims = await asyncio.gather(store.claim(60, 3), store.claim(60, 3))
    claim = next(c for c in claims if c)
    assert sum(c is not None for c in claims) == 1
    await store.cancel(run.id)
    assert not await store.complete(
        run.id,
        claim.lease_token,
        RunResult(outcome="insufficient_candidates", reasons=["不足"]),
    )
    assert (await store.get(run.id)).state == "cancelled"


async def test_expired_lease_cannot_write_and_last_crash_becomes_terminal(store):
    run = await store.create(CreateRun(external_request_id=uuid4()), uuid4())
    old = await store.claim(60, 2)
    async with store.sessions.begin() as session:
        await session.execute(text("UPDATE agent_runs SET lease_until=now()-interval '1 second'"))
    new = await store.claim(60, 2)
    assert new.lease_token != old.lease_token
    assert not await store.checkpoint(run.id, old.lease_token, "bad", {})
    async with store.sessions.begin() as session:
        await session.execute(text("UPDATE agent_runs SET lease_until=now()-interval '1 second'"))
    assert await store.claim(60, 2) is None
    assert (await store.get(run.id)).error_code == "lease_expired"


async def test_api_auth_strict_input_and_idempotency(store, settings):
    app = create_app(settings, store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://agent.test"
    ) as client:
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/readyz")).status_code == 401
        headers = {
            "Authorization": "Bearer " + settings.api_token.get_secret_value(),
            "Idempotency-Key": str(uuid4()),
        }
        body = CreateRun(external_request_id=uuid4()).model_dump(mode="json")
        assert (
            await client.post("/v1/runs", headers=headers, json={**body, "auto_approve": True})
        ).status_code == 422
        result = await client.post("/v1/runs", headers=headers, json=body)
        assert result.status_code == 202
        assert result.headers["x-request-id"]
        again = await client.post("/v1/runs", headers=headers, json=body)
        assert again.json()["id"] == result.json()["id"]
        conflict = await client.post(
            "/v1/runs", headers=headers, json={**body, "external_request_id": str(uuid4())}
        )
        assert conflict.status_code == 409
        assert conflict.headers["content-type"].startswith("application/problem+json")
