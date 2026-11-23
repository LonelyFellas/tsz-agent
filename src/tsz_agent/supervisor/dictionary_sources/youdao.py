import re
from html.parser import HTMLParser
from urllib.parse import quote, urlencode

from .source import DictionarySource


class YoudaoSummary(HTMLParser):
    """Read the basic entry on Youdao's public dictionary host."""

    def __init__(self) -> None:
        super().__init__()
        self.finished = False
        self._div_depth = 0
        self._field: tuple[str, str] | None = None
        self._field_depth = 0
        self._text: list[str] = []
        self._headword = ""
        self._pronunciations: list[str] = []
        self._positions: list[str] = []
        self._forms = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.finished:
            return
        attributes = dict(attrs)
        if tag == "div" and (self._div_depth or attributes.get("id") == "phrsListTab"):
            self._div_depth += 1
        if not self._div_depth:
            return
        if self._field is not None:
            if tag == self._field[0]:
                self._field_depth += 1
            return
        classes = (attributes.get("class") or "").split()
        for field_tag, name in (
            ("span", "keyword"),
            ("span", "pronounce"),
            ("p", "additional"),
            ("li", ""),
        ):
            if tag == field_tag and (not name or name in classes):
                self._field = (tag, name)
                self._field_depth = 1
                break

    def handle_data(self, data: str) -> None:
        if self._field is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._div_depth:
            self._div_depth -= 1
            self.finished = not self._div_depth
        if self._field is None or tag != self._field[0]:
            return
        self._field_depth -= 1
        if self._field_depth:
            return
        _, field = self._field
        value = " ".join("".join(self._text).split())
        self._text.clear()
        self._field = None
        if field == "keyword":
            self._headword = value
        elif field == "pronounce" and re.fullmatch(r"[英美]\s*\[[^\[\]]{1,80}\]", value):
            self._pronunciations.append(value)
        elif field == "additional":
            self._forms = value
        elif tag == "li" and (pos := re.match(r"^([a-z]{1,6}\.)\s", value)):
            self._positions.append(pos[1])

    def excerpt(self, word: str, summary: str) -> str:
        if not self.finished or self._headword.casefold() != word.casefold():
            return ""
        facts = [
            " / ".join(dict.fromkeys(self._pronunciations)),
            "词性 " + "、".join(dict.fromkeys(self._positions)) if self._positions else "",
            self._forms,
        ]
        if not any(facts):
            return ""
        return " · ".join([self._headword, *(fact for fact in facts if fact)])[:1400]


SOURCE = DictionarySource(
    "有道",
    "www.youdao.com",
    lambda word: "https://www.youdao.com/result?" + urlencode({"word": word, "lang": "en"}),
)
PUBLIC_SOURCE = DictionarySource(
    "有道",
    "dict.youdao.com",
    lambda word: f"https://dict.youdao.com/w/eng/{quote(word, safe='')}/",
    reader=lambda _: YoudaoSummary(),
)
