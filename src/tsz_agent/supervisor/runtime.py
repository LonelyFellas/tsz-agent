"""Domain-independent supervisor claims, recovery and per-agent receipts."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import DateTime, String, func, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from tsz_agent.storage import Base, Conflict, Store


class AdmissionLimit(Exception):
    pass


class Step[K: str](BaseModel):
    model_config = ConfigDict(extra="forbid")
    section: K
    state: Literal["pending", "running", "completed", "failed"]
    error: str = ""


class Execution[K: str](BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: UUID
    done: bool
    steps: list[Step[K]]


class SupervisorRun(Base):
    __tablename__ = "agent_supervisor_runs"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    actor: Mapped[UUID] = mapped_column(index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(60))
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    view: Mapped[dict[str, Any]] = mapped_column(JSONB)
    receipts: Mapped[dict[str, Any]] = mapped_column(JSONB)


async def claim[K: str](
    store: Store,
    actor: UUID,
    fingerprint: str,
    version: str,
    initial: Execution[K],
    reservation: dict[str, Any],
    *,
    reuse: Callable[[AsyncSession], Awaitable[tuple[Execution[K], dict[str, Any]] | None]]
    | None = None,
) -> tuple[dict[str, Any], K | None]:
    async with store.sessions.begin() as db:
        # Serialize admission per editor, including concurrent first requests.
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": actor.int % (2**63)})
        run = await db.get(SupervisorRun, initial.run_id, with_for_update=True)
        now = datetime.now(UTC)
        if run is None:
            recent = await db.scalar(
                select(func.count())
                .select_from(SupervisorRun)
                .where(
                    SupervisorRun.actor == actor,
                    SupervisorRun.created > now - timedelta(minutes=10),
                )
            )
            if (recent or 0) >= 12:
                raise AdmissionLimit
            reused = await reuse(db) if reuse is not None else None
            view, receipts = reused if reused is not None else (initial, {})
            run = SupervisorRun(
                id=initial.run_id,
                actor=actor,
                fingerprint=fingerprint,
                version=version,
                view=view.model_dump(mode="json"),
                receipts=receipts,
                deadline=None,
            )
            db.add(run)
        elif run.fingerprint != fingerprint or run.version != version:
            raise Conflict("run_scope_changed")
        view = type(initial).model_validate(run.view)
        for step in view.steps:
            if step.state == "running":
                if run.deadline and run.deadline > now:
                    return view.model_dump(mode="json"), None
                step.state, step.error = "failed", "result_unknown"
        pending = next((s for s in view.steps if s.state == "pending"), None)
        if pending is None:
            view.done = True
            run.view = view.model_dump(mode="json")
            return view.model_dump(mode="json"), None
        section = pending.section
        pending.state = "running"
        run.deadline = now + timedelta(seconds=60)
        run.view = view.model_dump(mode="json")
        run.receipts = {
            **run.receipts,
            section: {**reservation, "state": "reserved"},
        }
    return view.model_dump(mode="json"), section


async def finish[K: str](
    store: Store,
    view: Execution[K],
    pending: Step[K],
    receipt: dict[str, Any],
    *,
    remember: Callable[[AsyncSession, SupervisorRun], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    async with store.sessions.begin() as db:
        run = await db.get(SupervisorRun, view.run_id, with_for_update=True)
        assert run is not None
        current = type(view).model_validate(run.view)
        run.receipts = {
            **run.receipts,
            pending.section: {**run.receipts[pending.section], **receipt, "state": pending.state},
        }
        if not any(s.section == pending.section and s.state == "running" for s in current.steps):
            return current.model_dump(mode="json")
        view.done = all(s.state in {"completed", "failed"} for s in view.steps)
        run.view = view.model_dump(mode="json")
        if (
            remember is not None
            and pending.state == "completed"
            and run.deadline is not None
            and run.deadline > datetime.now(UTC)
        ):
            await remember(db, run)
        run.deadline = None
    return view.model_dump(mode="json")
