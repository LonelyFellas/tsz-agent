"""Versioned CNY estimates, never provider billing receipts.

Sources verified 2026-10-28:
https://api-docs.deepseek.com/zh-cn/quick_start/pricing/
https://www.beijing.gov.cn/zhengce/zhengcefagui/202511/t20251104_4258873.html
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from tsz_agent.schemas import ModelCallMetrics, ModelPricing

BEIJING = ZoneInfo("Asia/Shanghai")
PRICE_VERSION = "deepseek-flash-cny-2026-10-28"
VERIFIED_FROM = date(2026, 10, 28)
# Review prices and extend the official holiday calendar before this date.
VALID_UNTIL = date(2027, 1, 1)
HOLIDAYS_2026 = frozenset(
    date(2026, month, day)
    for month, first, last in [
        (1, 1, 3),
        (2, 15, 23),
        (4, 4, 6),
        (5, 1, 5),
        (6, 19, 21),
        (9, 25, 27),
        (10, 1, 7),
    ]
    for day in range(first, last + 1)
)


def is_peak(at: datetime) -> bool:
    local = at.astimezone(BEIJING)
    return (
        local.weekday() < 5
        and local.date() not in HOLIDAYS_2026
        and (9 <= local.hour < 12 or 14 <= local.hour < 18)
    )


def quote(model: str, at: datetime) -> ModelPricing | None:
    if (
        model != "deepseek-flash"
        or not VERIFIED_FROM <= at.astimezone(BEIJING).date() < VALID_UNTIL
    ):
        return None
    peak = is_peak(at)
    return ModelPricing(
        version=PRICE_VERSION,
        model=model,
        tariff="peak" if peak else "off_peak",
        quoted_at=at,
        input_hit_per_million=Decimal("0.04" if peak else "0.02"),
        input_miss_per_million=Decimal("2" if peak else "1"),
        output_per_million=Decimal("8" if peak else "4"),
    )


def estimate(
    pricing: ModelPricing | None,
    input_tokens: int | None,
    output_tokens: int | None,
    metrics: ModelCallMetrics | None,
) -> Decimal | None:
    if pricing is None or input_tokens is None or output_tokens is None or metrics is None:
        return None
    hit, miss = metrics.cache_hit_tokens, metrics.cache_miss_tokens
    if hit is None or miss is None or hit + miss != input_tokens:
        return None
    return (
        hit * pricing.input_hit_per_million
        + miss * pricing.input_miss_per_million
        + output_tokens * pricing.output_per_million
    ) / Decimal(1_000_000)


def period_starts(at: datetime) -> dict[str, datetime | None]:
    day = at.astimezone(BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "total": None,
        "today": day,
        "week": day - timedelta(days=day.weekday()),
        "month": day.replace(day=1),
    }
