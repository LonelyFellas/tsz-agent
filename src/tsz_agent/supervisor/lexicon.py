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
Level = Literal["A1", "A2", "B1", "B2", "C1", "C2"]
Index = Annotated[int, Field(ge=0, lt=100)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SenseContext(Strict):
    pos: Name
    gloss: Annotated[str, Field(max_length=500)]


class Scope(Strict):
    run_id: UUID
    entry_id: UUID
    actor_id: UUID
    base_revision: Annotated[int, Field(ge=1)]
    headword: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    medium: Literal["zh-Hans", "zh-Hant"]
    target: Literal["en-GB", "en-US"]
    sections: Annotated[list[Section], Field(min_length=1, max_length=10)]
    senses: Annotated[list[SenseContext], Field(max_length=80)]
    existing_pos: Annotated[list[Name], Field(max_length=60)]
    examples: Annotated[list[Text], Field(max_length=50)]
    catalog: Annotated[dict[Name, list[Name]], Field(min_length=1, max_length=60)]
    snapshot_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


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
    "grammar": SectionAgent(Grammar, "按词性给出常见语法结构，pattern 含目标词。"),
    "definitions": SectionAgent(Definition, "为各词义原创简洁英文定义及媒介语解释。"),
    "equivalents": SectionAgent(Equivalent, "按具体词义提供自然、常用的媒介语译词。"),
    "relations": SectionAgent(
        Relation, "仅提出有语义依据的近义词、反义词或派生词及其词义。无合适关联则略去。"
    ),
    "dialect": SectionAgent(
        Dialect,
        "只在资料明确标注该词义限于英式或美式英语时给出 usage。evidence 原样摘取含地域标注的短语，source_locator 引用该来源。通用、仅音标不同或证据不足时返回空数组。",
    ),
    "examples": SectionAgent(Example, "为指定词义原创匹配等级的英文例句，使用目标词。"),
    "translations": SectionAgent(
        Translation, "为 examples 中的句子按索引提供忠实自然的媒介语译文。"
    ),
}


def plan(scope: Scope) -> list[Section]:
    selected = set(scope.sections)
    if not scope.existing_pos and not scope.senses:
        selected.add("forms")
    if "translations" in selected and not scope.examples:
        selected.add("examples")
    if selected - {"forms", "grammar"} and not scope.senses:
        selected.add("senses")
    return [section for section in AGENTS if section in selected]


def sense_list(scope: Scope, result: Proposals) -> list[SenseContext]:
    return [*scope.senses, *[SenseContext(pos=s.pos, gloss=s.gloss) for s in result.senses]]


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
