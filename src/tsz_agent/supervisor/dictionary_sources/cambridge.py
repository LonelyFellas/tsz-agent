import re
from urllib.parse import quote

import httpx

from ..entry_facts import (
    EXAMPLE_PLURAL_CONTEXT,
    Element,
    EntryFacts,
    EntryMarkup,
    Field,
    dialect_field,
    regular_s_form,
)
from .source import DictionarySource

HOST = "dictionary.cambridge.org"
ENTRY_PATH = "/dictionary/english/"
_ENTRY_SLUG = re.compile(r"/dictionary/english/[\w'-]+")
INFLECTION_LABELS = (
    ("past tense and past participle", ("过去式", "过去分词")),
    ("past simple and past participle", ("过去式", "过去分词")),
    ("third-person singular", ("第三人称单数",)),
    ("third person singular", ("第三人称单数",)),
    ("present participle", ("现在分词",)),
    ("past participle", ("过去分词",)),
    ("past tense", ("过去式",)),
    ("past simple", ("过去式",)),
    ("comparative", ("比较级",)),
    ("superlative", ("最高级",)),
    ("plural", ("复数",)),
)
FORM_LABELS = {label for _, labels in INFLECTION_LABELS for label in labels}
VERB_LABELS = {"现在分词", "过去式", "过去分词"}


def valid_redirect(url: httpx.URL) -> bool:
    if (
        url.scheme != "https"
        or url.host != HOST
        or url.port is not None
        or url.userinfo
        or url.fragment
        or len(str(url)) > 1000
        or _ENTRY_SLUG.fullmatch(url.path) is None
    ):
        return False
    # Cambridge may put the original lookup in q when redirecting to a word slug.
    parameters = url.params.multi_items()
    if not url.query:
        return True
    return len(parameters) == 1 and parameters[0][0] == "q" and 0 < len(parameters[0][1]) <= 200


def regular_ing_form(word: str) -> str:
    if word.endswith("ie"):
        return word[:-2] + "ying"
    if word.endswith("e") and not word.endswith(("ee", "oe", "ye")):
        return word[:-1] + "ing"
    return word + "ing"


def regular_past_form(word: str) -> str:
    if word.endswith("e"):
        return word + "d"
    if re.search(r"[^aeiou]y$", word):
        return word[:-1] + "ied"
    return word + "ed"


def example_form_patterns(word: str, position: str) -> tuple[tuple[str, str], ...]:
    s_form = re.escape(regular_s_form(word))
    if position == "noun":
        return (
            (
                "复数",
                rf"(?<![\w-])(?:some|many|several|both|few|these|those|two|three"
                rf"|{EXAMPLE_PLURAL_CONTEXT})\s+({s_form})(?![\w'’-])",
            ),
        )

    # Look for a direct object after the form. Avoid ambiguous predicate adjectives
    # and occurrences where the spelling is part of a compound noun.
    object_start = (
        r"(?=\s+(?:a|an|the|this|that|my|your|his|her|its|our|their|me|him|it|us|them)\b)"
    )
    ing_form = re.escape(regular_ing_form(word))
    progressive = (
        r"(?<![\w-])(?:am|is|are|was|were|be|been|being)\s+"
        r"(?:(?:currently|now|still|already|just|actively)\s+)?"
    )
    subject_noun = (
        r"(?![a-z]+(?:s|ed|ing)\b)"
        r"(?!(?:is|was|has|had|do|did|can|could|will|would|may|might|must|should|made|kept|let)\b)"
        r"[a-z]+"
    )
    singular_subject = rf"^(?:he|she|it|(?:this|that)\s+{subject_noun})\s+"
    perfect = r"(?<![\w-])(?:has|have|had)\s+(?:(?:already|just|never|[a-z]+ly|well and truly)\s+)?"
    past_form = re.escape(regular_past_form(word))
    return (
        ("现在分词", rf"{progressive}({ing_form}){object_start}"),
        ("第三人称单数", rf"{singular_subject}({s_form}){object_start}"),
        ("过去分词", rf"{perfect}({past_form}){object_start}"),
    )


def field(
    tag: str, classes: set[str], stack: list[Element], record: dict[Field, list[str]]
) -> Field | None:
    if tag == "span" and "dhw" in classes:
        return "word"
    if tag == "span" and "dpos" in classes and any("dpos-h" in parent.classes for parent in stack):
        return "pos"
    if "dgc" in classes and any("dgram" in parent.classes for parent in stack):
        return "grammar_pattern"
    if (
        "deg" in classes
        and record.get("pos") in (["noun"], ["verb"])
        and any("dexamp" in parent.classes for parent in stack)
    ):
        return "example"
    if any("dinfg" in parent.classes for parent in stack):
        return None
    if "dinfg" in classes:
        return "form"
    if "dinf" in classes:
        return "form"
    if classes & {"ipa", "dipa"}:
        return dialect_field(stack, "uk", "us")
    return None


MARKUP = EntryMarkup(lambda _, classes: "entry-body__el" in classes, field)


