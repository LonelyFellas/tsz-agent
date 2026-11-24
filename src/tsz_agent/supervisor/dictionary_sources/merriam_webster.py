import asyncio
import json
import logging
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from pydantic import SecretStr

from ..entry_facts import Element, EntryFacts, EntryMarkup, Field
from ..lexicon import Material
from .source import DictionarySource

HOST = "www.merriam-webster.com"
API_HOST = "www.dictionaryapi.com"
API_PATH = "/api/v3/references/collegiate/json/"
MAX_RESPONSE_BYTES = 512_000
MAX_EXCERPT_LENGTH = 1400
INDEX_FORM_LABELS = {"noun": "复数", "verb": "第三人称单数"}


@dataclass(frozen=True)
class MerriamWebsterEntry:
    headwords: Sequence[str]
    positions: Sequence[str]
    readings: Sequence[str]
    inflections: Sequence[str]
    indexed_inflections: Sequence[str] = ()


def field(
    _: str, classes: set[str], stack: list[Element], record: dict[Field, list[str]]
) -> Field | None:
    if "hword" in classes:
        return "word"
    if "parts-of-speech" in classes:
        return "pos"
    if "prons-entry-list-item" in classes and not record.get("pronunciation"):
        return "pronunciation"
    if "if" in classes and any("vg-ins" in parent.classes for parent in stack):
        return "form"
    return None


MARKUP = EntryMarkup(
    lambda element_id, _: re.fullmatch(r"dictionary-entry-\d+", element_id) is not None,
    field,
)


class MerriamWebsterPage(EntryFacts):
    def excerpt(self, headword: str, summary: str) -> str:
        entries = (
            MerriamWebsterEntry(
                headwords=record.get("word", []),
                positions=record.get("pos", []),
                readings=record.get("pronunciation", []),
                inflections=record.get("form", []),
            )
            for record in self.matching_records()
        )
        return _entry_excerpt(headword, entries, summary)


SOURCE = DictionarySource(
    "Merriam-Webster",
    HOST,
    lambda word: f"https://{HOST}/dictionary/{quote(word, safe='')}",
    reader=lambda word: MerriamWebsterPage(MARKUP, word),
    # Ads and navigation can push the main dictionary entries deep into the HTML.
    max_page_chars=1_600_000,
)


def _mask_collegiate_query(record: logging.LogRecord) -> bool:
    # HTTPX logs the full URL at INFO. The API requires the key in its query string.
    args = record.args
    if isinstance(args, tuple) and len(args) > 1:
        url = args[1]
        if isinstance(url, httpx.URL) and url.host == API_HOST and url.path.startswith(API_PATH):
            record.args = (args[0], url.copy_with(query=None), *args[2:])
    return True


logging.getLogger("httpx").addFilter(_mask_collegiate_query)


def _objects(value: object) -> list[dict[str, object]]:
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


