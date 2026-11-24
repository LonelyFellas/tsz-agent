"""Reusable, editor-scoped outcomes of independent lexicon section runs."""

from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String, tuple_
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from tsz_agent.config import Settings
from tsz_agent.storage import Base, digest

from . import dictionary_materials, gateway
from .lexicon import AGENTS, Input, Material, Proposals, Section, Strict, View, plan
from .runtime import Step, SupervisorRun

# Bump when shared prompts, candidate validation or dictionary evidence extraction changes.
RULES_VERSION = "lexicon-candidates-v1"
ReusableSection = Literal["forms", "grammar"]
CandidateKey = tuple[UUID, UUID, ReusableSection, str]


class LexiconCandidate(Strict):
    source_run_id: UUID
    generated_at: datetime
    result: View


class CandidateLookup(Strict):
    candidate: LexiconCandidate | None


class StoredCandidate(Base):
    __tablename__ = "agent_lexicon_candidates"

    actor_id: Mapped[UUID] = mapped_column(primary_key=True)
    entry_id: Mapped[UUID] = mapped_column(primary_key=True)
    section: Mapped[str] = mapped_column(String(16), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_supervisor_runs.id", ondelete="CASCADE")
    )
    run_created: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    versions: Mapped[dict[str, Any]] = mapped_column(JSONB)
    proposals: Mapped[dict[str, Any]] = mapped_column(JSONB)
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)


def candidate_context(
    settings: Settings, body: Input, material_snapshot: str | None = None
) -> tuple[CandidateKey, dict[str, Any]] | None:
    scope = body.scope
    if len(scope.sections) != 1:
        return None
    section = scope.sections[0]
    if section not in ("forms", "grammar") or plan(scope) != [section]:
        return None

    agent = AGENTS[section]
    versions = {
        "rules": RULES_VERSION,
        "model": settings.deepseek_model,
        "model_endpoint": sha256(settings.supervisor_model_url.encode()).hexdigest(),
        "output_limit": gateway.OUTPUT_LIMIT,
        "section_rules": digest(
            {"task": agent.task, "schema": agent.row.model_json_schema(), "limit": agent.limit}
        ),
        "youdao_search": bool(settings.dictionary_search_key),
        "youdao_public": settings.youdao_public_lookup_enabled,
        "merriam_webster_api": bool(settings.merriam_webster_api_key),
    }
    if material_snapshot is not None:
        versions["dictionary_materials"] = material_snapshot

    fingerprint = digest(
        {
            "scope": scope.model_dump(
                mode="json",
                exclude={"run_id", "base_revision", "snapshot_id", "entry_id", "actor_id"},
            ),
            "materials": [material.model_dump(mode="json") for material in body.materials],
            "versions": versions,
        }
    )
    return (scope.actor_id, scope.entry_id, section, fingerprint), versions


async def read(db: AsyncSession, key: CandidateKey, body: Input) -> LexiconCandidate | None:
    stored = await db.get(StoredCandidate, key)
    if stored is None:
        return None
    scope = body.scope
    return LexiconCandidate(
        source_run_id=stored.source_run_id,
        generated_at=stored.generated_at,
        result=View(
            run_id=scope.run_id,
            base_revision=scope.base_revision,
            snapshot_id=scope.snapshot_id,
            done=True,
            steps=[Step[Section](section=key[2], state="completed")],
            proposals=Proposals.model_validate(stored.proposals),
            sources=[Material.model_validate(source) for source in stored.sources],
        ),
    )


async def legacy_candidate(
    db: AsyncSession, settings: Settings, body: Input
) -> LexiconCandidate | None:
    context = candidate_context(settings, body)
    return await read(db, context[0], body) if context else None


async def current(db: AsyncSession, settings: Settings, body: Input) -> LexiconCandidate | None:
    legacy_context = candidate_context(settings, body)
    if legacy_context is None:
        return None
    legacy = await read(db, legacy_context[0], body)
    previous = dictionary_materials.reusable(legacy.result.sources) if legacy else {}
    persisted = await dictionary_materials.read(
        db, dictionary_materials.query_key(settings, body.scope.headword)
    )
    effective = dictionary_materials.snapshot({**previous, **persisted}.values())
    context = candidate_context(settings, body, effective)
    assert context is not None
    candidate = await read(db, context[0], body)
    if candidate is not None:
        return candidate
    # Earlier candidates bring their own evidence only for the same editor and generation context.
    if legacy is not None and dictionary_materials.snapshot(previous.values()) == effective:
        return legacy
    return None


async def remember(
    db: AsyncSession,
    run: SupervisorRun,
    key: CandidateKey,
    versions: dict[str, Any],
    view: View,
) -> None:
    section: str = key[2]
    proposals = view.proposals.model_dump(mode="json", include={section})
    if not proposals[section]:
        return
    statement = insert(StoredCandidate).values(
        actor_id=key[0],
        entry_id=key[1],
        section=section,
        fingerprint=key[3],
        source_run_id=run.id,
        run_created=run.created,
        generated_at=datetime.now(UTC),
        versions=versions,
        proposals=proposals,
        sources=[source.model_dump(mode="json") for source in view.sources],
    )
    await db.execute(
        statement.on_conflict_do_update(
            index_elements=[
                StoredCandidate.actor_id,
                StoredCandidate.entry_id,
                StoredCandidate.section,
                StoredCandidate.fingerprint,
            ],
            set_={
                name: statement.excluded[name]
                for name in (
                    "source_run_id",
                    "run_created",
                    "generated_at",
                    "versions",
                    "proposals",
                    "sources",
                )
            },
            # Order by the source run's admission, independent of finish order.
            where=tuple_(StoredCandidate.run_created, StoredCandidate.source_run_id)
            < tuple_(statement.excluded.run_created, statement.excluded.source_run_id),
        )
    )
