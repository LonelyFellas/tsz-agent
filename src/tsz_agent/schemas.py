from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

Level = Literal["A1", "A2", "B1", "B2", "C1", "C2"]
Profile = Literal["BrE", "AmE"]
OptionId = Literal["A", "B", "C", "D"]
Capability = Literal["vocabulary_mcq_zh_en"]
RunState = Literal["queued", "running", "succeeded", "failed", "cancelled"]
Text = Annotated[str, Field(min_length=1, max_length=2000)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProblemDetails(StrictModel):
    type: str
    title: str
    status: int
    detail: str
    code: str


class ReviewReceipt(StrictModel):
    applied: bool


class SourceRef(StrictModel):
    entry_id: UUID
    publication_id: UUID
    pos_id: UUID
    sense_id: UUID
    definition_id: UUID
    form_id: UUID


class Candidate(StrictModel):
    candidate_ref: Annotated[str, Field(min_length=1, max_length=80)]
    source: SourceRef
    content_fingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    spelling: Annotated[str, Field(min_length=1, max_length=200)]
    part_of_speech: Annotated[str, Field(min_length=1, max_length=50)]
    level: Level
    definition: Text
    other_definitions: list[Text] = Field(default_factory=list, max_length=100)
    accepted_spellings: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=30
    )
    answer_profile: Profile
    form_type: Literal["base"] = "base"
    teaching_eligible: bool


class GenerationContext(StrictModel):
    contract_version: Literal[1] = 1
    external_request_id: UUID
    target: Candidate
    question_level: Level
    answer_profile: Profile

    @model_validator(mode="after")
    def valid_target(self) -> "GenerationContext":
        if not self.target.teaching_eligible or self.target.answer_profile != self.answer_profile:
            raise ValueError("target must be teaching eligible and match answer_profile")
        return self


class CandidatePage(StrictModel):
    contract_version: Literal[1] = 1
    items: list[Candidate] = Field(max_length=80)


class CandidateSearch(StrictModel):
    contract_version: Literal[1] = 1
    target_source: SourceRef
    question_level: Level
    answer_profile: Profile
    exclude_candidate_refs: list[str] = Field(default_factory=list, max_length=80)
    limit: Literal[40] = 40


class EntryRead(StrictModel):
    candidate_ref: str
    source: SourceRef
    answer_profile: Profile


class EntriesRequest(StrictModel):
    contract_version: Literal[1] = 1
    items: list[EntryRead] = Field(min_length=1, max_length=80)


class CreateRun(StrictModel):
    contract_version: Literal[1] = 1
    external_request_id: UUID
    capability: Capability = "vocabulary_mcq_zh_en"


class DistractorChoice(StrictModel):
    candidate_ref: Annotated[str, Field(min_length=1, max_length=80)]
    interference_type: Literal["semantic", "orthographic", "phonetic"]
    why_plausible: Text
    why_wrong: Text


class DraftProposal(StrictModel):
    outcome: Literal["draft"] = "draft"
    distractors: list[DistractorChoice] = Field(min_length=3, max_length=3)
    needs_prompt_revision: bool
    suggested_prompt: Text | None
    concerns: list[Text] = Field(max_length=12)

    @model_validator(mode="after")
    def consistent_revision(self) -> "DraftProposal":
        if self.needs_prompt_revision != (self.suggested_prompt is not None):
            raise ValueError("suggested_prompt must accompany needs_prompt_revision")
        return self


class InsufficientProposal(StrictModel):
    outcome: Literal["insufficient_candidates"] = "insufficient_candidates"
    reasons: list[Text] = Field(min_length=1, max_length=12)


Proposal = Annotated[DraftProposal | InsufficientProposal, Field(discriminator="outcome")]


class SemanticReview(StrictModel):
    verdict: Literal["no_obvious_issue", "needs_revision"]
    issues: list[Text] = Field(max_length=12)

    @model_validator(mode="after")
    def issues_match_verdict(self) -> "SemanticReview":
        if (self.verdict == "needs_revision") != bool(self.issues):
            raise ValueError("review verdict and issues disagree")
        return self


class QuestionOption(StrictModel):
    option_id: Literal["A", "B", "C", "D"]
    candidate: Candidate


class GeneratedQuestion(StrictModel):
    target: Candidate
    prompt: Text
    question_level: Level
    answer_profile: Profile
    options: list[QuestionOption] = Field(min_length=4, max_length=4)
    correct_option_id: Literal["A", "B", "C", "D"]
    proposal: DraftProposal
    semantic_review: SemanticReview
    requires_human_review: Literal[True] = True

    @model_validator(mode="after")
    def correct_source(self) -> "GeneratedQuestion":
        if {o.option_id for o in self.options} != {"A", "B", "C", "D"}:
            raise ValueError("four distinct option IDs required")
        correct = next(o for o in self.options if o.option_id == self.correct_option_id)
        if correct.candidate != self.target:
            raise ValueError("correct option must be the authoritative target")
        return self


class RunResult(StrictModel):
    outcome: Literal["draft", "insufficient_candidates"]
    question: GeneratedQuestion | None = None
    reasons: list[Text] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def coherent(self) -> "RunResult":
        if self.outcome == "draft" and (self.question is None or self.reasons):
            raise ValueError("draft requires a question and no reasons")
        if self.outcome == "insufficient_candidates" and (self.question or not self.reasons):
            raise ValueError("insufficient_candidates requires reasons and no question")
        return self


class Usage(StrictModel):
    model: str = "deepseek-flash"
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # None means unknown, never a fabricated zero-cost receipt.
    cost: str | None = None


class RunView(StrictModel):
    contract_version: Literal[1] = 1
    id: UUID
    external_request_id: UUID
    capability: Capability
    state: RunState
    stage: str
    attempts: int
    error_code: str | None
    result: RunResult | None
    usage: Usage
    created_at: datetime
    updated_at: datetime


class ReviewExample(StrictModel):
    target: Candidate
    question_level: Level
    prompt: Text
    distractors: list[Candidate] = Field(min_length=3, max_length=3)
    lesson: Text


class ReviewEvent(StrictModel):
    contract_version: Literal[1] = 1
    event_id: UUID
    review_id: UUID
    review_revision: int = Field(ge=1)
    source_run_id: UUID
    question_revision_id: UUID
    capability: Capability = "vocabulary_mcq_zh_en"
    decision: Literal["approved", "edited_and_approved", "rejected", "revoked"]
    reason_codes: list[
        Literal[
            "ambiguous",
            "weak_distractor",
            "level_mismatch",
            "source_error",
            "prompt_unclear",
            "explanation_error",
            "other",
        ]
    ] = Field(max_length=7)
    comment: Annotated[str, Field(max_length=2000)] = ""
    example: ReviewExample | None = None

    @model_validator(mode="after")
    def decision_content(self) -> "ReviewEvent":
        if self.decision in {"approved", "edited_and_approved"} and self.example is None:
            raise ValueError("approved feedback requires a reviewed example")
        if self.decision == "revoked" and self.example is not None:
            raise ValueError("revoked feedback cannot contain an active example")
        if self.decision == "rejected" and not self.reason_codes:
            raise ValueError("rejected feedback requires a reason")
        return self
