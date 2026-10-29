import json
from copy import deepcopy
from uuid import uuid4

import pytest
from conftest import candidate
from test_review_regressions import completion, mocked_model
from test_validators import proposal

from tsz_agent.clients.tsz import TaskError
from tsz_agent.models.deepseek import DeepSeekModel
from tsz_agent.models.inputs import generation_input
from tsz_agent.schemas import ReviewEvent, ReviewExample


def test_projection_preserves_semantics_and_feedback_without_provenance(
    generation_data, context, pool
):
    rich = pool[0].model_copy(
        update={
            "other_definitions": ["结束；终点", "涂层"],
            "accepted_spellings": ["finish", "finishes"],
        }
    )
    generation_data["candidate_pool"][0] = rich.model_dump(mode="json")
    event = ReviewEvent(
        event_id=uuid4(),
        review_id=uuid4(),
        review_revision=2,
        source_run_id=uuid4(),
        question_revision_id=uuid4(),
        decision="rejected",
        reason_codes=["ambiguous"],
        comment="必须检查其他释义",
        example=ReviewExample(
            target=context.target,
            question_level="A1",
            prompt="开始",
            distractors=[rich, *pool[1:]],
            lesson="原题干可能存在第二答案；只适用于该案例。",
        ),
    )
    generation_data["reviewed_examples"] = [event.model_dump(mode="json")]
    result, references = generation_input(generation_data)
    assert result["candidate_pool"][0]["other_definitions"] == rich.other_definitions
    assert result["candidate_pool"][0]["accepted_spellings"] == rich.accepted_spellings
    example = result["reviewed_examples"][0]
    assert example["decision"] == "rejected" and example["reason_codes"] == ["ambiguous"]
    assert (
        example["comment"] == event.comment and example["example"]["lesson"] == event.example.lesson
    )
    assert example["example"]["distractors"][0]["other_definitions"] == rich.other_definitions
    assert references == {f"c{i:02}": c.candidate_ref for i, c in enumerate(pool, 1)}
    outbound = DeepSeekModel.prompt(result)
    assert str(event.review_id) not in outbound
    assert "publication_id" not in outbound and "content_fingerprint" not in outbound
    assert "external_request_id" not in outbound
    assert all(c.candidate_ref not in outbound for c in [context.target, *pool])
    # The projection never mutates the authoritative versioned checkpoint data.
    assert generation_data["candidate_pool"][0]["source"] == rich.model_dump(mode="json")["source"]


def test_task_changes_leave_candidate_prefix_stable_and_new_publications_use_new_references(
    generation_data,
):
    first, _ = generation_input(generation_data)
    changed = deepcopy(generation_data)
    changed["context"]["external_request_id"] = str(uuid4())
    changed["context"]["target"]["definition"] = "启程"
    second, _ = generation_input(changed)
    first_prompt, second_prompt = map(DeepSeekModel.prompt, [first, second])
    assert first_prompt.split(',"context":')[0] == second_prompt.split(',"context":')[0]
    assert first_prompt != second_prompt
    assert list(first) == ["candidate_pool", "context", "reviewed_examples"]
    # A changed meaning is sent immediately, even when its short number stays the same.
    changed["candidate_pool"][0]["definition"] = "全新释义"
    changed["candidate_pool"][0]["source"]["publication_id"] = str(uuid4())
    third, references = generation_input(changed)
    assert third["candidate_pool"][0]["definition"] == "全新释义"
    assert third["candidate_pool"] != second["candidate_pool"]
    assert references["c01"] == changed["candidate_pool"][0]["candidate_ref"]
    republished = deepcopy(generation_data)
    republished["candidate_pool"][0]["source"]["publication_id"] = str(uuid4())
    republished["candidate_pool"][0]["candidate_ref"] = "republished-candidate"
    fourth, references = generation_input(republished)
    assert fourth == first  # Identical semantics may reuse provider computation.
    assert references["c01"] == "republished-candidate"


async def test_supplemental_generation_uses_its_own_alias_mapping(settings, generation_data, pool):
    async def respond(request):
        return completion({"proposal": proposal(["c01", "c02", "c03"]).model_dump(mode="json")})

    model = await mocked_model(settings, respond)
    try:
        first = await model.generate(generation_data)
        supplemental = [candidate(i, f"word{i}", f"新释义{i}") for i in range(10, 13)]
        generation_data["candidate_pool"] = [c.model_dump(mode="json") for c in supplemental]
        second = await model.generate(generation_data)
        assert [c.candidate_ref for c in first.distractors] == [c.candidate_ref for c in pool]
        assert [c.candidate_ref for c in second.distractors] == [
            c.candidate_ref for c in supplemental
        ]
        assert model.usage().requests == 2 and model.usage().cost is None
    finally:
        await model.close()