def _text(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _inflection_spellings(value: object) -> Iterator[str]:
    for form in _objects(value):
        if spelling := _text(form.get("if")).replace("*", ""):
            yield spelling


def _collegiate_inflections(entry: dict[str, object]) -> Iterator[str]:
    yield from _inflection_spellings(entry.get("ins"))
    # Definitions may list word-sense-specific forms. Keep derived entries and phrases out.
    pending = [entry.get("def")]
    while pending:
        node = pending.pop()
        if isinstance(node, list):
            pending.extend(reversed(node))
        elif isinstance(node, dict):
            yield from _inflection_spellings(node.get("ins"))
            pending.extend(value for key, value in reversed(node.items()) if key != "ins")


def _indexed_inflections(meta: object, word: str, pos: str) -> list[str]:
    if pos not in INDEX_FORM_LABELS or not re.fullmatch(r"[A-Za-z]+", word):
        return []
    stems = meta.get("stems") if isinstance(meta, dict) else None
    if not isinstance(stems, list):
        return []

    # Search stems also include derivatives and phrases. Use only regular -s forms
    # that the index actually associates with this exact subentry.
    lower = word.lower()
    candidates = {f"{word}s"}
    if lower.endswith(("s", "sh", "ch", "x", "z", "o")):
        candidates.add(f"{word}es")
        if lower.endswith(("s", "z")) and not lower.endswith(("ss", "zz")):
            candidates.add(f"{word}{word[-1]}es")
    if re.search(r"[^aeiou]y$", lower):
        candidates.add(f"{word[:-1]}ies")
    return [stem for stem in stems if isinstance(stem, str) and stem in candidates]


def _collegiate_entries(
    entries: list[dict[str, object]], word: str
) -> Iterator[MerriamWebsterEntry]:
    for entry in entries:
        head = entry.get("hwi")
        if not isinstance(head, dict):
            continue
        spelling = _text(head.get("hw")).replace("*", "")
        if spelling.casefold() != word.casefold():
            continue
        pos = _text(entry.get("fl"))
        yield MerriamWebsterEntry(
            headwords=[spelling],
            positions=[pos] if pos else [],
            readings=[
                reading for pron in _objects(head.get("prs")) if (reading := _text(pron.get("mw")))
            ],
            inflections=list(_collegiate_inflections(entry)),
            indexed_inflections=_indexed_inflections(entry.get("meta"), spelling, pos),
        )


def _british_spelling_target(entries: list[dict[str, object]], word: str) -> str:
    targets: set[str] = set()
    for entry in entries:
        head = entry.get("hwi")
        if (
            not isinstance(head, dict)
            or _text(head.get("hw")).replace("*", "").casefold() != word.casefold()
        ):
            continue
        for reference in _objects(entry.get("cxs")):
            if _text(reference.get("cxl")).casefold() not in {
                "british spelling of",
                "chiefly british spelling of",
            }:
                continue
            for item in _objects(reference.get("cxtis")):
                targets.add(_text(item.get("cxt")).casefold())
    if len(targets) == 1:
        target = targets.pop()
        if target != word.casefold() and re.fullmatch(r"[a-z]{2,40}", target):
            return target
    return ""


def _entry_excerpt(
    word: str,
    entries: Iterable[MerriamWebsterEntry],
    summary: str = "",
    *,
    american_spelling: str = "",
) -> str:
    base_spelling = (american_spelling or word).casefold()
    forms_by_pos: dict[str, list[str]] = {}
    headwords_by_pos: dict[str, list[str]] = {}
    indexed_by_pos: dict[str, list[str]] = {}
    unassigned_forms: list[str] = []
    readings: list[str] = []
    for entry in entries:
        positions = list(dict.fromkeys(entry.positions))
        for position in positions:
            forms_by_pos.setdefault(position, [])
        if len(positions) == 1:
            position = positions[0]
            forms_by_pos[position].extend(entry.inflections)
            if position in {"adjective", "abbreviation"}:
                headwords_by_pos.setdefault(position, []).extend(
                    f"原形 {headword}"
                    for headword in entry.headwords
                    if headword.casefold() == base_spelling
                )
            if entry.indexed_inflections:
                indexed_by_pos.setdefault(position, []).extend(entry.indexed_inflections)
        else:
            unassigned_forms.extend(entry.inflections)
        readings.extend(entry.readings)

    parts = []
    if forms_by_pos:
        parts.append("词性 " + "、".join(forms_by_pos))
    if readings:
        parts.append("读音标记 " + "、".join(list(dict.fromkeys(readings))[:3]))
    for position, inflections in forms_by_pos.items():
        displayed = inflections or headwords_by_pos.get(position, [])
        parts.append(f"词形 {position} [" + "、".join(dict.fromkeys(displayed)) + "]")
    if unassigned_forms:
        parts.append("[ " + "、".join(dict.fromkeys(unassigned_forms)) + " ]")
    for position, candidates in indexed_by_pos.items():
        explicit = set(forms_by_pos[position])
        indexed = [form for form in dict.fromkeys(candidates) if form not in explicit]
        if indexed:
            label = INDEX_FORM_LABELS[position]
            parts.append(
                f"索引词形 {position} [" + "、".join(f"{label} {form}" for form in indexed) + "]"
            )
    if parts and american_spelling:
        parts.append(f"[ 英式拼写 {word}、美式拼写 {american_spelling} ]")

    excerpt = word
    for part in parts:
        if len(excerpt) + len(part) + 3 <= MAX_EXCERPT_LENGTH:
            excerpt += f" · {part}"
    if excerpt == word:
        return summary
    if summary:
        excerpt = f"{excerpt} · 来源摘要 {summary}"
    return excerpt[:MAX_EXCERPT_LENGTH]


async def _fetch_collegiate(client: httpx.AsyncClient, word: str, key: SecretStr) -> object:
    async with client.stream(
        "GET",
        f"https://{API_HOST}{API_PATH}{quote(word, safe='')}",
        params={"key": key.get_secret_value()},
        headers={"Accept": "application/json"},
        follow_redirects=False,
    ) as response:
        if response.status_code != 200:
            return None
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                return None
            body.extend(chunk)
    return json.loads(body)


async def lookup_collegiate(
    client: httpx.AsyncClient, item: Material, word: str, key: SecretStr
) -> Material:
    item.status = "unavailable"
    try:
        async with asyncio.timeout(7):
            result = await _fetch_collegiate(client, word, key)
            if not isinstance(result, list):
                return item
            if all(isinstance(match, str) for match in result):
                # Empty lists and spelling suggestions contain no dictionary entries.
                item.status = "no_match"
                return item
            entries = _objects(result)
            excerpt = _entry_excerpt(word, _collegiate_entries(entries, word))
            if not excerpt and (target := _british_spelling_target(entries, word)):
                related = _objects(await _fetch_collegiate(client, target, key))
                excerpt = _entry_excerpt(
                    word, _collegiate_entries(related, target), american_spelling=target
                )
                if excerpt:
                    item.locator = SOURCE.url(target)
    except (TimeoutError, httpx.HTTPError, ValueError, RecursionError):
        return item

    if excerpt:
        item.status, item.excerpt = "found", excerpt
    return item
