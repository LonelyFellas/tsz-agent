from uuid import uuid4

import httpx
import pytest
from conftest import candidate
from sqlalchemy import text
from test_validators import proposal

from tsz_agent.clients.tsz import TaskError, TszClient
from tsz_agent.schemas import (
    CandidateSearch,
    CreateRun,
    EntriesRequest,
    InsufficientProposal,
    SemanticReview,
    Usage,
)
from tsz_agent.worker import run_once

pytestmark = pytest.mark.integration


class Model:
    def __init__(self, refs, *, on_review=None, fail_review=False):
        self.refs, self.on_review, self.fail_review = refs, on_review, fail_review
        self.generations, self.reviews = 0, 0
        self.inputs = []

    def usage(self):
        return Usage(requests=self.generations + self.reviews, input_tokens=100, output_tokens=50)

    async def generate(self, data):
        self.inputs.append(data)
        self.generations += 1
        return proposal(self.refs)

    async def review(self, data):
        self.reviews += 1
        if self.on_review:
            await self.on_review()
        if self.fail_review:
            self.fail_review = False
            raise TaskError("model_unavailable", retryable=True)
        return SemanticReview(verdict="needs_revision", issues=["应确认语义唯一性"])


class Source:
    def __init__(self, context, pool):
        self.context_value, self.pool = context, pool
        self.stale, self.denied = False, False

    async def context(self, request_id):
        if self.denied:
            raise TaskError("authorization_revoked")
        assert request_id == self.context_value.external_request_id
        return self.context_value

    async def candidates(self, request_id, context, exclude=None):
        return self.pool

    async def check_sources(self, request_id, candidates):
        if self.denied:
            raise TaskError("authorization_revoked")
        if self.stale:
            raise TaskError("source_changed")


async def test_complete_pipeline_preserves_sources_and_never_approves(
    store, settings, context, pool
):
    run = await store.create(CreateRun(external_request_id=context.external_request_id), uuid4())
    model = Model([c.candidate_ref for c in pool])
    await run_once(settings, store, Source(context, pool), lambda _: model)
    result = await store.get(run.id)
    assert result.state == "succeeded"
    question = result.result.question
    assert question.prompt == context.target.definition
    assert question.requires_human_review is True
    assert question.semantic_review.verdict == "needs_revision"
    assert {o.candidate.source.entry_id for o in question.options} == {
        c.source.entry_id for c in [context.target, *pool]
    }


@pytest.mark.parametrize(
    "condition,expected", [("stale", "source_changed"), ("denied", "authorization_revoked")]
)
async def test_source_or_permission_change_during_model_call_discards_result(
    store, settings, context, pool, condition, expected
):
    source = Source(context, pool)

    async def mutate():
        setattr(source, condition, True)

    run = await store.create(CreateRun(external_request_id=context.external_request_id), uuid4())
    model = Model([c.candidate_ref for c in pool], on_review=mutate)
    await run_once(settings, store, source, lambda _: model)
    result = await store.get(run.id)
    assert result.state == "failed"
    assert result.error_code == expected
    assert result.result is None


async def test_foreign_model_word_is_not_accepted(store, settings, context, pool):
    run = await store.create(CreateRun(external_request_id=context.external_request_id), uuid4())
    model = Model(["candidate_2", "candidate_3", "invented"])
    await run_once(settings, store, Source(context, pool), lambda _: model)
    result = await store.get(run.id)
    assert result.state == "failed"
    assert result.error_code == "invalid_model_selection"
    assert model.reviews == 0


async def test_insufficient_pool_does_not_invoke_model(store, settings, context, pool):
    run = await store.create(CreateRun(external_request_id=context.external_request_id), uuid4())

    def forbidden(_):
        pytest.fail("must not call model for insufficient candidates")

    await run_once(settings, store, Source(context, pool[:2]), forbidden)
    result = await store.get(run.id)
    assert result.state == "succeeded"
    assert result.result.outcome == "insufficient_candidates"