@pytest.mark.parametrize(
    "refs", [["c01", "c02", "c99"], ["c01"] * 3, ["candidate_2", "c02", "c03"]]
)
async def test_invalid_model_aliases_are_rejected_but_usage_is_retained(
    settings, generation_data, refs
):
    async def respond(request):
        return completion({"proposal": proposal(refs).model_dump(mode="json")})

    model = await mocked_model(settings, respond)
    try:
        with pytest.raises(TaskError, match="invalid_model_selection"):
            await model.generate(generation_data)
        assert model.usage().requests == 1 and model.usage().input_tokens == 50
        assert len(model.budget.metrics) == 1
    finally:
        await model.close()


async def test_generation_schema_repair_keeps_mapping_and_accounts_for_each_call(
    settings, generation_data, pool
):
    calls = []

    async def respond(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return completion({"invalid": True})
        return completion({"proposal": proposal(["c01", "c02", "c03"]).model_dump(mode="json")})

    model = await mocked_model(settings, respond)
    try:
        result = await model.generate(generation_data)
        assert [c.candidate_ref for c in result.distractors] == [c.candidate_ref for c in pool]
        assert len(calls) == model.usage().requests == 2
        assert model.usage().input_tokens == 100 and model.usage().cost is None
        assert len(model.budget.metrics) == 2
        assert "publication_id" not in json.dumps(calls)
    finally:
        await model.close()


async def test_insufficient_candidates_does_not_require_alias_choices(settings, generation_data):
    async def respond(request):
        return completion(
            {"proposal": {"outcome": "insufficient_candidates", "reasons": ["候选无法成题"]}}
        )

    model = await mocked_model(settings, respond)
    try:
        result = await model.generate(generation_data)
        assert result.outcome == "insufficient_candidates"
        assert result.reasons == ["候选无法成题"]
    finally:
        await model.close()


@pytest.mark.integration
@pytest.mark.parametrize("failure", [None, "source_changed", "authorization_revoked"])
async def test_sdk_projection_round_trip_keeps_authority_checks(
    store, settings, context, pool, monkeypatch, failure
):
    from test_workflow import Source

    from tsz_agent.schemas import CreateRun
    from tsz_agent.storage import Run
    from tsz_agent.worker import run_once

    source = Source(context, pool)
    checks = []
    reviewed = False

    async def check_sources(request_id, candidates):
        checks.append(candidates)
        if reviewed and failure:
            raise TaskError(failure)

    monkeypatch.setattr(source, "check_sources", check_sources)

    async def respond(request):
        nonlocal reviewed
        if not calls:
            calls.append("generate")
            return completion({"proposal": proposal(["c01", "c02", "c03"]).model_dump(mode="json")})
        calls.append("review")
        reviewed = True
        return completion()

    calls = []
    model = await mocked_model(settings, respond)
    run = await store.create(CreateRun(external_request_id=context.external_request_id), uuid4())
    try:
        await run_once(settings, store, source, lambda _: model)
        result = await store.get(run.id)
        assert calls == ["generate", "review"]
        assert checks[0] == [context.target, *pool]
        assert {c.candidate_ref: c.source for c in checks[-1]} == {
            c.candidate_ref: c.source for c in [context.target, *pool]
        }
        async with store.sessions() as session:
            checkpoint = (await session.get(Run, run.id)).checkpoint
        assert checkpoint["prompt_version"] == "mcq_zh_en_v5"
        assert checkpoint["pool"] == [c.model_dump(mode="json") for c in pool]
        assert [c["candidate_ref"] for c in checkpoint["proposal"]["distractors"]] == [
            c.candidate_ref for c in pool
        ]
        if failure:
            assert result.state == "failed" and result.error_code == failure
            assert result.result is None
        else:
            question = result.result.question
            assert result.state == "succeeded" and question.requires_human_review is True
            assert {c.candidate.candidate_ref: c.candidate.source for c in question.options} == {
                c.candidate_ref: c.source for c in [context.target, *pool]
            }
    finally:
        await model.close()


async def test_large_semantic_payload_is_rejected_before_dispatch(settings, generation_data):
    generation_data["candidate_pool"][0]["other_definitions"] = ["义" * 2000] * 100

    async def forbidden(request):
        pytest.fail("oversized semantic content must not be silently truncated or dispatched")

    model = await mocked_model(settings, forbidden)
    try:
        with pytest.raises(TaskError, match="context_too_large"):
            await model.generate(generation_data)
        assert model.usage().requests == 0
    finally:
        await model.close()


def test_semantic_feedback_keeps_meanings_without_exposing_authoritative_identities(
    generation_data, pool
):
    data = {
        **generation_data,
        "semantic_feedback": {
            "issues": ["原选项仍可能正确"],
            "previous_distractors": [c.model_dump(mode="json") for c in pool],
        },
    }
    model_input, _ = generation_input(data)
    feedback = model_input["semantic_feedback"]
    assert feedback["issues"] == ["原选项仍可能正确"]
    assert [c["spelling"] for c in feedback["previous_distractors"]] == [c.spelling for c in pool]
    assert all(
        "source" not in c and "candidate_ref" not in c and "content_fingerprint" not in c
        for c in feedback["previous_distractors"]
    )
