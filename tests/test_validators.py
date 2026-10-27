from uuid import uuid4

import pytest
from conftest import candidate
from pydantic import ValidationError

from tsz_agent.capabilities.vocabulary_mcq.validators import selected, valid_pool
from tsz_agent.clients.tsz import TaskError
from tsz_agent.schemas import DraftProposal, ReviewEvent, SemanticReview


def proposal(refs):
    return DraftProposal(
        distractors=[
            dict(
                candidate_ref=r,
                interference_type="semantic",
                why_plausible="相关动作",
                why_wrong="含义不同",
            )
            for r in refs
        ],
        needs_prompt_revision=False,
        suggested_prompt=None,
        concerns=[],
    )


def test_pool_excludes_synonym_variant_wrong_pos_level_and_nonteaching(context, pool):
    extra = [
        candidate(10, "start", "开始"),
        candidate(11, "BEＧIN", "其他"),
        candidate(12, "beginning", "开端", part_of_speech="noun"),
        candidate(13, "inaugurate", "创始", level="C1"),
        candidate(14, "probe", "测试", teaching_eligible=False),
        candidate(15, "launch", "发射", other_definitions=["开始"]),
        candidate(16, "alias", "别的", accepted_spellings=["begin"]),
    ]
    assert valid_pool(context, pool + extra) == pool


def test_unknown_and_duplicate_candidate_refs_rejected(context, pool):
    for refs in [["candidate_2", "candidate_3", "invented"], ["candidate_2"] * 3]:
        with pytest.raises(TaskError, match="invalid_model_selection"):
            selected(context, pool, proposal(refs))


def test_changed_ref_cannot_rebind_another_source(context, pool):
    forged = candidate(50, "invented", "伪造", candidate_ref=pool[0].candidate_ref)
    with pytest.raises(TaskError, match="candidate_identity_conflict"):
        valid_pool(context, [*pool, forged])


def test_ambiguous_other_meanings_are_not_silently_proved_safe(context, pool):
    # Exact text filtering cannot prove semantic uniqueness: the model/human must inspect it.
    broad = candidate(50, "commence", "着手；开始进行")
    assert broad in valid_pool(context, [*pool, broad])


def test_revision_suggestions_and_review_issues_must_be_explicit():
    with pytest.raises(ValidationError):
        DraftProposal(**{**proposal(["a", "b", "c"]).model_dump(), "suggested_prompt": "味觉"})
    with pytest.raises(ValidationError):
        SemanticReview(verdict="no_obvious_issue", issues=["可能双解"])
    with pytest.raises(ValidationError):
        ReviewEvent(
            event_id=uuid4(),
            review_id=uuid4(),
            review_revision=1,
            source_run_id=uuid4(),
            question_revision_id=uuid4(),
            decision="approved",
        )
