import re
from urllib.parse import quote, urljoin

import httpx

from ..entry_facts import Element, EntryFacts, EntryMarkup, Field, dialect_field, regular_s_form
from .source import DictionarySource

HOST = "www.oxfordlearnersdictionaries.com"
ENTRY_PATH = "/definition/english/"
_ENTRY_SLUG = re.compile(r"/definition/english/[\w'-]+")
_OPPOSITE_POS = {"noun": "verb", "verb": "noun"}
_INFLECTION_LABELS = {"plural": "复数", "comparative": "比较级", "superlative": "最高级"}
_GRADE_POS = {"adjective", "adverb"}


def valid_redirect(url: httpx.URL) -> bool:
    return (
        url.scheme == "https"
        and url.host == HOST
        and url.port is None
        and not (url.userinfo or url.query or url.fragment)
        and _ENTRY_SLUG.fullmatch(url.path) is not None
    )


def field(
    tag: str, classes: set[str], stack: list[Element], record: dict[Field, list[str]]
) -> Field | None:
    if any(parent.capture == "inflection" for parent in stack):
        return None  # Inflection pronunciations belong to the form, not the headword.
    if "inflections" in classes and any("webtop" in parent.classes for parent in stack):
        return "inflection"
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
        if (
            "x" in classes
            and record.get("pos") in (["noun"], ["adjective"], ["adverb"])
            and any("examples" in parent.classes for parent in stack)
        ):
            return "example"
    return None


MARKUP = EntryMarkup(
    lambda element_id, _: element_id == "entryContent", field, prefer_definition=True
)


def head_inflections(text: str, pos: str) -> list[str]:
    content = text.strip("() ")
    labeled = re.split(r"\b(plural|comparative|superlative)\s+", content, flags=re.I)
    if len(labeled) > 1:
        return [
            f"{_INFLECTION_LABELS[label.casefold()]} {form}"
            for label, spelling in zip(labeled[1::2], labeled[2::2], strict=True)
            for raw in re.split(r"\s+or\s+|[,;]", spelling)
            if (form := raw.strip())
        ]
    # Oxford also uses ordered comparative/superlative pairs for irregular entries.
    if pos not in _GRADE_POS:
        return []
    pairs = [[form.strip() for form in pair.split(",")] for pair in content.split(" or ")]
    if any(len(pair) != 2 or not all(pair) for pair in pairs):
        return []
    return [
        f"{label} {form}"
        for pair in pairs
        for label, form in zip(("比较级", "最高级"), pair, strict=True)
    ]


def example_patterns(word: str, pos: str) -> list[tuple[str, str]]:
    if not re.fullmatch(r"[a-z]{2,32}", word):
        return []
    if pos == "noun":
        quantity = (
            r"(?:some|many|several|both|few|these|those|two|three|four|five|six|seven|eight|nine|ten"
            r"|(?:piles|stacks|lots|dozens|hundreds|thousands) of|a (?:number|series) of)"
        )
        return [("复数", rf"(?<![\w-]){quantity}\s+({re.escape(regular_s_form(word))})(?![\w'’-])")]
    if pos not in _GRADE_POS:
        return []

    stem = word[:-1] + "i" if re.search(r"[^aeiou]y$", word) else word.removesuffix("e")
    stems = [stem]
    if re.search(r"[b-df-hj-np-tv-z][aeiou][b-df-hj-np-tvz]$", word):
        stems.append(word + word[-1])
    comparative = "|".join(re.escape(form) for form in [f"more {word}", *(s + "er" for s in stems)])
    superlative = "|".join(
        re.escape(form) for form in [f"most {word}", *(s + "est" for s in stems)]
    )
    return [
        ("比较级", rf"(?<![\w-])({comparative})\s+than\b"),
        (
            "最高级",
            rf"(?<![\w-])(?:the|my|your|his|her|its|our|their)\s+({superlative})(?![\w'’-])",
        ),
    ]


class OxfordEntry(EntryFacts):
    def __init__(self, word: str) -> None:
        super().__init__(MARKUP, word)
        self.related_links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            attr = dict(attrs)
            title = (attr.get("title") or "").casefold()
            if href := attr.get("href"):
                for pos in _OPPOSITE_POS:
                    if title in {
                        f"{self.word} {pos} definition",
                        f"definition of {self.word} {pos} in english",
                    }:
                        self.related_links.append((pos, href))
                        break
        super().handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        for index, element in enumerate(self.stack):
            if element.capture == "inflection":
                children = self.stack[index + 1 :]
                if children and not (children[0].classes & {"inflected_form", "label", "prefix"}):
                    return
                if any(child.classes & {"phonetics", "phon"} for child in children):
                    return
                break
        super().handle_data(data)

    def matching_records(self) -> list[dict[Field, list[str]]]:
        records = super().matching_records()
        for index, row in enumerate(records):
            positions = row.get("pos", [])
            if len(positions) != 1:
                continue
            pos = positions[0]
            forms = [*row.get("form", [])]
            for annotation in row.get("inflection", []):
                forms.extend(head_inflections(annotation, pos))
            examples = row.get("example", [])
            if examples:
                listed = {form.partition(" ")[0] for form in forms}
                for label, pattern in example_patterns(self.word, pos):
                    if label in listed:
                        continue
                    for example in examples:
                        if match := re.search(pattern, example, flags=re.I):
                            forms.append(f"{label} {match[1]}")
                            break
            if forms:
                records[index] = {**row, "form": forms}
                records[index].pop("inflection", None)
        return records

    def missing_position(self) -> str | None:
        positions = {pos for row in super().matching_records() for pos in row.get("pos", [])}
        return next(
            (
                other
                for pos, other in _OPPOSITE_POS.items()
                if pos in positions and other not in positions
            ),
            None,
        )

    def related_entry(self, locator: str, summary: str) -> tuple[str, str] | None:
        complementary = self.missing_position()
        for pos, href in self.related_links:
            if pos != complementary:
                continue
            try:
                url = httpx.URL(urljoin(locator, href))
            except (ValueError, httpx.InvalidURL):
                continue
            if valid_redirect(url) and str(url) != locator:
                return str(url), self.word
        return None

    def merge_related(self, other: EntryFacts) -> None:
        complementary = self.missing_position()
        if complementary and other.word == self.word:
            self.records.extend(
                row for row in other.matching_records() if row.get("pos") == [complementary]
            )

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
