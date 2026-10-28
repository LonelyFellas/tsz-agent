from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from tsz_agent.app import create_app
from tsz_agent.models.pricing import estimate, is_peak, period_starts, quote
from tsz_agent.schemas import CreateRun, ModelCallMetrics
from tsz_agent.storage import ModelCall

BJ = ZoneInfo("Asia/Shanghai")


@pytest.mark.parametrize(
    ("local", "peak"),
    [
        ("2026-10-28T08:59:59", False),
        ("2026-10-28T09:00:00", True),
        ("2026-10-28T11:59:59", True),
        ("2026-10-28T12:00:00", False),
        ("2026-10-28T13:59:59", False),
        ("2026-10-28T14:00:00", True),
        ("2026-10-28T17:59:59", True),
        ("2026-10-28T18:00:00", False),
        ("2026-10-31T10:00:00", False),
        ("2026-10-01T10:00:00", False),
        ("2026-10-10T10:00:00", False),
    ],
)
def test_peak_schedule_excludes_weekends_and_official_holidays(local, peak):
    assert is_peak(datetime.fromisoformat(local).replace(tzinfo=BJ)) is peak


def test_decimal_estimate_and_missing_evidence():
    at = datetime(2026, 10, 28, 10, tzinfo=BJ)
    pricing = quote("deepseek-flash", at)
    metrics = ModelCallMetrics(
        phase="generate",
        outcome="response",
        duration_ms=1,
        cache_hit_tokens=800,
        cache_miss_tokens=200,
    )
    assert estimate(pricing, 1000, 120, metrics) == Decimal("0.001392")
    assert estimate(quote("deepseek-flash", at.replace(hour=12)), 1000, 120, metrics) == Decimal(
        "0.000696"
    )
    for evidence in [
        (None, 1000, 120, metrics),
        (pricing, None, 120, metrics),
        (pricing, 1000, None, metrics),
        (pricing, 1000, 120, None),
        (pricing, 999, 120, metrics),
    ]:
        assert estimate(*evidence) is None
    assert quote("unknown-model", at) is None
    assert quote("deepseek-flash", at.replace(year=2027)) is None
    assert quote("deepseek-flash", at.replace(day=27)) is None


def test_beijing_calendar_boundaries():
    # UTC afternoon crosses Beijing midnight and the month boundary.
    at = datetime(2026, 10, 31, 16, tzinfo=UTC)
    starts = period_starts(at)
    assert starts["today"] == datetime(2026, 11, 1, tzinfo=BJ)
    assert starts["month"] == starts["today"]
    assert starts["week"] == datetime(2026, 10, 26, tzinfo=BJ)
    monday = period_starts(datetime(2026, 11, 1, 16, tzinfo=UTC))
    assert monday["week"] == monday["today"] == datetime(2026, 11, 2, tzinfo=BJ)


@pytest.mark.integration
async def test_frozen_prices_and_out_of_order_receipts_survive_cancel(store, monkeypatch):
    at = datetime(2026, 10, 28, 10, tzinfo=BJ)
    monkeypatch.setattr("tsz_agent.storage.now", lambda: at)
    run = await store.create(CreateRun(external_request_id=uuid4()), uuid4())
    lease = await store.claim(60, 3)
    call_id = uuid4()
    await store.reserve_model_call(run.id, lease.lease_token, call_id, 1000, 4, 6000)
    metrics = ModelCallMetrics(
        phase="review",
        outcome="response",
        duration_ms=1,
        cache_hit_tokens=800,
        cache_miss_tokens=200,
    )
    await store.cancel(run.id)
    await store.record_model_metrics(run.id, call_id, metrics)
    assert (await store.model_cost_ledger(1, 20, run.id)).cost_summary.estimated_cost_cny is None
    monkeypatch.setattr("tsz_agent.storage.quote", lambda *args: None)
    await store.record_model_usage(run.id, call_id, 1000, 120)
    await store.record_model_usage(run.id, call_id, 1000, 120)
    await store.record_model_metrics(run.id, call_id, metrics)
    result = await store.model_cost_ledger(1, 20, run.id)
    assert result.cost_summary.priced_calls == 1
    assert result.cost_summary.unknown_calls == 0
    assert Decimal(result.cost_summary.estimated_cost_cny) == Decimal("0.001392")
    assert result.costs[0].pricing.input_miss_per_million == Decimal(2)
    assert result.ledger.items[0].cost is None  # legacy field remains actual-cost unknown
    assert (await store.get(run.id)).usage.cost is None


