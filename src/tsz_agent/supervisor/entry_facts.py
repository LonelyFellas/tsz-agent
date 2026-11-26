"""Read factual fields from the main entry of public dictionary pages."""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Literal

_VOID = set("area base br col embed hr img input link meta param source track wbr".split())
EXAMPLE_PLURAL_CONTEXT = r"one of the(?: (?:lower|upper))?|there (?:are|were)(?: [a-z]+)?"
Field = Literal[
    "word",
    "pos",
    "uk",
    "us",
    "pronunciation",
    "form",
    "definition",
    "example",
    "inflection",
    "inflection_note",
    "grammar_pattern",
]


def regular_s_form(word: str) -> str:
    if re.search(r"[^aeiou]y$", word):
        return word[:-1] + "ies"
    return word + ("es" if word.endswith(("s", "x", "z", "ch", "sh")) else "s")


def append_grammar_evidence(
    excerpt: str, word: str, *, patterns: Iterable[str] = (), examples: Iterable[str] = ()
) -> str:
    result = excerpt
    for label, values, limit in (("句型", patterns, 4), ("例句证据", examples, 2)):
        unique = dict.fromkeys(" ".join(value.split()).replace(" · ", " — ") for value in values)
        for value in [value for value in unique if value and len(value) <= 120][:limit]:
            base = result or word
            if base.partition(" · ")[0].casefold() != word.casefold():
                base = f"{word} · 来源摘要 {base}"
            field = f" · {label} {value}"
            if len(base) + len(field) <= 1400:
                result = base + field
    return result


@dataclass
class Element:
    tag: str
    classes: set[str]
    entry: int | None
    capture: Field | None = None
    text: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EntryMarkup:
    starts_entry: Callable[[str, set[str]], bool]
    field: Callable[[str, set[str], list[Element], dict[Field, list[str]]], Field | None]
    prefer_definition: bool = False


def dialect_field(stack: list[Element], uk_class: str, us_class: str) -> Field | None:
    for parent in reversed(stack):
        if uk_class in parent.classes:
            return "uk"
        if us_class in parent.classes:
            return "us"
    return None


class EntryFacts(HTMLParser):
    def __init__(self, markup: EntryMarkup, word: str) -> None:
        super().__init__()
        self.markup = markup
        self.word = word.casefold()
        self.stack: list[Element] = []
        self.records: list[dict[Field, list[str]]] = []
        self.finished = False  # Read the bounded page to collect repeated POS entries.

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID:
            return
        attr = dict(attrs)
        classes = set((attr.get("class") or "").split())
        entry = self.stack[-1].entry if self.stack else None
        element_id = attr.get("id") or ""
        if entry is None and self.markup.starts_entry(element_id, classes):
            entry = len(self.records)
            self.records.append({})
        element = Element(tag, classes, entry)
        if entry is not None:
            element.capture = self.markup.field(tag, classes, self.stack, self.records[entry])
        self.stack.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _VOID:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if not self.stack or self.stack[-1].entry is None:
            return
        for element in reversed(self.stack):
            if element.tag in {"script", "style"} or element.classes & {
                "visually-hidden",
                "sr-only",
            }:
                return
            if element.capture:
                element.text.append(data)
                return

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID or not self.stack:
            return
        index = next(
            (i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i].tag == tag),
            None,
        )
        if index is None:
            return
        for element in self.stack[index:]:
            if element.capture and element.entry is not None:
                value = " ".join("".join(element.text).split())
                if value:
                    self.records[element.entry].setdefault(element.capture, []).append(value[:240])
        del self.stack[index:]

    def matching_records(self) -> list[dict[Field, list[str]]]:
        return [
            row
            for row in self.records
            if any(word.casefold() == self.word for word in row.get("word", []))
        ]

    def related_entry(self, locator: str, summary: str) -> tuple[str, str] | None:
        return None

    def merge_related(self, other: "EntryFacts") -> None:
        # A provider-verified alternate spelling uses the lookup word in the assembled result.
        self.records.extend({**row, "word": [self.word]} for row in other.matching_records())

    def display_forms(self, forms: list[str]) -> list[str]:
        return forms

    def grammar_patterns(self) -> Iterable[str]:
        return (
            pattern for row in self.matching_records() for pattern in row.get("grammar_pattern", [])
        )

    def excerpt(self, headword: str, summary: str) -> str:
        entries = self.matching_records()

        def values(key: Field) -> list[str]:
            return list(dict.fromkeys(item for row in entries for item in row.get(key, [])))

        phone_labels: tuple[tuple[Field, str], ...] = (("uk", "英"), ("us", "美"))
        phones = [
            f"{label} [{items[0].strip('/[]')}]"
            for key, label in phone_labels
            if (items := values(key))
        ]
        parts = [" / ".join(phones)] if phones else []
        if pos := values("pos"):
            parts.append("词性 " + "、".join(pos))
        if reading := values("pronunciation"):
            parts.append("读音标记 " + "、".join(reading[:3]))
        forms_by_pos: dict[str, list[str]] = {}
        for row in entries:
            if forms := row.get("form"):
                positions = list(dict.fromkeys(row.get("pos", [])))
                position = positions[0] if len(positions) == 1 else ""
                forms_by_pos.setdefault(position, []).extend(forms)
        for position, forms in forms_by_pos.items():
            displayed = "、".join(self.display_forms(list(dict.fromkeys(forms)))[:12])
            parts.append(f"词形 {position} [{displayed}]" if position else f"[ {displayed} ]")
        if not parts:
            return summary
        if self.markup.prefer_definition:
            definitions = values("definition")
            if definitions:
                # One short cited definition is enough for an entry preview.
                parts.append("释义 " + " ".join(definitions[0].split()[:20]))
        elif summary:
            parts.append("来源摘要 " + summary)
        return " · ".join([headword, *parts])[:1400]
