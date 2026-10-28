from uuid import uuid4

import httpx
import pytest

from tsz_agent.app import create_app
from tsz_agent.schemas import CreateRun, ModelCallMetrics

pytestmark = pytest.mark.integration


async def test_ledger_pagination_filter_summary_and_safe_projection(store, settings):
    runs = []
    for hit, miss in [(20, 30), (160, 40)]:
        run = await store.create(CreateRun(external_request_id=uuid4()), uuid4())
        lease = await store.claim(60, 3)
        call_id = uuid4()
        await store.reserve_model_call(run.id, lease.lease_token, call_id, 1000, 4, 6000)
        await store.record_model_usage(run.id, call_id, hit + miss, 10)
        await store.record_model_metrics(
            run.id,
            call_id,
            ModelCallMetrics(
                phase="generate",
                outcome="response",
                duration_ms=100,
                cache_hit_tokens=hit,
                cache_miss_tokens=miss,
            ),
        )
        await store.checkpoint(run.id, lease.lease_token, "generated", {"private_prompt": "hidden"})
        await store.cancel(run.id)
        runs.append(run)
    pending = await store.create(CreateRun(external_request_id=uuid4()), uuid4())
    lease = await store.claim(60, 3)
    await store.reserve_model_call(pending.id, lease.lease_token, uuid4(), 1000, 4, 6000)
    headers = {"Authorization": "Bearer " + settings.api_token.get_secret_value()}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app(settings, store)), base_url="http://agent.test"
    ) as client:
        assert (await client.get("/v1/model-calls")).status_code == 401
        for query in ["page=0", "page_size=101", "run_id=invalid"]:
            assert (
                await client.get(f"/v1/model-calls?{query}", headers=headers)
            ).status_code == 422
        first = (await client.get("/v1/model-calls?page_size=1", headers=headers)).json()
        second = (await client.get("/v1/model-calls?page=2&page_size=1", headers=headers)).json()
        assert first["total"] == 3 and len(first["items"]) == 1
        assert first["items"][0]["run_id"] == str(pending.id)
        assert first["items"][0]["input_tokens"] is None
        assert first["items"][0]["metrics"] is None
        assert second["items"][0]["run_id"] == str(runs[1].id)
        assert first["summary"] == second["summary"]
        assert first["summary"]["cache_hit_rate"] == 0.72
        assert first["summary"]["input_tokens"] == 250
        assert first["summary"]["usage_observed_calls"] == 2
        assert first["summary"]["average_duration_ms"] == 100
        assert first["items"][0]["cost"] is None
        assert "private_prompt" not in str(second)
        assert set(second["items"][0]) == {
            "call_id",
            "run_id",
            "external_request_id",
            "model",
            "created_at",
            "input_tokens",
            "output_tokens",
            "metrics",
            "cost",
        }
        filtered = (
            await client.get(f"/v1/model-calls?run_id={runs[0].id}", headers=headers)
        ).json()
        assert filtered["total"] == 1 and filtered["summary"]["cache_hit_rate"] == 0.4
        empty = (await client.get(f"/v1/model-calls?run_id={uuid4()}", headers=headers)).json()
        assert empty["total"] == 0 and empty["items"] == []
        assert empty["summary"]["cache_hit_rate"] is None
        assert empty["summary"]["input_tokens"] is None
        assert empty["summary"]["average_duration_ms"] is None
