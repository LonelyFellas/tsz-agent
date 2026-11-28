import re
from html.parser import HTMLParser
from urllib.parse import quote

from ..entry_facts import append_grammar_evidence
from .source import DictionarySource


class YoudaoSummary(HTMLParser):
    """Read the basic entry on Youdao's public dictionary host."""

    def __init__(self) -> None:
        super().__init__()
        self.finished = False
        self._section: str | None = None
        self._completed_sections: set[str] = set()
        self._collins_heading = False
        self._collins_headword = ""
        self._div_depth = 0
        self._definitions_depth: int | None = None
        self._examples_depth: int | None = None
        self._grammar_examples: list[str] = []
        self._example_lines: list[str] | None = None
        self._bilingual_examples: list[tuple[str, ...]] = []
        self._field: tuple[str, str] | None = None
        self._field_depth = 0
        self._text: list[str] = []
        self._headword = ""
        self._pronunciations: dict[str, str] = {}
        self._positions: list[str] = []
        self._forms = ""
        self._definitions: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.finished:
            return
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "div":
            section = attributes.get("id")
            if not self._section and section not in self._completed_sections:
                if section == "phrsListTab" or (
                    "phrsListTab" in self._completed_sections
                    and section in ("collinsResult", "bilingual")
                ):
                    self._section = section
            if self._section:
                self._div_depth += 1
                if (
                    self._section == "phrsListTab"
                    and "trans-container" in classes
                    and self._definitions_depth is None
                ):
                    self._definitions_depth = self._div_depth
        if not self._section:
            return
        if self._field is not None:
            if tag == self._field[0]:
                self._field_depth += 1
            return
        if self._section == "bilingual":
            if tag == "li":
                self._example_lines = []
            elif (
                tag == "p"
                and self._example_lines is not None
                and len(self._example_lines) < 2
                and "example-via" not in classes
            ):
                self._field = (tag, "bilingual")
                self._field_depth = 1
            return
        if self._section == "collinsResult":
            if tag == "div" and "examples" in classes:
                self._examples_depth = self._div_depth
            if tag == "h4":
                self._collins_heading = True
                self._collins_headword = ""
            if tag == "span" and self._collins_heading:
                if "title" in classes:
                    self._field = (tag, "title")
                    self._field_depth = 1
                elif {"additional", "pattern"} <= classes:
                    self._field = (tag, "pattern")
                    self._field_depth = 1
            if (
                tag == "p"
                and self._examples_depth is not None
                and self._collins_headword.casefold() == self._headword.casefold()
                and len(self._grammar_examples) < 2
            ):
                self._field = (tag, "example")
                self._field_depth = 1
            return
        for field_tag, name in (
            ("span", "keyword"),
            ("span", "pronounce"),
            ("p", "additional"),
            ("li", ""),
        ):
            if field_tag == "li" and self._definitions_depth is None:
                continue
            if tag == field_tag and (not name or name in classes):
                self._field = (tag, name)
                self._field_depth = 1
                break

    def handle_data(self, data: str) -> None:
        if self._field is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.finished:
            return
        if tag == "h4":
            self._collins_heading = False
        if tag == "li" and self._section == "bilingual" and self._example_lines is not None:
            if self._example_lines:
                self._bilingual_examples.append(tuple(self._example_lines))
            self._example_lines = None
        if tag == "div" and self._section:
            if self._div_depth == self._definitions_depth:
                self._definitions_depth = None
            if self._div_depth == self._examples_depth:
                self._examples_depth = None
            self._div_depth -= 1
            if not self._div_depth:
                self._completed_sections.add(self._section)
                self._section = None
                self.finished = (
                    not self._headword or {"collinsResult", "bilingual"} <= self._completed_sections
                )
                self._field = None
                self._text.clear()
                return
        if self._field is None or tag != self._field[0]:
            return
        self._field_depth -= 1
        if self._field_depth:
            return
        _, field = self._field
        value = " ".join("".join(self._text).split())
        self._text.clear()
        self._field = None
        if field == "keyword" and not self._headword:
            self._headword = value
        elif field == "pronounce" and (
            match := re.fullmatch(r"([英美])\s*(\[[^\[\]]{1,80}\])", value)
        ):
            self._pronunciations.setdefault(match[1], match[2])
        elif field == "additional" and (match := re.fullmatch(r"\[\s*(.+?)\s*\]", value)):
            self._forms = f"[{match[1]}]"
        elif field == "title":
            self._collins_headword = value
        elif field == "example" and re.match(r"^[A-Za-z]", value):
            self._grammar_examples.append(f"（柯林斯）{value}")
        elif field == "bilingual" and value and self._example_lines is not None:
            self._example_lines.append(value)
        elif (
            field == "pattern"
            and not self._forms
            and self._collins_headword.casefold() == self._headword.casefold()
            and (match := re.fullmatch(r"\(\s*(.+?)\s*\)", value))
        ):
            forms = [form.strip() for form in match[1].split(",")]
            if all(re.fullmatch(r"[A-Za-z]+(?:[-'’ ][A-Za-z]+)*", form) for form in forms):
                self._forms = "[ " + "、".join(dict.fromkeys(forms)) + " ]"
        elif tag == "li" and value:
            self._definitions.append(value)
            if pos := re.match(r"^([a-z]{1,6}\.)\s", value):
                self._positions.append(pos[1])

    def excerpt(self, word: str, summary: str) -> str:
        if (
            "phrsListTab" not in self._completed_sections
            or self._headword.casefold() != word.casefold()
        ):
            return ""
        facts = [
            " / ".join(
                f"{accent} {self._pronunciations[accent]}"
                for accent in ("英", "美")
                if accent in self._pronunciations
            ),
            "词性 " + "、".join(dict.fromkeys(self._positions)) if self._positions else "",
            self._forms,
        ]
        excerpt = (
            " · ".join([self._headword, *(fact for fact in facts if fact)]) if any(facts) else ""
        )
        excerpt = append_grammar_evidence(excerpt[:1400], word, examples=self._grammar_examples)
        fields = [f"释义 {definition.replace(' · ', ' — ')}" for definition in self._definitions]
        for example in self._bilingual_examples:
            fields.append(
                " · ".join(
                    f"{label} {line.replace(' · ', ' — ')}"
                    for label, line in zip(("例句原文", "例句译文"), example, strict=False)
                )
            )
        for field in dict.fromkeys(fields):
            candidate = f"{excerpt or self._headword} · {field}"
            if len(candidate) <= 1400:
                excerpt = candidate
        return excerpt


SOURCE = DictionarySource(
    "有道",
    "dict.youdao.com",
    lambda word: f"https://dict.youdao.com/w/eng/{quote(word, safe='')}/",
    reader=lambda _: YoudaoSummary(),
)