async def test_retry_reuses_saved_generation_and_rechecks_sources(store, settings, context, pool):
    run = await store.create(CreateRun(external_request_id=context.external_request_id), uuid4())
    model = Model([c.candidate_ref for c in pool], fail_review=True)
    source = Source(context, pool)
    await run_once(settings, store, source, lambda _: model)
    assert (await store.get(run.id)).state == "queued"
    async with store.sessions.begin() as session:
        await session.execute(text("UPDATE agent_runs SET next_attempt_at=now()"))
    await run_once(settings, store, source, lambda _: model)
    assert (await store.get(run.id)).state == "succeeded"
    assert model.generations == 1
    assert model.reviews == 2


async def test_tsz_client_rejects_identity_mismatch_and_redirect(context, pool):
    async def handle(request):
        if request.url.path.endswith("context"):
            return httpx.Response(
                200, json={**context.model_dump(mode="json"), "external_request_id": str(uuid4())}
            )
        return httpx.Response(302, headers={"location": "https://untrusted.test"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://tsz.test"
    ) as http:
        client = TszClient(http)
        with pytest.raises(TaskError, match="request_identity_mismatch"):
            await client.context(context.external_request_id)
        with pytest.raises(TaskError, match="source_contract_invalid"):
            await client.candidates(context.external_request_id, context)


async def test_pipeline_uses_typed_http_source_contract(store, settings, context, pool):

    known = {c.candidate_ref: c for c in [context.target, *pool]}
    calls = []

    async def handle(request):
        calls.append(request.url.path)
        prefix = f"/api/internal/agent/v1/requests/{context.external_request_id}/"
        assert request.url.path.startswith(prefix)
        if request.url.path.endswith("/context"):
            return httpx.Response(200, json=context.model_dump(mode="json"))
        if request.url.path.endswith("/candidates/search"):
            body = CandidateSearch.model_validate_json(request.content)
            assert body.target_source == context.target.source
            return httpx.Response(200, json={"items": [c.model_dump(mode="json") for c in pool]})
        body = EntriesRequest.model_validate_json(request.content)
        assert all(known[item.candidate_ref].source == item.source for item in body.items)
        return httpx.Response(
            200,
            json={
                "items": [known[item.candidate_ref].model_dump(mode="json") for item in body.items]
            },
        )

    run = await store.create(CreateRun(external_request_id=context.external_request_id), uuid4())
    model = Model([c.candidate_ref for c in pool])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://tsz.test"
    ) as http:
        await run_once(settings, store, TszClient(http), lambda _: model)
    assert (await store.get(run.id)).state == "succeeded"
    assert sum(path.endswith("/entries/batch-get") for path in calls) >= 2


async def test_semantic_insufficiency_recalls_once_and_resumes_saved_second_draft(
    store, settings, context, pool, monkeypatch
):
    extra = [candidate(i, f"word{i}", f"释义{i}") for i in range(10, 50)]
    searches, checks, example_reads = [], [], []
    original_examples = store.examples

    async def examples(ctx):
        example_reads.append(ctx)
        return await original_examples(ctx)

    monkeypatch.setattr(store, "examples", examples)

    class RecallSource(Source):
        async def candidates(self, request_id, ctx, exclude=None):
            searches.append(exclude)
            return extra if exclude else pool

        async def check_sources(self, request_id, candidates):
            assert len(candidates) <= 80
            checks.append([c.candidate_ref for c in candidates])

    class RecallModel(Model):
        async def generate(self, data):
            result = await super().generate(data)
            if self.generations == 1:
                return InsufficientProposal(reasons=["首批干扰项不合适"])
            assert [c["candidate_ref"] for c in data["candidate_pool"]] == [
                c.candidate_ref for c in extra
            ]
            assert extra[0].candidate_ref in checks[-1]
            return result

    run = await store.create(CreateRun(external_request_id=context.external_request_id), uuid4())
    model = RecallModel([c.candidate_ref for c in extra[:3]], fail_review=True)
    source = RecallSource(context, pool)
    await run_once(settings, store, source, lambda _: model)
    assert (await store.get(run.id)).state == "queued"
    async with store.sessions.begin() as session:
        await session.execute(text("UPDATE agent_runs SET next_attempt_at=now()"))
    await run_once(settings, store, source, lambda _: model)
    result = await store.get(run.id)
    assert result.state == "succeeded"
    assert result.result.question.requires_human_review
    assert model.generations == 2 and model.reviews == 2
    assert searches == [None, [c.candidate_ref for c in pool]]
    assert len(example_reads) == 2  # Exactly once per generation, none on review recovery.


