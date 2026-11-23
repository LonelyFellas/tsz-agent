import re
from urllib.parse import quote, urljoin

import httpx

from ..entry_facts import Element, EntryFacts, EntryMarkup, Field, dialect_field
from .source import DictionarySource

HOST = "www.oxfordlearnersdictionaries.com"
ENTRY_PATH = "/definition/english/"
_ENTRY_SLUG = re.compile(r"/definition/english/[\w'-]+")


def valid_redirect(url: httpx.URL) -> bool:
    return (
        url.scheme == "https"
        and url.host == HOST
        and url.port is None
        and not (url.userinfo or url.query or url.fragment)
        and _ENTRY_SLUG.fullmatch(url.path) is not None
    )


def field(
    tag: str, classes: set[str], stack: list[Element], _: dict[Field, list[str]]
) -> Field | None:
    if tag == "h1" and "headword" in classes:
        return "word"
    if tag == "td" and "verb_form" in classes:
        return "form"
    if "phon" in classes:
        return dialect_field(stack, "phons_br", "phons_n_am")
    if tag == "span":
        if "pos" in classes and any("webtop" in parent.classes for parent in stack):
            return "pos"
        if "def" in classes:
            return "definition"
    return None


MARKUP = EntryMarkup(
    lambda element_id, _: element_id == "entryContent", field, prefer_definition=True
)


class OxfordEntry(EntryFacts):
    def __init__(self, word: str) -> None:
        super().__init__(MARKUP, word)
        self.verb_links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            attr = dict(attrs)
            title = (attr.get("title") or "").casefold()
            if title in {
                f"{self.word} verb definition",
                f"definition of {self.word} verb in english",
            } and (href := attr.get("href")):
                self.verb_links.append(href)
        super().handle_starttag(tag, attrs)

    def related_entry(self, locator: str, summary: str) -> tuple[str, str] | None:
        if any("verb" in row.get("pos", []) for row in self.matching_records()):
            return None
        for href in self.verb_links:
            url = httpx.URL(urljoin(locator, href))
            if valid_redirect(url) and str(url) != locator:
                return str(url), self.word
        return None

    def display_forms(self, forms: list[str]) -> list[str]:
        labels = (
            ("present simple I / you / we / they", "原形"),
            ("he / she / it", "第三人称单数"),
            ("past simple", "过去式"),
            ("past participle", "过去分词"),
            ("-ing form", "现在分词"),
        )
        formatted = []
        for value in forms:
            for prefix, label in labels:
                if value.startswith(prefix + " "):
                    formatted.append(f"{label} {value[len(prefix) :].strip()}")
                    break
            else:
                formatted.append(value)
        return formatted


SOURCE = DictionarySource(
    "Oxford Learner’s Dictionaries",
    HOST,
    lambda word: f"https://{HOST}{ENTRY_PATH}{quote(word, safe='')}",
    reader=OxfordEntry,
    valid_redirect=valid_redirect,
)
