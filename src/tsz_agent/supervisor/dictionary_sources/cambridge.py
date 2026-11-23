import re
from urllib.parse import quote

from ..entry_facts import Element, EntryFacts, EntryMarkup, Field, dialect_field
from .source import DictionarySource

HOST = "dictionary.cambridge.org"
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


def field(
    tag: str, classes: set[str], stack: list[Element], _: dict[Field, list[str]]
) -> Field | None:
    if tag == "span" and "dhw" in classes:
        return "word"
    if tag == "span" and "dpos" in classes and any("dpos-h" in parent.classes for parent in stack):
        return "pos"
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

    def display_forms(self, forms: list[str]) -> list[str]:
        displayed: list[str] = []
        for form in forms:
            for prefix, labels in INFLECTION_LABELS:
                start = f"{prefix} "
                if form.casefold().startswith(start):
                    displayed.extend(f"{label} {form[len(start) :]}" for label in labels)
                    break
            else:
                displayed.append(form)
        return displayed

    def participle_candidate(self, linked: EntryFacts | None) -> tuple[str, str] | None:
        # A linked British spelling is already verified by the source. Look for
        # an individually listed participle only if the verb entries omit one.
        verbs = [row for row in self.matching_records() if row.get("pos") == ["verb"]]
        if not verbs or any(
            form.startswith("现在分词 ")
            for row in verbs
            for form in self.display_forms(row.get("form", []))
        ):
            return None
        lemma = self.word
        if linked and any(row.get("pos") == ["verb"] for row in linked.matching_records()):
            lemma = linked.word
        if not re.fullmatch(r"[a-z]{2,32}", lemma):
            return None
        if lemma.endswith("ie"):
            stem = lemma[:-2] + "y"
        elif lemma.endswith("e") and not lemma.endswith(("ee", "oe", "ye")):
            stem = lemma[:-1]
        else:
            stem = lemma
        return f"{stem}ing", lemma

    def add_verified_participle(self, word: str, form: str, lemma: str, summary: str) -> bool:
        heading = re.escape(f"{form} definition:")
        root = re.escape(lemma)
        if not re.match(
            rf"{heading}\s*(?:1\.\s*)?present participle of {root}(?=[:.]|\s+\d+\.\s|\s*$)",
            summary,
            flags=re.I,
        ):
            return False
        self.records.append({"word": [word], "pos": ["verb"], "form": [f"现在分词 {form}"]})
        return True

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
        return f"https://{HOST}/dictionary/english/{quote(british, safe='')}", british


SOURCE = DictionarySource(
    "Cambridge Dictionary",
    HOST,
    lambda word: f"https://{HOST}/dictionary/english/{quote(word, safe='')}",
    reader=CambridgeEntry,
)