class CambridgeEntry(EntryFacts):
    def __init__(self, word: str) -> None:
        super().__init__(MARKUP, word)

    def handle_data(self, data: str) -> None:
        if any("dinfg" in element.classes for element in self.stack) and data.strip():
            if not any(element.classes & {"dlab", "dinf"} for element in self.stack):
                return
        if (
            data.strip() == "r"
            and self.stack
            and "dsp" in self.stack[-1].classes
            and any(element.capture == "uk" for element in self.stack)
        ):
            data = "(r)"
        super().handle_data(data)

    def handle_endtag(self, tag: str) -> None:
        if self.stack and self.stack[-1].tag == tag and "dinf" in self.stack[-1].classes:
            for parent in reversed(self.stack[:-1]):
                if "dinfg" in parent.classes and parent.capture == "form":
                    # A single grammar label can apply to multiple spelling nodes.
                    parent.text.append("、")
                    break
        super().handle_endtag(tag)

    def display_forms(self, forms: list[str]) -> list[str]:
        displayed: list[str] = []
        for form in forms:
            labels: tuple[str, ...] = ()
            for prefix, translated in INFLECTION_LABELS:
                start = f"{prefix} "
                if form.casefold().startswith(start):
                    labels = translated
                    form = form[len(start) :]
                    break
            spellings = [value.strip() for value in form.split("、") if value.strip()]
            if labels:
                displayed.extend(
                    f"{label} {spelling}" for label in labels for spelling in spellings
                )
            else:
                displayed.extend(spellings)
        labeled = {
            spelling.casefold()
            for form in displayed
            for label, _, spelling in (form.partition(" "),)
            if label in FORM_LABELS
        }
        return [form for form in dict.fromkeys(displayed) if form.casefold() not in labeled]

    def matching_records(self) -> list[dict[Field, list[str]]]:
        records = super().matching_records()
        if not re.fullmatch(r"[a-z]{2,32}", self.word):
            return records
        for position in ("noun", "verb"):
            entries = [row for row in records if row.get("pos") == [position]]
            examples = [example for row in entries for example in row.get("example", [])]
            if not examples:
                continue
            listed = {
                form.partition(" ")[0]
                for row in entries
                for form in self.display_forms(row.get("form", []))
            }
            confirmed = []
            for label, pattern in example_form_patterns(self.word, position):
                if label in listed:
                    continue
                for example in examples:
                    if match := re.search(pattern, example, flags=re.I):
                        confirmed.append(f"{label} {match[1]}")
                        break
            if confirmed:
                records.append({"word": [self.word], "pos": [position], "form": confirmed})
        return records

    def verb_form_candidates(self, linked: EntryFacts | None) -> tuple[str, list[str]]:
        verbs = [row for row in self.matching_records() if row.get("pos") == ["verb"]]
        forms = self.display_forms([form for row in verbs for form in row.get("form", [])])
        labels = {form.partition(" ")[0] for form in forms}
        lemma = self.word
        if linked and any(row.get("pos") == ["verb"] for row in linked.matching_records()):
            lemma = linked.word
        if not verbs or not re.fullmatch(r"[a-z]{2,32}", lemma):
            return lemma, []

        candidates = []
        ing = regular_ing_form(lemma)
        if "现在分词" not in labels:
            candidates.append(ing)
        if {"过去式", "过去分词"} - labels:
            # A headword may list irregular forms without labels. Verify those spellings first.
            non_past = {lemma, ing, regular_s_form(lemma)}
            unmarked = [
                form
                for form in forms
                if form not in non_past and re.fullmatch(r"[a-z]{2,32}", form)
            ]
            past_candidates = list(dict.fromkeys([*unmarked, regular_past_form(lemma)]))
            candidates.extend(past_candidates[:2])
        return lemma, candidates

    def add_verified_verb_forms(self, form: str, lemma: str, summary: str) -> None:
        title = re.escape(f"{form} definition:")
        root = re.escape(lemma)
        for relation, labels in INFLECTION_LABELS:
            if all(label in VERB_LABELS for label in labels) and re.match(
                rf"{title}\s*(?:1\.\s*)?{relation} of {root}(?=:|\.(?:\s|$)|\s+\d+\.\s|\s*$)",
                summary,
                flags=re.I,
            ):
                self.records.append(
                    {
                        "word": [self.word],
                        "pos": ["verb"],
                        "form": [f"{label} {form}" for label in labels],
                    }
                )
                return

    def related_entry(self, locator: str, summary: str) -> tuple[str, str] | None:
        # The American lookup can be an explicit cross-reference to the British headword.
        prefix = f"{self.word} definition:"
        if not summary.casefold().startswith(prefix):
            return None
        variant = re.match(
            r"\s*(?:1\.\s+)?US spelling of ([\w'-]+)(?=\s|\.|$)", summary[len(prefix) :], re.I
        )
        if not variant:
            return None
        british = variant[1]
        if british.casefold() == self.word:
            return None
        self.records.append(
            {
                "word": [self.word],
                "form": [f"英式拼写 {british}", f"美式拼写 {self.word}"],
            }
        )
        return f"https://{HOST}{ENTRY_PATH}{quote(british, safe='')}", british


SOURCE = DictionarySource(
    "Cambridge Dictionary",
    HOST,
    lambda word: f"https://{HOST}{ENTRY_PATH}{quote(word, safe='')}",
    reader=CambridgeEntry,
    valid_redirect=valid_redirect,
    # Long entries can put explicitly labeled forms in a later dictionary section.
    max_page_chars=600_000,
)