@pytest.mark.parametrize("new_candidates", [False, True])
async def test_semantic_insufficiency_stops_after_one_supplement(
    store, settings, context, pool, new_candidates
):
    class RecallSource(Source):
        calls = 0

        async def candidates(self, request_id, ctx, exclude=None):
            self.calls += 1
            assert self.calls <= 2
            return [candidate(10, "resume", "继续")] if exclude and new_candidates else pool

    class InsufficientModel(Model):
        async def generate(self, data):
            self.generations += 1
            return InsufficientProposal(reasons=["没有足够的合适干扰项"])

    run = await store.create(CreateRun(external_request_id=context.external_request_id), uuid4())
    model = InsufficientModel([])
    source = RecallSource(context, pool)
    await run_once(settings, store, source, lambda _: model)
    result = await store.get(run.id)
    assert result.state == "succeeded"
    assert result.result.outcome == "insufficient_candidates"
    assert model.generations == (2 if new_candidates else 1)
    assert model.reviews == 0 and source.calls == 2


@pytest.mark.parametrize(
    "failure", ["source_changed", "authorization_revoked", "candidate_identity_conflict"]
)
async def test_supplement_rechecks_authority_and_all_first_page_identities(
    store, settings, context, pool, failure
):
    filtered = candidate(10, "resume", "继续", teaching_eligible=False)

    class RecallSource(Source):
        supplemented = False

        async def candidates(self, request_id, ctx, exclude=None):
            if not exclude:
                return [*pool, filtered] if failure == "candidate_identity_conflict" else pool
            self.supplemented = True
            if failure == "candidate_identity_conflict":
                assert filtered.candidate_ref in exclude
            return [filtered.model_copy(update={"teaching_eligible": True})]

        async def check_sources(self, request_id, candidates):
            if self.supplemented:
                raise TaskError(failure)

    class InsufficientModel(Model):
        async def generate(self, data):
            self.generations += 1
            return InsufficientProposal(reasons=["需要其他干扰项"])

    source = RecallSource(context, pool)
    run = await store.create(CreateRun(external_request_id=context.external_request_id), uuid4())
    model = InsufficientModel([])
    await run_once(settings, store, source, lambda _: model)
    result = await store.get(run.id)
    assert result.state == "failed" and result.error_code == failure
    assert result.result is None and model.generations == 1 and model.reviews == 0


async def test_hard_shortage_already_uses_the_only_supplement(store, settings, context, pool):
    class ShortSource(Source):
        calls = 0

        async def candidates(self, request_id, ctx, exclude=None):
            self.calls += 1
            assert self.calls <= 2
            return pool[2:] if exclude else pool[:2]

    class InsufficientModel(Model):
        async def generate(self, data):
            self.generations += 1
            return InsufficientProposal(reasons=["干扰不足"])

    run = await store.create(CreateRun(external_request_id=context.external_request_id), uuid4())
    model = InsufficientModel([])
    source = ShortSource(context, pool)
    await run_once(settings, store, source, lambda _: model)
    assert (await store.get(run.id)).result.outcome == "insufficient_candidates"
    assert source.calls == 2 and model.generations == 1


@pytest.mark.parametrize("version", ["mcq_zh_en_v2", "mcq_zh_en_v3"])
async def test_previous_workflow_checkpoint_fails_without_model_calls(
    store, settings, context, pool, version
):
    from tsz_agent.storage import Run

    run = await store.create(CreateRun(external_request_id=context.external_request_id), uuid4())
    async with store.sessions.begin() as session:
        row = await session.get(Run, run.id)
        row.checkpoint = {"prompt_version": version}
    model = Model([])
    await run_once(settings, store, Source(context, pool), lambda _: model)
    assert (await store.get(run.id)).error_code == "workflow_version_changed"
    assert model.generations == 0 and model.reviews == 0
    async with store.sessions() as session:
        assert (await session.get(Run, run.id)).checkpoint == {"prompt_version": version}
