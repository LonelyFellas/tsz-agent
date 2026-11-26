"""Section agents and the dependency plan for the smart lexicon editor."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from tsz_agent.form_candidates import CandidatePos

from .runtime import Execution

Section = Literal[
    "forms",
    "senses",
    "groups",
    "grammar",
    "definitions",
    "equivalents",
    "relations",
    "dialect",
    "examples",
    "translations",
]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Headword = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Level = Literal["A1", "A2", "B1", "B2", "C1", "C2"]
Index = Annotated[int, Field(ge=0, lt=100)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SenseContext(Strict):
    pos: Name
    gloss: Annotated[str, Field(max_length=500)]


class GrammarSubPosOption(Strict):
    code: Name
    name_zh: Name
    name_en: Name


class Scope(Strict):
    run_id: UUID
    entry_id: UUID
    actor_id: UUID
    base_revision: Annotated[int, Field(ge=1)]
    headword: Headword
    medium: Literal["zh-Hans", "zh-Hant"]
    target: Literal["en-GB", "en-US"]
    sections: Annotated[list[Section], Field(min_length=1, max_length=10)]
    senses: Annotated[list[SenseContext], Field(max_length=80)]
    existing_pos: Annotated[list[Name], Field(max_length=60)]
    examples: Annotated[list[Text], Field(max_length=50)]
    catalog: Annotated[dict[Name, list[Name]], Field(min_length=1, max_length=60)]
    grammar_sub_pos: Annotated[dict[Name, list[GrammarSubPosOption]], Field(max_length=60)] = Field(
        default_factory=dict, exclude_if=lambda options: not options
    )
    snapshot_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class DictionaryLookup(Strict):
    headword: Headword
    source: Literal[
        "有道", "Oxford Learner’s Dictionaries", "Merriam-Webster", "Cambridge Dictionary"
    ]


class Material(Strict):
    source: Name
    locator: Annotated[str, Field(max_length=1000)]
    excerpt: Annotated[str, Field(max_length=1400)]
    status: Literal["found", "no_match", "unavailable", "not_configured"]


class Input(Strict):
    scope: Scope
    materials: Annotated[list[Material], Field(max_length=12)]


class Sense(Strict):
    pos: Name
    gloss: Text
    level: Level


class BySense(Strict):
    sense: Index


class Group(BySense):
    name_en: Name
    name_zh: Name


class Grammar(Strict):
    pos: Name
    pattern: Text
    sub_pos: Name | None = Field(default=None, exclude_if=lambda code: code is None)


class Definition(BySense):
    english: Text
    explanation: Text


class Equivalent(BySense):
    word: Name


class Relation(BySense):
    kind: Literal["synonym", "antonym", "derivative"]
    word: Name
    gloss: Name
    score: Annotated[float, Field(ge=0, le=99.9, multiple_of=0.1)]


class Dialect(BySense):
    usage: Literal["en-GB", "en-US"]
    source_locator: Annotated[str, Field(min_length=1, max_length=1000)]
    evidence: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class Example(BySense):
    text: Text
    level: Level


class Translation(Strict):
    example: Index
    text: Text


class Proposals(Strict):
    forms: list[CandidatePos] = Field(default_factory=list)
    senses: list[Sense] = Field(default_factory=list)
    groups: list[Group] = Field(default_factory=list)
    grammar: list[Grammar] = Field(default_factory=list)
    definitions: list[Definition] = Field(default_factory=list)
    equivalents: list[Equivalent] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    dialect: list[Dialect] = Field(default_factory=list)
    examples: list[Example] = Field(default_factory=list)
    translations: list[Translation] = Field(default_factory=list)


@dataclass(frozen=True)
class SectionAgent:
    row: type[BaseModel]
    task: str
    limit: int = 24

    def parse(self, output: object) -> Sequence[BaseModel]:
        if not isinstance(output, dict) or set(output) != {"items"}:
            raise ValueError("invalid_output")
        values = output["items"]
        if not isinstance(values, list) or len(values) > self.limit:
            raise ValueError("invalid_output")
        return [self.row.model_validate(item) for item in values]


SCOPED_SENSE_RULE = (
    "sense 必须使用 existing_senses 的显式索引。对应 gloss 非空时，严格沿用它的语义；"
    "词典中同一个词的其他义项不适用。"
)

OBJECT_COMPLEMENT_PATTERN = " + object + object complement (e.g. "
# Full constructions with a clear direct object and an object-oriented predicate.
# Arbitrary AI-generated examples may still be displayed as unclassified patterns.
OBJECT_COMPLEMENT_EXAMPLES: dict[str, tuple[str, ...]] = {
    "appoint": ("appoint [her] [chair]",),
    "call": ("call [it] [a mistake]",),
    "consider": ("consider [him] [an expert]",),
    "drive": ("drive [him] [mad]",),
    "elect": ("elect [her] [president]",),
    "find": ("find [it] [difficult]",),
    "keep": ("keep [them] [safe]",),
    "leave": ("leave [the door] [open]",),
    "make": ("make [them] [happy]",),
    "paint": ("paint [the door] [red]",),
    "run": (
        "run [the engine] [hot]",
        "run [the tank] [dry]",
        "run [the business] [into the ground]",
    ),
}

AGENTS: dict[Section, SectionAgent] = {
    "forms": SectionAgent(
        CandidatePos,
        "判断实际词性，按 catalog 提供常用词形、英美拼写和字典音标。每词性含一项 base。",
        8,
    ),
    "senses": SectionAgent(
        Sense,
        "补充尚未列在 existing_senses 中的常见独立词义。gloss 用指定媒介语。避免重复生成已有词义。",
        12,
    ),
    "groups": SectionAgent(Group, "为目录中每个词义分配恰当的语义区间，提供中英文名称。"),
    "grammar": SectionAgent(
        Grammar,
        "按词性给出常见语法结构，pattern 含目标词。根据该句型的实际语法功能，"
        "从 grammar_sub_pos 中该 pos 对应的系统选项推荐一个 sub_pos，填写原始 code。"
        "判断动作义的及物性时，只有状语、没有直接宾语用 V-I；"
        "只有一个直接宾语和可选状语用 V-T-MONO（若目录有对应选项）。"
        "只有直接宾语之后有明确描述该宾语的身份、性质、动作或结果状态的宾语补足语时，才用 V-T-COMPLEX。"
        "修饰动作的时间、地点、方式等副词或介词短语不是宾补，不能仅因出现在宾语后就推荐复合及物。"
        f"推荐 V-T-COMPLEX 时，pattern 写成「目标词{OBJECT_COMPLEMENT_PATTERN}目标词 [具体宾语] [宾补])」。"
        "例如 make + object + object complement (e.g. make [them] [happy])，happy 描述宾语 them。"
        "只在例子与目标词及义项吻合，并能逐字使用以下范例之一时推荐 V-T-COMPLEX："
        f"{'；'.join(example for examples in OBJECT_COMPLEMENT_EXAMPLES.values() for example in examples)}。"
        "不同子类的句型应分别列出。"
        "没有系统选项或证据不足时省略 sub_pos，不自创分类。",
    ),
    "definitions": SectionAgent(
        Definition, "为各词义原创简洁英文定义及媒介语解释，两者表达同一含义。" + SCOPED_SENSE_RULE
    ),
    "equivalents": SectionAgent(
        Equivalent, "按具体词义提供自然、常用的媒介语译词。" + SCOPED_SENSE_RULE
    ),
    "relations": SectionAgent(
        Relation,
        "仅提出有语义依据的近义词、反义词或派生词及其词义。无合适关联则略去。" + SCOPED_SENSE_RULE,
    ),
    "dialect": SectionAgent(
        Dialect,
        "只在资料明确标注该词义限于英式或美式英语时给出 usage。evidence 原样摘取含地域标注的短语，source_locator 引用该来源。通用、仅音标不同或证据不足时返回空数组。",
    ),
    "examples": SectionAgent(
        Example, "为指定词义原创匹配等级的英文例句，使用目标词。" + SCOPED_SENSE_RULE
    ),
    "translations": SectionAgent(
        Translation, "为 examples 中的句子按索引提供忠实自然的媒介语译文。"
    ),
}


def plan(scope: Scope) -> list[Section]:
    selected = set(scope.sections)
    if "translations" in selected and not scope.examples:
        selected.add("examples")
    if selected - {"translations"} and not scope.existing_pos and not scope.senses:
        selected.add("forms")
    if selected - {"forms", "grammar", "translations"} and not scope.senses:
        selected.add("senses")
    return [section for section in AGENTS if section in selected]


def sense_list(scope: Scope, result: Proposals) -> list[SenseContext]:
    return [*scope.senses, *[SenseContext(pos=s.pos, gloss=s.gloss) for s in result.senses]]


def retain_grammar_sub_pos(row: Grammar, scope: Scope) -> None:
    if row.sub_pos is None:
        return
    if not any(option.code == row.sub_pos for option in scope.grammar_sub_pos.get(row.pos, [])):
        row.sub_pos = None
        return
    if row.sub_pos == "V-T-COMPLEX":
        headword = scope.headword.casefold()
        prefix = headword + OBJECT_COMPLEMENT_PATTERN
        pattern = row.pattern.casefold()
        if not any(
            pattern == f"{prefix}{example})"
            for example in OBJECT_COMPLEMENT_EXAMPLES.get(headword, ())
        ):
            row.sub_pos = None


def validate_rows(
    section: Section, raw: object, scope: Scope, result: Proposals
) -> list[dict[str, object]]:
    rows = AGENTS[section].parse(raw)
    senses = sense_list(scope, result)
    entry_pos = (
        set(scope.existing_pos) | {s.pos for s in scope.senses} | {p.pos for p in result.forms}
    )
    for row in rows:
        if isinstance(row, (Sense, Grammar)) and row.pos not in entry_pos:
            raise ValueError("missing_form_pos")
        if isinstance(row, BySense) and row.sense >= len(senses):
            raise ValueError("unknown_sense")
        if isinstance(row, Translation) and row.example >= len(scope.examples) + len(
            result.examples
        ):
            raise ValueError("unknown_example")
        if isinstance(row, (Sense, Grammar, CandidatePos)) and row.pos not in scope.catalog:
            raise ValueError("unknown_pos")
        if isinstance(row, Grammar):
            retain_grammar_sub_pos(row, scope)
        if isinstance(row, CandidatePos):
            kinds = [f.form_type for f in row.forms]
            if (
                len(set(kinds)) != len(kinds)
                or kinds.count("base") != 1
                or not set(kinds) <= set(scope.catalog[row.pos])
            ):
                raise ValueError("unknown_form")
            base = next(f for f in row.forms if f.form_type == "base")
            if scope.headword.casefold() not in {
                base.uk.spelling.casefold(),
                base.us.spelling.casefold(),
            }:
                raise ValueError("wrong_headword")
    return [row.model_dump() for row in rows]


class View(Execution[Section]):
    base_revision: int
    snapshot_id: str
    requires_human_review: Literal[True] = True
    proposals: Proposals
    sources: list[Material]
