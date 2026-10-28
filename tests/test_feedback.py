from uuid import uuid4

import pytest
from test_workflow import Model, Source

from tsz_agent.capabilities.vocabulary_mcq.workflow import knowledge
from tsz_agent.schemas import CreateRun, ReviewEvent, ReviewExample
from tsz_agent.storage import Conflict
from tsz_agent.worker import run_once

pytestmark = pytest.mark.integration


async def generate(store, settings, context, pool):
    run = await store.create(CreateRun(external_request_id=context.external_request_id), uuid4())
    model = Model([c.candidate_ref for c in pool])
    await run_once(settings, store, Source(context, pool), lambda _: model)
    return run


def approved(run, context, pool):
    return ReviewEvent(
        event_id=uuid4(),
        review_id=uuid4(),
        review_revision=1,
        source_run_id=run.id,
        question_revision_id=uuid4(),
        decision="approved",
        reason_codes=[],
        example=ReviewExample(
            target=context.target,
            question_level="A1",
            prompt="开始",
            distractors=pool,
            lesson="保持原题干唯一性",
        ),
    )


async def test_feedback_idempotency_conflict_and_revocation_cannot_be_reversed_by_old_delivery(
    store, settings, context, pool
):
    run = await generate(store, settings, context, pool)
    first = approved(run, context, pool)
    assert await store.feedback(first)
    assert not await store.feedback(first)
    with pytest.raises(Conflict, match="event_id_conflict"):
        await store.feedback(first.model_copy(update={"comment": "changed"}))
    revoked = first.model_copy(
        update={"event_id": uuid4(), "review_revision": 3, "decision": "revoked", "example": None}
    )
    assert await store.feedback(revoked)
    late = first.model_copy(update={"event_id": uuid4(), "review_revision": 2})
    assert not await store.feedback(late)
    assert await store.examples() == []


async def test_rejected_feedback_uses_actual_question_and_is_learned_as_negative(
    store, settings, context, pool
):
    run = await generate(store, settings, context, pool)
    event = ReviewEvent(
        event_id=uuid4(),
        review_id=uuid4(),
        review_revision=1,
        source_run_id=run.id,
        question_revision_id=uuid4(),
        decision="rejected",
        reason_codes=["weak_distractor"],
        comment="wait干扰太弱，需要同类的动作进程词",
    )
    await store.feedback(event)
    examples = await knowledge(store, Source(context, pool), context)
    assert len(examples) == 1
    assert examples[0].decision == "rejected"
    assert examples[0].example.prompt == "开始"
    assert "wait干扰太弱" in examples[0].example.lesson
    new_context = context.model_copy(update={"external_request_id": uuid4()})
    model = Model([c.candidate_ref for c in pool])
    new_run = await store.create(
        CreateRun(external_request_id=new_context.external_request_id), uuid4()
    )
    await run_once(settings, store, Source(new_context, pool), lambda _: model)
    assert model.inputs[0]["reviewed_examples"][0]["decision"] == "rejected"
    assert (await store.get(new_run.id)).result.question.requires_human_review


async def test_stale_examples_not_retrieved_and_feedback_cannot_rebind_target(
    store, settings, context, pool
):
    run = await generate(store, settings, context, pool)
    event = approved(run, context, pool)
    await store.feedback(event)
    stale = Source(context, pool)
    stale.stale = True
    assert await knowledge(store, stale, context) == []
    wrong = event.model_copy(
        update={
            "event_id": uuid4(),
            "review_id": uuid4(),
            "example": event.example.model_copy(update={"target": pool[0]}),
        }
    )
    with pytest.raises(Conflict, match="feedback_target_mismatch"):
        await store.feedback(wrong)


async def test_only_one_payload_per_review_revision(store, settings, context, pool):
    run = await generate(store, settings, context, pool)
    event = approved(run, context, pool)
    await store.feedback(event)
    with pytest.raises(Conflict, match="review_revision_conflict"):
        await store.feedback(event.model_copy(update={"event_id": uuid4()}))


@pytest.mark.parametrize("first_is_revocation", [False, True])
async def test_review_cannot_change_question_identity_even_for_out_of_order_events(
    store, settings, context, pool, first_is_revocation
):
    run = await generate(store, settings, context, pool)
    event = approved(run, context, pool)
    if first_is_revocation:
        event = event.model_copy(
            update={"decision": "revoked", "example": None, "review_revision": 3}
        )
    assert await store.feedback(event)
    other = event.model_copy(
        update={
            "event_id": uuid4(),
            "question_revision_id": uuid4(),
            "review_revision": 1 if first_is_revocation else 2,
        }
    )
    with pytest.raises(Conflict, match="review_question_revision_mismatch"):
        await store.feedback(other)
    # Conflict does not consume the event identity or change the existing knowledge.
    same = other.model_copy(update={"question_revision_id": event.question_revision_id})
    assert await store.feedback(same) is (not first_is_revocation)
    assert bool(await store.examples()) is (not first_is_revocation)
