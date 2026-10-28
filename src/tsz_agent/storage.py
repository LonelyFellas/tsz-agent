import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    cast,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from tsz_agent.clients.tsz import TaskError
from tsz_agent.models.pricing import PRICE_VERSION, VALID_UNTIL, estimate, period_starts, quote
from tsz_agent.schemas import (
    CreateRun,
    GenerationContext,
    ModelCallCost,
    ModelCallMetrics,
    ModelCallView,
    ModelCostLedgerPage,
    ModelCostOverview,
    ModelCostPeriod,
    ModelCostTotals,
    ModelLedgerItem,
    ModelLedgerPage,
    ModelLedgerSummary,
    ModelPricing,
    ModelSelection,
    ReviewEvent,
    ReviewExample,
    RunModelMetrics,
    RunResult,
    RunView,
    Usage,
)


def now() -> datetime:
    return datetime.now(UTC)


def digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


class Conflict(Exception):
    pass


class Base(DeclarativeBase):
    pass


class ModelSetting(Base):
    __tablename__ = "agent_model_setting"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    selection: Mapped[dict[str, Any]] = mapped_column(JSONB)


class Run(Base):
    __tablename__ = "agent_runs"
    id: Mapped[UUID] = mapped_column(PGUUID, primary_key=True, default=uuid4)
    external_request_id: Mapped[UUID] = mapped_column(PGUUID, unique=True)
    idempotency_key: Mapped[UUID] = mapped_column(PGUUID, unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    capability: Mapped[str] = mapped_column(String(60))
    state: Mapped[str] = mapped_column(String(20), default="queued")
    stage: Mapped[str] = mapped_column(String(40), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[UUID | None] = mapped_column(PGUUID)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (Index("agent_runs_due", "state", "next_attempt_at"),)


class FeedbackEvent(Base):
    __tablename__ = "agent_review_events"
    event_id: Mapped[UUID] = mapped_column(PGUUID, primary_key=True)
    review_id: Mapped[UUID] = mapped_column(PGUUID)
    review_revision: Mapped[int] = mapped_column(Integer)
    payload_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("review_id", "review_revision"),)


class ModelCall(Base):
    __tablename__ = "agent_model_calls"
    id: Mapped[UUID] = mapped_column(PGUUID, primary_key=True)
    run_id: Mapped[UUID] = mapped_column(PGUUID, ForeignKey("agent_runs.id"), index=True)
    lease_token: Mapped[UUID] = mapped_column(PGUUID)
    reserved_output_tokens: Mapped[int] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    pricing: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    estimated_cost_cny: Mapped[Decimal | None] = mapped_column(Numeric(24, 12))


def estimate_call(call: ModelCall) -> None:
    call.estimated_cost_cny = estimate(
        ModelPricing.model_validate(call.pricing) if call.pricing else None,
        call.input_tokens,
        call.output_tokens,
        ModelCallMetrics.model_validate(call.metrics) if call.metrics else None,
    )


def cost_totals(total: int, priced: int, amount: Decimal | None) -> ModelCostTotals:
    return ModelCostTotals(
        total_calls=total,
        priced_calls=priced,
        unknown_calls=total - priced,
        estimated_cost_cny="0"
        if total == 0
        else format(amount, "f")
        if amount is not None
        else None,
    )


class ReviewCurrent(Base):
    __tablename__ = "agent_review_current"
    review_id: Mapped[UUID] = mapped_column(PGUUID, primary_key=True)
    review_revision: Mapped[int] = mapped_column(Integer)
    source_run_id: Mapped[UUID] = mapped_column(PGUUID, ForeignKey("agent_runs.id"))
    active: Mapped[bool]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


def view(run: Run) -> RunView:
    return RunView.model_validate(
        {
            "id": run.id,
            "external_request_id": run.external_request_id,
            "capability": run.capability,
            "state": run.state,
            "stage": run.stage,
            "attempts": run.attempts,
            "error_code": run.error_code,
            "result": run.result,
            "usage": Usage.model_validate(run.usage),
            "created_at": run.created_at,
            "updated_at": run.updated_at,
        }
    )


class Store:
    def __init__(self, url: str):
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def close(self) -> None:
        await self.engine.dispose()

    async def ready(self) -> None:
        async with self.sessions() as session:
            # Readiness requires the schema, not only a live database socket.
            await session.execute(select(Run).limit(1))
            revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
            if revision != "0005":
                raise RuntimeError("unsupported_database_revision")

    async def model_selection(self) -> ModelSelection:
        async with self.sessions() as session:
            row = await session.get(ModelSetting, 1)
            return ModelSelection.model_validate(row.selection) if row else ModelSelection()

    async def set_model_selection(self, selection: ModelSelection) -> ModelSelection:
        from sqlalchemy.dialects.postgresql import insert

        async with self.sessions.begin() as session:
            statement = insert(ModelSetting).values(id=1, selection=selection.model_dump())
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[ModelSetting.id], set_={"selection": selection.model_dump()}
                )
            )
        return selection

    async def create(self, body: CreateRun, key: UUID) -> RunView:
        payload_hash = digest(body.model_dump(mode="json"))
        async with self.sessions.begin() as session:
            # Serialize both identity axes, including concurrent first inserts.
            for identity in sorted([f"run-key:{key}", f"request:{body.external_request_id}"]):
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": identity},
                )
            existing = (
                await session.scalars(
                    select(Run).where(
                        (Run.idempotency_key == key)
                        | (Run.external_request_id == body.external_request_id)
                    )
                )
            ).all()
            if existing:
                if len(existing) != 1 or existing[0].request_hash != payload_hash:
                    raise Conflict("idempotency_conflict")
                return view(existing[0])
            setting = await session.get(ModelSetting, 1)
            selection = (
                ModelSelection.model_validate(setting.selection) if setting else ModelSelection()
            )
            run = Run(
                usage=Usage(model=selection.usage_model()).model_dump(mode="json"),
                external_request_id=body.external_request_id,
                idempotency_key=key,
                request_hash=payload_hash,
                capability=body.capability,
            )
            session.add(run)
            await session.flush()
            return view(run)

    async def get(self, run_id: UUID) -> RunView | None:
        async with self.sessions() as session:
            run = await session.get(Run, run_id)
            return view(run) if run else None

    async def model_metrics(self, run_id: UUID) -> RunModelMetrics | None:
        async with self.sessions() as session:
            if await session.get(Run, run_id) is None:
                return None
            calls = [
                ModelCallView(call_id=call.id, created_at=call.created_at, metrics=call.metrics)
                for call in await session.scalars(
                    select(ModelCall)
                    .where(ModelCall.run_id == run_id)
                    .order_by(ModelCall.created_at, ModelCall.id)
                )
            ]
        observed = [call.metrics for call in calls if call.metrics is not None]
        cached = [item for item in observed if item.cache_hit_tokens is not None]
        hits = sum(item.cache_hit_tokens or 0 for item in cached) if cached else None
        misses = sum(item.cache_miss_tokens or 0 for item in cached) if cached else None
        cache_total = (hits or 0) + (misses or 0)
        duration = sum(item.duration_ms for item in observed) if observed else None
        return RunModelMetrics(
            run_id=run_id,
            reserved_calls=len(calls),
            observed_calls=len(observed),
            cache_observed_calls=len(cached),
            cache_hit_tokens=hits,
            cache_miss_tokens=misses,
            cache_hit_rate=hits / cache_total if hits is not None and cache_total else None,
            total_duration_ms=duration,
            average_duration_ms=duration / len(observed) if duration is not None else None,
            calls=calls,
        )

    async def model_ledger(self, page: int, page_size: int, run_id: UUID | None) -> ModelLedgerPage:
        hit = cast(ModelCall.metrics["cache_hit_tokens"].astext, BigInteger)
        miss = cast(ModelCall.metrics["cache_miss_tokens"].astext, BigInteger)
        duration = cast(ModelCall.metrics["duration_ms"].astext, Float)
        filters = [ModelCall.run_id == run_id] if run_id is not None else []
        async with self.sessions() as session:
            totals = (
                (
                    await session.execute(
                        select(
                            func.count(ModelCall.id).label("total"),
                            func.count(duration).label("observed_calls"),
                            func.count(ModelCall.recorded_at).label("usage_observed_calls"),
                            func.count(hit).label("cache_observed_calls"),
                            func.sum(ModelCall.input_tokens).label("input_tokens"),
                            func.sum(ModelCall.output_tokens).label("output_tokens"),
                            func.sum(hit).label("cache_hit_tokens"),
                            func.sum(miss).label("cache_miss_tokens"),
                            func.sum(duration).label("total_duration_ms"),
                            func.avg(duration).label("average_duration_ms"),
                        ).where(*filters)
                    )
                )
                .mappings()
                .one()
            )
            rows = await session.execute(
                select(ModelCall, Run)
                .join(Run, Run.id == ModelCall.run_id)
                .where(*filters)
                .order_by(ModelCall.created_at.desc(), ModelCall.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            items = [
                ModelLedgerItem(
                    call_id=call.id,
                    run_id=run.id,
                    external_request_id=run.external_request_id,
                    model=Usage.model_validate(run.usage).model,
                    created_at=call.created_at,
                    input_tokens=call.input_tokens,
                    output_tokens=call.output_tokens,
                    metrics=call.metrics,
                )
                for call, run in rows
            ]
        counts = dict(totals)
        total = counts.pop("total")
        cache_total = (counts["cache_hit_tokens"] or 0) + (counts["cache_miss_tokens"] or 0)
        counts["cache_hit_rate"] = counts["cache_hit_tokens"] / cache_total if cache_total else None
        return ModelLedgerPage(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            summary=ModelLedgerSummary.model_validate(counts),
        )

    async def model_cost_ledger(
        self, page: int, page_size: int, run_id: UUID | None
    ) -> ModelCostLedgerPage:
        ledger = await self.model_ledger(page, page_size, run_id)
        filters = [ModelCall.run_id == run_id] if run_id is not None else []
        async with self.sessions() as session:
            total, priced, amount = (
                await session.execute(
                    select(
                        func.count(ModelCall.id),
                        func.count(ModelCall.estimated_cost_cny),
                        func.sum(ModelCall.estimated_cost_cny),
                    ).where(*filters)
                )
            ).one()
            calls = await session.scalars(
                select(ModelCall).where(ModelCall.id.in_([item.call_id for item in ledger.items]))
            )
            costs = [
                ModelCallCost(
                    call_id=call.id,
                    estimated_cost_cny=format(call.estimated_cost_cny, "f")
                    if call.estimated_cost_cny is not None
                    else None,
                    pricing=call.pricing,
                )
                for call in calls
            ]
        return ModelCostLedgerPage(
            ledger=ledger, costs=costs, cost_summary=cost_totals(total, priced, amount)
        )

    async def model_cost_overview(self, as_of: datetime | None = None) -> ModelCostOverview:
        at = as_of or now()
        starts = period_starts(at)
        columns: list[Any] = []
        for name, start in starts.items():
            condition = ModelCall.created_at < at
            if start is not None:
                condition &= ModelCall.created_at >= start
            columns.extend(
                [
                    func.count(ModelCall.id).filter(condition).label(f"{name}_total"),
                    func.count(ModelCall.estimated_cost_cny)
                    .filter(condition)
                    .label(f"{name}_priced"),
                    func.sum(ModelCall.estimated_cost_cny)
                    .filter(condition)
                    .label(f"{name}_amount"),
                ]
            )
        async with self.sessions() as session:
            row = (await session.execute(select(*columns))).mappings().one()
        periods = {
            name: ModelCostPeriod(
                **cost_totals(
                    row[f"{name}_total"], row[f"{name}_priced"], row[f"{name}_amount"]
                ).model_dump(),
                start=start,
                end=at,
            )
            for name, start in starts.items()
        }
        return ModelCostOverview(
            generated_at=at,
            pricing_version=PRICE_VERSION,
            pricing_valid_until=VALID_UNTIL,
            total=periods["total"],
            today=periods["today"],
            week=periods["week"],
            month=periods["month"],
        )

    async def cancel(self, run_id: UUID) -> RunView | None:
        async with self.sessions.begin() as session:
            run = await session.get(Run, run_id, with_for_update=True)
            if run is None:
                return None
            if run.state not in {"succeeded", "failed", "cancelled"}:
                run.state = "cancelled"
                run.stage = "cancelled"
                run.lease_token = None
                run.lease_until = None
                run.updated_at = now()
            return view(run)

    async def claim(self, lease_seconds: int, max_attempts: int) -> Run | None:
        async with self.sessions.begin() as session:
            # Finish exhausted crashed jobs before searching for runnable work.
            await session.execute(
                text("""UPDATE agent_runs SET state='failed', stage='failed',
                error_code='lease_expired', lease_token=NULL, lease_until=NULL, updated_at=now()
                WHERE state='running' AND lease_until<=now() AND attempts>=:limit"""),
                {"limit": max_attempts},
            )
            run = await session.scalar(
                select(Run)
                .where(
                    ((Run.state == "queued") & (Run.next_attempt_at <= func.now()))
                    | ((Run.state == "running") & (Run.lease_until <= func.now()))
                )
                .where(Run.attempts < max_attempts)
                .order_by(Run.created_at, Run.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if run is None:
                return None
            run.state = "running"
            run.lease_token = uuid4()
            run.lease_until = now() + timedelta(seconds=lease_seconds)
            run.attempts += 1
            run.error_code = None
            run.updated_at = now()
            return run

    async def renew(self, run_id: UUID, token: UUID, lease_seconds: int) -> bool:
        async with self.sessions.begin() as session:
            run = await session.get(Run, run_id, with_for_update=True)
            if not self.owned(run, token):
                return False
            assert run is not None
            run.lease_until = now() + timedelta(seconds=lease_seconds)
            return True

    @staticmethod
    def owned(run: Run | None, token: UUID) -> bool:
        return bool(
            run
            and run.state == "running"
            and run.lease_token == token
            and run.lease_until
            and run.lease_until > now()
        )

    async def checkpoint(
        self, run_id: UUID, token: UUID, stage: str, checkpoint: dict[str, Any]
    ) -> bool:
        async with self.sessions.begin() as session:
            run = await session.get(Run, run_id, with_for_update=True)
            if not self.owned(run, token):
                return False
            assert run is not None
            run.stage, run.checkpoint = stage, checkpoint
            run.updated_at = now()
            return True

    async def complete(self, run_id: UUID, token: UUID, result: RunResult) -> bool:
        async with self.sessions.begin() as session:
            run = await session.get(Run, run_id, with_for_update=True)
            if not self.owned(run, token):
                return False
            assert run is not None
            run.state, run.stage = "succeeded", "completed"
            run.result = result.model_dump(mode="json")
            run.lease_token, run.lease_until, run.updated_at = None, None, now()
            return True

    async def fail(
        self, run_id: UUID, token: UUID, code: str, retryable: bool, max_attempts: int
    ) -> None:
        async with self.sessions.begin() as session:
            run = await session.get(Run, run_id, with_for_update=True)
            if not self.owned(run, token):
                return
            assert run is not None
            run.state = "queued" if retryable and run.attempts < max_attempts else "failed"
            run.error_code = code
            run.next_attempt_at = now() + timedelta(seconds=10 * 2 ** (run.attempts - 1))
            run.lease_token, run.lease_until, run.updated_at = None, None, now()

    async def reserve_model_call(
        self,
        run_id: UUID,
        token: UUID,
        call_id: UUID,
        requested_output: int,
        request_limit: int,
        output_limit: int,
    ) -> tuple[int, Usage]:
        async with self.sessions.begin() as session:
            run = await session.get(Run, run_id, with_for_update=True)
            if not self.owned(run, token):
                raise TaskError("lease_lost")
            assert run is not None
            usage = Usage.model_validate(run.usage)
            pending = await session.scalar(
                select(func.coalesce(func.sum(ModelCall.reserved_output_tokens), 0)).where(
                    ModelCall.run_id == run_id, ModelCall.recorded_at.is_(None)
                )
            )
            remaining = output_limit - usage.output_tokens - int(pending or 0)
            if usage.requests >= request_limit or remaining <= 0 or requested_output <= 0:
                raise TaskError("model_budget_exceeded")
            allowance = min(requested_output, remaining)
            reserved_at = now()
            pricing = quote(usage.model, reserved_at)
            session.add(
                ModelCall(
                    id=call_id,
                    run_id=run_id,
                    lease_token=token,
                    reserved_output_tokens=allowance,
                    created_at=reserved_at,
                    pricing=pricing.model_dump(mode="json") if pricing else None,
                )
            )
            usage.requests += 1
            run.usage, run.updated_at = usage.model_dump(mode="json"), now()
            return allowance, usage

    async def record_model_usage(
        self, run_id: UUID, call_id: UUID, input_tokens: int, output_tokens: int
    ) -> Usage:
        if min(input_tokens, output_tokens) < 0:
            raise TaskError("usage_receipt_invalid")
        async with self.sessions.begin() as session:
            # Accounting must survive cancellation and lease transfer. Lock order matches reserve.
            run = await session.get(Run, run_id, with_for_update=True)
            call = await session.get(ModelCall, call_id, with_for_update=True)
            if run is None or call is None or call.run_id != run_id:
                raise TaskError("usage_receipt_invalid")
            usage = Usage.model_validate(run.usage)
            if call.recorded_at is not None:
                if (call.input_tokens, call.output_tokens) != (input_tokens, output_tokens):
                    raise TaskError("usage_receipt_conflict")
                return usage
            call.input_tokens, call.output_tokens = input_tokens, output_tokens
            call.recorded_at = now()
            estimate_call(call)
            usage.input_tokens += input_tokens
            usage.output_tokens += output_tokens
            run.usage, run.updated_at = usage.model_dump(mode="json"), now()
            return usage

    async def record_model_metrics(
        self, run_id: UUID, call_id: UUID, metrics: ModelCallMetrics
    ) -> None:
        async with self.sessions.begin() as session:
            # Like usage receipts, observations survive cancellation and lease transfer.
            call = await session.get(ModelCall, call_id, with_for_update=True)
            if call is None or call.run_id != run_id:
                raise TaskError("usage_receipt_invalid")
            payload = metrics.model_dump(mode="json")
            if call.metrics is not None and call.metrics != payload:
                raise TaskError("usage_receipt_conflict")
            call.metrics = payload
            estimate_call(call)

    async def feedback(self, event: ReviewEvent) -> bool:
        body, payload_hash = event.model_dump(mode="json"), digest(event.model_dump(mode="json"))
        async with self.sessions.begin() as session:
            for identity in sorted([f"event:{event.event_id}", f"review:{event.review_id}"]):
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": identity},
                )
            previous = await session.get(FeedbackEvent, event.event_id)
            if previous:
                if previous.payload_hash != payload_hash:
                    raise Conflict("event_id_conflict")
                return False
            same_revision = await session.scalar(
                select(FeedbackEvent).where(
                    FeedbackEvent.review_id == event.review_id,
                    FeedbackEvent.review_revision == event.review_revision,
                )
            )
            if same_revision:
                raise Conflict("review_revision_conflict")
            source = await session.get(Run, event.source_run_id)
            if source is None or source.state != "succeeded" or source.result is None:
                raise Conflict("source_run_unavailable")
            result = RunResult.model_validate(source.result)
            if result.question is None:
                raise Conflict("source_run_has_no_question")
            if event.example and event.example.target != result.question.target:
                raise Conflict("feedback_target_mismatch")
            current_body = dict(body)
            if event.decision == "rejected" and event.example is None:
                # Negative examples come from the actual rejected revision, never fabricated text.
                current_body["example"] = ReviewExample(
                    target=result.question.target,
                    question_level=result.question.question_level,
                    prompt=result.question.prompt,
                    distractors=[
                        o.candidate
                        for o in result.question.options
                        if o.option_id != result.question.correct_option_id
                    ],
                    lesson=event.comment or "; ".join(event.reason_codes),
                ).model_dump(mode="json")
            if current_body.get("example"):
                from tsz_agent.capabilities.vocabulary_mcq.validators import valid_pool
                from tsz_agent.clients.tsz import TaskError

                example = ReviewExample.model_validate(current_body["example"])
                example_context = GenerationContext(
                    external_request_id=source.external_request_id,
                    target=example.target,
                    question_level=example.question_level,
                    answer_profile=example.target.answer_profile,
                )
                # Rejected examples deliberately preserve bad distractors as negative evidence.
                if event.decision in {"approved", "edited_and_approved"}:
                    try:
                        valid = valid_pool(example_context, example.distractors)
                    except TaskError as exc:
                        raise Conflict("feedback_source_invalid") from exc
                    if len(valid) != 3:
                        raise Conflict("feedback_source_invalid")
            current = await session.get(ReviewCurrent, event.review_id)
            if current and current.source_run_id != event.source_run_id:
                raise Conflict("review_source_mismatch")
            if current and current.payload["question_revision_id"] != body["question_revision_id"]:
                raise Conflict("review_question_revision_mismatch")
            session.add(
                FeedbackEvent(
                    event_id=event.event_id,
                    review_id=event.review_id,
                    review_revision=event.review_revision,
                    payload_hash=payload_hash,
                    payload=body,
                )
            )
            if current and current.review_revision >= event.review_revision:
                return False
            if current is None:
                current = ReviewCurrent(
                    review_id=event.review_id, source_run_id=event.source_run_id
                )
                session.add(current)
            current.review_revision, current.payload = event.review_revision, current_body
            current.active = event.decision != "revoked"
            current.updated_at = now()
            return True

    async def examples(self, context: GenerationContext) -> list[ReviewEvent]:
        example = ReviewCurrent.payload["example"]
        target = example["target"]
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(ReviewCurrent)
                    .where(
                        ReviewCurrent.active,
                        ReviewCurrent.payload["capability"].astext == "vocabulary_mcq_zh_en",
                        target["part_of_speech"].astext == context.target.part_of_speech,
                        target["answer_profile"].astext == context.answer_profile,
                        example["question_level"].astext == context.question_level,
                    )
                    .order_by(
                        (
                            target["source"]["sense_id"].astext
                            == str(context.target.source.sense_id)
                        ).desc(),
                        ReviewCurrent.updated_at.desc(),
                        ReviewCurrent.review_id,
                    )
                    .limit(12)
                )
            ).all()
            return [ReviewEvent.model_validate(row.payload) for row in rows]
