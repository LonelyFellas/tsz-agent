from collections.abc import Awaitable, Callable
from typing import Any

from tsz_agent.clients.tsz import TaskError, TszClient
from tsz_agent.models.deepseek import PROMPT_VERSION, QuestionModel
from tsz_agent.schemas import (
    Candidate,
    DraftProposal,
    GeneratedQuestion,
    GenerationContext,
    InsufficientProposal,
    OptionId,
    QuestionOption,
    ReviewEvent,
    RunResult,
    SemanticReview,
)
from tsz_agent.storage import Run, Store

from .validators import selected, valid_pool

Save = Callable[[str, dict[str, Any]], Awaitable[None]]


async def knowledge(store: Store, tsz: TszClient, context: GenerationContext) -> list[ReviewEvent]:
    events = await store.examples()
    # Specific feedback first; then same part-of-speech and level, bounded context.
    events.sort(
        key=lambda e: (
            e.example is None or e.example.target.source.sense_id != context.target.source.sense_id,
            -e.review_revision,
        )
    )
    chosen = []
    for event in events:
        example = event.example
        if example is None or example.target.part_of_speech != context.target.part_of_speech:
            continue
        if example.question_level != context.question_level:
            continue
        try:
            await tsz.check_sources(
                context.external_request_id, [example.target, *example.distractors]
            )
        except TaskError as exc:
            if exc.code == "source_changed":
                continue
            if exc.code == "authorization_revoked":
                current = await tsz.context(context.external_request_id)
                if current != context:
                    raise TaskError("source_changed") from exc
                continue
            raise
        chosen.append(event)
        if len(chosen) == 4:
            break
    return chosen


async def execute(
    run: Run,
    store: Store,
    tsz: TszClient,
    model_factory: Callable[[], QuestionModel],
    save: Save,
) -> RunResult:
    checkpoint = dict(run.checkpoint)
    if checkpoint and checkpoint.get("prompt_version") != PROMPT_VERSION:
        raise TaskError("workflow_version_changed")
    context = await tsz.context(run.external_request_id)
    context_json = context.model_dump(mode="json")
    if "context" in checkpoint and checkpoint["context"] != context_json:
        raise TaskError("source_changed")
    checkpoint.update(prompt_version=PROMPT_VERSION, context=context_json)

    if "pool" not in checkpoint:
        candidates = await tsz.candidates(run.external_request_id, context)
        pool = valid_pool(context, candidates)
        if len(pool) < 3:
            extra = await tsz.candidates(
                run.external_request_id, context, [c.candidate_ref for c in candidates]
            )
            pool = valid_pool(context, [*candidates, *extra])
        if len(pool) < 3:
            await tsz.check_sources(run.external_request_id, [context.target])
            return RunResult(
                outcome="insufficient_candidates",
                reasons=["词库内同词性、等级合适且不重复的候选不足三个"],
            )
        examples = await knowledge(store, tsz, context)
        checkpoint.update(
            pool=[c.model_dump(mode="json") for c in pool],
            examples=[e.model_dump(mode="json") for e in examples],
        )
        await save("sources_ready", checkpoint)
    pool = [Candidate.model_validate(c) for c in checkpoint["pool"]]
    await tsz.check_sources(run.external_request_id, [context.target, *pool])

    model = model_factory()
    if "proposal" not in checkpoint:
        # Refresh active feedback at execution time. It is not an unbounded chat history.
        examples = await knowledge(store, tsz, context)
        checkpoint["examples"] = [e.model_dump(mode="json") for e in examples]
        await save("generating", checkpoint)
        proposal = await model.generate(
            {
                "context": context_json,
                "candidate_pool": checkpoint["pool"],
                "reviewed_examples": checkpoint["examples"],
            }
        )
        if isinstance(proposal, DraftProposal):
            selected(context, pool, proposal)
        checkpoint["proposal"] = proposal.model_dump(mode="json")
        await save("generated", checkpoint)
    raw_proposal = checkpoint["proposal"]
    proposal = (
        DraftProposal.model_validate(raw_proposal)
        if raw_proposal.get("outcome") == "draft"
        else InsufficientProposal.model_validate(raw_proposal)
    )
    if isinstance(proposal, InsufficientProposal):
        await tsz.check_sources(run.external_request_id, [context.target, *pool])
        return RunResult(outcome="insufficient_candidates", reasons=proposal.reasons)

    distractors = selected(context, pool, proposal)
    # Fixed by run ID, stable across retries; the model does not choose correct position.
    candidates = list(distractors)
    correct_index = run.id.int % 4
    candidates.insert(correct_index, context.target)
    ids: tuple[OptionId, ...] = ("A", "B", "C", "D")
    options = [QuestionOption(option_id=ids[i], candidate=c) for i, c in enumerate(candidates)]
    if "semantic_review" not in checkpoint:
        await save("reviewing", checkpoint)
        review = await model.review(
            {
                "original_prompt": context.target.definition,
                "part_of_speech": context.target.part_of_speech,
                "question_level": context.question_level,
                "options": [o.model_dump(mode="json") for o in options],
                "intended_answer": context.target.spelling,
                "suggested_prompt": proposal.suggested_prompt,
            }
        )
        checkpoint["semantic_review"] = review.model_dump(mode="json")
        await save("reviewed", checkpoint)
    await tsz.check_sources(run.external_request_id, candidates)
    # A fresh delegated read catches permission revocation and target changes near completion.
    if (await tsz.context(run.external_request_id)) != context:
        raise TaskError("source_changed")
    question = GeneratedQuestion(
        target=context.target,
        prompt=context.target.definition,
        question_level=context.question_level,
        answer_profile=context.answer_profile,
        options=options,
        correct_option_id=ids[correct_index],
        proposal=proposal,
        semantic_review=SemanticReview.model_validate(checkpoint["semantic_review"]),
    )
    return RunResult(outcome="draft", question=question)
