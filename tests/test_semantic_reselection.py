from copy import deepcopy
from types import SimpleNamespace

import pytest
from conftest import candidate
from test_validators import proposal

from tsz_agent.capabilities.vocabulary_mcq.workflow import execute
from tsz_agent.clients.tsz import TaskError
from tsz_agent.schemas import InsufficientProposal, SemanticReview, Usage


class Sources:
    def __init__(self, context, pool):
        self.value, self.pool = context, pool
        self.stale = False

    async def context(self, _):
        if self.stale:
            raise TaskError("source_changed")
        return self.value

    async def candidates(self, *args):
        return self.pool

    async def check_sources(self, *args):
        if self.stale:
            raise TaskError("source_changed")


class Examples:
    async def examples(self, _):
        return []


class Model:
    def __init__(self, proposals, reviews):
        self.proposals, self.reviews = iter(proposals), iter(reviews)
        self.inputs, self.calls = [], 0

    def usage(self):
        return Usage(requests=self.calls)

    async def generate(self, data):
        self.calls += 1
        self.inputs.append(deepcopy(data))
        value = next(self.proposals)
        if isinstance(value, Exception):
            raise value
        return value

    async def review(self, data):
        self.calls += 1
        value = next(self.reviews)
        if isinstance(value, Exception):
            raise value
        return value


BAD = SemanticReview(verdict="needs_revision", issues=["选项 finish 也匹配题干"])
GOOD = SemanticReview(verdict="no_obvious_issue", issues=[])


async def run(context, pool, model, *, checkpoint=None, save=None, request_limit=4):
    async def noop(*_):
        pass

    return await execute(
        SimpleNamespace(
            id=context.external_request_id,
            external_request_id=context.external_request_id,
            checkpoint=checkpoint or {},
        ),
        Examples(),
        Sources(context, pool),
        lambda: model,
        save or noop,
        request_limit=request_limit,
    )


async def test_review_failure_reselects_then_reviews_without_changing_target(context, pool):
    extra = candidate(5, "lend", "借出")
    model = Model(
        [
            proposal([c.candidate_ref for c in pool]),
            proposal([c.candidate_ref for c in [pool[1], pool[2], extra]]),
        ],
        [BAD, GOOD],
    )
    result = await run(context, [*pool, extra], model)
    assert result.outcome == "draft"
    assert result.question.target == context.target
    assert result.question.prompt == context.target.definition
    assert result.question.semantic_review == GOOD
    assert extra in [o.candidate for o in result.question.options]
    assert model.calls == 4
    assert model.inputs[1]["semantic_feedback"]["issues"] == BAD.issues


async def test_unresolved_second_review_returns_shortage_without_a_question(context, pool):
    extra = candidate(5, "lend", "借出")
    model = Model(
        [
            proposal([c.candidate_ref for c in pool]),
            proposal([c.candidate_ref for c in [pool[1], pool[2], extra]]),
        ],
        [BAD, BAD],
    )
    result = await run(context, [*pool, extra], model)
    assert result.outcome == "insufficient_candidates" and result.question is None
    assert any("语义" in reason or "歧义" in reason for reason in result.reasons)
    assert model.calls == 4


async def test_reselection_that_repeats_same_options_is_not_a_fixed_question(context, pool):
    p = proposal([c.candidate_ref for c in pool])
    model = Model([p, p], [BAD, GOOD])
    result = await run(context, pool, model)
    assert result.outcome == "insufficient_candidates" and result.question is None
    assert model.calls == 3


async def test_reselection_can_report_no_appropriate_candidates(context, pool):
    model = Model(
        [
            proposal([c.candidate_ref for c in pool]),
            InsufficientProposal(reasons=["候选都与答案同义"]),
        ],
        [BAD],
    )
    result = await run(context, pool, model)
    assert result.outcome == "insufficient_candidates"
    assert "候选都与答案同义" in result.reasons


async def test_review_budget_exhaustion_never_releases_the_ambiguous_draft(context, pool):
    model = Model([proposal([c.candidate_ref for c in pool])], [BAD])
    result = await run(context, pool, model, request_limit=2)
    assert result.outcome == "insufficient_candidates" and result.question is None
    assert model.calls == 2


async def test_retry_budget_reservation_failure_returns_shortage(context, pool):
    model = Model(
        [proposal([c.candidate_ref for c in pool]), TaskError("model_budget_exceeded")], [BAD]
    )
    result = await run(context, pool, model)
    assert result.outcome == "insufficient_candidates" and result.question is None


async def test_resume_reuses_saved_second_proposal_and_review(context, pool):
    extra = candidate(5, "lend", "借出")
    model = Model(
        [
            proposal([c.candidate_ref for c in pool]),
            proposal([c.candidate_ref for c in [pool[1], pool[2], extra]]),
        ],
        [BAD, GOOD],
    )
    checkpoints = []

    async def interrupt(stage, checkpoint):
        checkpoints.append(deepcopy(checkpoint))
        if stage == "reviewed" and model.calls == 4:
            raise TaskError("test_interruption")

    with pytest.raises(TaskError, match="test_interruption"):
        await run(context, [*pool, extra], model, save=interrupt)
    result = await run(context, [*pool, extra], model, checkpoint=checkpoints[-1])
    assert result.outcome == "draft" and result.question.semantic_review == GOOD
    assert model.calls == 4


async def test_prompt_revision_request_cannot_escape_as_a_clean_draft(context, pool):
    p = proposal([c.candidate_ref for c in pool]).model_copy(
        update={"needs_prompt_revision": True, "suggested_prompt": "加一个原题干没有的条件"}
    )
    model = Model([p], [GOOD])
    result = await run(context, pool, model, request_limit=2)
    assert result.outcome == "insufficient_candidates" and result.question is None


async def test_second_review_budget_failure_cannot_release_an_unreviewed_reselection(context, pool):
    extra = candidate(5, "lend", "借出")
    model = Model(
        [
            proposal([c.candidate_ref for c in pool]),
            proposal([c.candidate_ref for c in [pool[1], pool[2], extra]]),
        ],
        [BAD, TaskError("model_budget_exceeded")],
    )
    result = await run(context, [*pool, extra], model)
    assert result.outcome == "insufficient_candidates" and result.question is None
    assert any("预算" in reason for reason in result.reasons)
