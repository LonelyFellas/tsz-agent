"""Reuse the bounded external dictionary references shared by lexicon runs."""

from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import datetime

from sqlalchemy import DateTime, String, func, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from tsz_agent.config import Settings
from tsz_agent.storage import Base, Store, digest

from .lexicon import Material
from .tools import DICTIONARIES

# Bump when provider access, query normalization or the extracted evidence changes.
VERSION = "lexicon-dictionary-v1"


class StoredDictionaryMaterial(Base):
    __tablename__ = "agent_lexicon_dictionary_materials"

    query_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(100), primary_key=True)
    material: Mapped[dict[str, str]] = mapped_column(JSONB)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def query_key(settings: Settings, headword: str) -> str:
    return digest(
        {
            "version": VERSION,
            "headword": headword,
            "youdao_public": settings.youdao_public_lookup_enabled,
            "web_search": bool(settings.dictionary_search_key),
            "merriam_webster_api": bool(settings.merriam_webster_api_key),
        }
    )


def reusable(materials: Iterable[Material]) -> dict[str, Material]:
    return {
        item.source: item
        for item in materials
        if item.source in DICTIONARIES
        and item.locator.startswith("https://")
        and (item.status == "no_match" or (item.status == "found" and item.excerpt.strip()))
    }


def snapshot(materials: Iterable[Material]) -> str:
    refs = reusable(materials)
    return digest(
        {
            "version": VERSION,
            "references": [
                refs[name].model_dump(mode="json") for name in DICTIONARIES if name in refs
            ],
        }
    )


async def read(db: AsyncSession, key: str) -> dict[str, Material]:
    rows = await db.scalars(
        select(StoredDictionaryMaterial.material).where(StoredDictionaryMaterial.query_key == key)
    )
    return reusable(Material.model_validate(row) for row in rows)


async def resolve(
    store: Store,
    key: str,
    previous: Mapping[str, Material],
    fetch: Callable[[tuple[str, ...]], Awaitable[list[Material]]],
) -> list[Material]:
    async with store.sessions() as db:
        available = {**previous, **await read(db, key)}
    if all(name in available for name in DICTIONARIES):
        return [available[name] for name in DICTIONARIES]

    # One bounded lookup per key across API processes. Complete reads never take the lock.
    # Negative keys keep this lock separate from the supervisor's per-editor admission lock.
    lock_key = -1 - (int(key[:16], 16) >> 1)
    async with store.sessions.begin() as db:
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
        materials = {**previous, **await read(db, key)}
        missing = tuple(name for name in DICTIONARIES if name not in materials)
        if missing:
            fetched = {item.source: item for item in await fetch(missing) if item.source in missing}
            for name, material in reusable(fetched.values()).items():
                db.add(
                    StoredDictionaryMaterial(
                        query_key=key, source=name, material=material.model_dump(mode="json")
                    )
                )
            materials.update(fetched)
    return [materials[name] for name in DICTIONARIES if name in materials]