@pytest.mark.integration
async def test_period_totals_include_unknowns_and_exclude_future_calls(store, settings):
    run = await store.create(CreateRun(external_request_id=uuid4()), uuid4())
    pricing = quote("deepseek-flash", datetime(2026, 10, 28, 10, tzinfo=BJ))
    metrics = ModelCallMetrics(
        phase="generate",
        outcome="response",
        duration_ms=1,
        cache_hit_tokens=800,
        cache_miss_tokens=200,
    )
    dates = [
        datetime(2026, 9, 30, 23, 59, tzinfo=BJ),
        datetime(2026, 10, 1, tzinfo=BJ),
        datetime(2026, 10, 26, tzinfo=BJ),
        datetime(2026, 10, 28, tzinfo=BJ),
        datetime(2026, 10, 28, 11, tzinfo=BJ),
        datetime(2026, 10, 28, 13, tzinfo=BJ),
    ]
    async with store.sessions.begin() as session:
        for index, created in enumerate(dates):
            known = index != 4
            session.add(
                ModelCall(
                    id=uuid4(),
                    run_id=run.id,
                    lease_token=uuid4(),
                    reserved_output_tokens=1000,
                    created_at=created,
                    input_tokens=1000 if known else None,
                    output_tokens=120 if known else None,
                    recorded_at=created if known else None,
                    metrics=metrics.model_dump(mode="json") if known else None,
                    pricing=pricing.model_dump(mode="json") if known else None,
                    estimated_cost_cny=Decimal("0.001392") if known else None,
                )
            )
    result = await store.model_cost_overview(datetime(2026, 10, 28, 12, tzinfo=BJ))
    assert (result.total.total_calls, result.total.priced_calls, result.total.unknown_calls) == (
        5,
        4,
        1,
    )
    assert (result.month.total_calls, result.week.total_calls, result.today.total_calls) == (
        4,
        3,
        2,
    )
    assert Decimal(result.total.estimated_cost_cny) == Decimal("0.005568")
    assert Decimal(result.today.estimated_cost_cny) == Decimal("0.001392")
    empty = await store.model_cost_overview(datetime(2026, 1, 1, tzinfo=BJ))
    assert empty.total.total_calls == 0 and empty.total.estimated_cost_cny == "0"
    headers = {"Authorization": "Bearer " + settings.api_token.get_secret_value()}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app(settings, store)), base_url="http://agent.test"
    ) as client:
        for path in ["/v1/model-costs", "/v1/model-costs/overview"]:
            assert (await client.get(path)).status_code == 401
            assert (await client.get(path, headers=headers)).status_code == 200
        cost_page = (
            await client.get(f"/v1/model-costs?run_id={run.id}&page_size=1", headers=headers)
        ).json()
        assert len(cost_page["costs"]) == 1 and cost_page["cost_summary"]["total_calls"] == 6
        assert cost_page["costs"][0]["call_id"] == cost_page["ledger"]["items"][0]["call_id"]


@pytest.mark.integration
async def test_old_call_without_price_stays_unknown(store):
    run = await store.create(CreateRun(external_request_id=uuid4()), uuid4())
    lease = await store.claim(60, 3)
    call_id = uuid4()
    await store.reserve_model_call(run.id, lease.lease_token, call_id, 1000, 4, 6000)
    async with store.sessions.begin() as session:
        call = await session.scalar(select(ModelCall).where(ModelCall.id == call_id))
        call.pricing = None
    await store.record_model_usage(run.id, call_id, 50, 20)
    await store.record_model_metrics(
        run.id,
        call_id,
        ModelCallMetrics(
            phase="review",
            outcome="response",
            duration_ms=1,
            cache_hit_tokens=0,
            cache_miss_tokens=50,
        ),
    )
    result = await store.model_cost_ledger(1, 20, run.id)
    assert result.cost_summary.estimated_cost_cny is None
    assert result.cost_summary.unknown_calls == 1
