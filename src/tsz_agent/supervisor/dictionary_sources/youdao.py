import re
from html.parser import HTMLParser
from urllib.parse import quote

from .source import DictionarySource


class YoudaoSummary(HTMLParser):
    """Read the basic entry on Youdao's public dictionary host."""

    def __init__(self) -> None:
        super().__init__()
        self.finished = False
        self._basic_complete = False
        self._collins_heading = False
        self._collins_headword = ""
        self._div_depth = 0
        self._definitions_depth: int | None = None
        self._field: tuple[str, str] | None = None
        self._field_depth = 0
        self._text: list[str] = []
        self._headword = ""
        self._pronunciations: dict[str, str] = {}
        self._positions: list[str] = []
        self._forms = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.finished:
            return
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        section = "collinsResult" if self._basic_complete else "phrsListTab"
        if tag == "div" and (self._div_depth or attributes.get("id") == section):
            self._div_depth += 1
            if (
                not self._basic_complete
                and "trans-container" in classes
                and self._definitions_depth is None
            ):
                self._definitions_depth = self._div_depth
        if not self._div_depth:
            return
        if self._field is not None:
            if tag == self._field[0]:
                self._field_depth += 1
            return
        if self._basic_complete:
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
        if tag == "div" and self._div_depth:
            if self._div_depth == self._definitions_depth:
                self._definitions_depth = None
            self._div_depth -= 1
            if not self._div_depth:
                self.finished = self._basic_complete or bool(self._forms) or not self._headword
                self._basic_complete = True
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
        elif (
            field == "pattern"
            and self._collins_headword.casefold() == self._headword.casefold()
            and (match := re.fullmatch(r"\(\s*(.+?)\s*\)", value))
        ):
            forms = [form.strip() for form in match[1].split(",")]
            if all(re.fullmatch(r"[A-Za-z]+(?:[-'’ ][A-Za-z]+)*", form) for form in forms):
                self._forms = "[ " + "、".join(dict.fromkeys(forms)) + " ]"
                self.finished = True
        elif tag == "li" and (pos := re.match(r"^([a-z]{1,6}\.)\s", value)):
            self._positions.append(pos[1])

    def excerpt(self, word: str, summary: str) -> str:
        if not self._basic_complete or self._headword.casefold() != word.casefold():
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
        if not any(facts):
            return ""
        return " · ".join([self._headword, *(fact for fact in facts if fact)])[:1400]


SOURCE = DictionarySource(
    "有道",
    "dict.youdao.com",
    lambda word: f"https://dict.youdao.com/w/eng/{quote(word, safe='')}/",
    reader=lambda _: YoudaoSummary(),
)
