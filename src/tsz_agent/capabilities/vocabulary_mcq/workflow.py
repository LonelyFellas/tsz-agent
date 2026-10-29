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
from tsz_agent.storage import Run, Store, digest

from .validators import selected, valid_pool

Save = Callable[[str, dict[str, Any]], Awaitable[None]]


async def knowledge(store: Store, tsz: TszClient, context: GenerationContext) -> list[ReviewEvent]:
    # Filter/rank before limiting, including old feedback for this exact sense.
    events = await store.examples(context)
    chosen = []
    for event in events:
        example = event.example
        if example is None:
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
    *,
    request_limit: int,
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
        searches = 1
        pool = valid_pool(context, candidates)
        if len(pool) < 3:
            extra = await tsz.candidates(
                run.external_request_id, context, [c.candidate_ref for c in candidates]
            )
            searches += 1
            pool = valid_pool(context, [*candidates, *extra])[:40]
            candidates = [*candidates, *extra]
        if len(pool) < 3:
            await tsz.check_sources(run.external_request_id, [context.target])
            return RunResult(
                outcome="insufficient_candidates",
                reasons=["词库内同词性、等级合适且不重复的候选不足三个"],
            )
        checkpoint.update(
            pool=[c.model_dump(mode="json") for c in pool],
            searches=searches,
            candidate_hashes={
                c.candidate_ref: digest(c.model_dump(mode="json")) for c in candidates
            },
        )
        await save("sources_ready", checkpoint)
    pool = [Candidate.model_validate(c) for c in checkpoint["pool"]]
    await tsz.check_sources(run.external_request_id, [context.target, *pool])

    model = model_factory()
    while True:
        while True:
            if "proposal" in checkpoint:
                raw = checkpoint["proposal"]
                proposal = (
                    DraftProposal.model_validate(raw)
                    if raw.get("outcome") == "draft"
                    else InsufficientProposal.model_validate(raw)
                )
                if (
                    isinstance(proposal, DraftProposal)
                    or checkpoint["searches"] >= 2
                    or model.usage().requests + 2 > request_limit
                ):
                    break
                extra = await tsz.candidates(
                    run.external_request_id, context, list(checkpoint["candidate_hashes"])
                )
                # Check all returned identities, including candidates filtered out of the first pool.
                for candidate in extra:
                    previous = checkpoint["candidate_hashes"].get(candidate.candidate_ref)
                    if previous is not None and previous != digest(
                        candidate.model_dump(mode="json")
                    ):
                        raise TaskError("candidate_identity_conflict")
                valid_pool(context, [*pool, *extra])
                refreshed = valid_pool(context, [*extra, *pool])[:40]
                checkpoint["searches"] = 2
                if len(refreshed) < 3 or not {c.candidate_ref for c in refreshed} - {
                    c.candidate_ref for c in pool
                }:
                    await save("generated", checkpoint)
                    break
                pool = refreshed
                checkpoint["pool"] = [c.model_dump(mode="json") for c in pool]
                del checkpoint["proposal"]
                await save("sources_ready", checkpoint)
                await tsz.check_sources(run.external_request_id, [context.target, *pool])
            # Refresh active feedback at execution time. It is not an unbounded chat history.
            examples = await knowledge(store, tsz, context)
            checkpoint["examples"] = [e.model_dump(mode="json") for e in examples]
            await save("generating", checkpoint)
            try:
                proposal = await model.generate(
                    {
                        "context": context_json,
                        "candidate_pool": checkpoint["pool"],
                        "reviewed_examples": checkpoint["examples"],
                        "semantic_feedback": checkpoint.get("semantic_feedback"),
                    }
                )
            except TaskError as exc:
                if exc.code != "model_budget_exceeded" or "semantic_feedback" not in checkpoint:
                    raise
                await tsz.check_sources(run.external_request_id, [context.target, *pool])
                return RunResult(
                    outcome="insufficient_candidates",
                    reasons=[
                        "语义重选预算已用尽，尚未排除歧义",
                        *checkpoint["semantic_feedback"]["issues"],
                    ],
                )
            if isinstance(proposal, DraftProposal):
                selected(context, pool, proposal)
            checkpoint["proposal"] = proposal.model_dump(mode="json")
            await save("generated", checkpoint)
        if isinstance(proposal, InsufficientProposal):
            await tsz.check_sources(run.external_request_id, [context.target, *pool])
            return RunResult(outcome="insufficient_candidates", reasons=proposal.reasons)

        distractors = selected(context, pool, proposal)
        feedback = checkpoint.get("semantic_feedback")
        if feedback and {str(c.source.entry_id) for c in distractors} == {
            c["source"]["entry_id"] for c in feedback["previous_distractors"]
        }:
            await tsz.check_sources(run.external_request_id, [context.target, *pool])
            return RunResult(
                outcome="insufficient_candidates",
                reasons=["语义复审未通过，重选后仍为原干扰项，无法排除歧义", *feedback["issues"]],
            )
        # Fixed by run ID, stable across retries; the model does not choose correct position.
        candidates = list(distractors)
        correct_index = run.id.int % 4
        candidates.insert(correct_index, context.target)
        ids: tuple[OptionId, ...] = ("A", "B", "C", "D")
        options = [QuestionOption(option_id=ids[i], candidate=c) for i, c in enumerate(candidates)]
        if "semantic_review" not in checkpoint:
            await save("reviewing", checkpoint)
            try:
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
            except TaskError as exc:
                if exc.code != "model_budget_exceeded" or "semantic_feedback" not in checkpoint:
                    raise
                await tsz.check_sources(run.external_request_id, [context.target, *pool])
                return RunResult(
                    outcome="insufficient_candidates",
                    reasons=[
                        "语义重选后复审预算已用尽，尚未排除歧义",
                        *checkpoint["semantic_feedback"]["issues"],
                    ],
                )
            checkpoint["semantic_review"] = review.model_dump(mode="json")
            await save("reviewed", checkpoint)
        await tsz.check_sources(run.external_request_id, candidates)
        # A fresh delegated read catches permission revocation and target changes near completion.
        if (await tsz.context(run.external_request_id)) != context:
            raise TaskError("source_changed")
        review = SemanticReview.model_validate(checkpoint["semantic_review"])
        if review.verdict == "needs_revision" or proposal.needs_prompt_revision:
            issues = list(review.issues)
            if proposal.needs_prompt_revision:
                issues.append("原题干需要补充限定条件，不能通过只修改解析来排除歧义")
            if checkpoint.get("semantic_reselection", 0) >= 1:
                return RunResult(
                    outcome="insufficient_candidates",
                    reasons=["语义复审后仍未找到三个可排除歧义的干扰项", *issues],
                )
            if model.usage().requests + 2 > request_limit:
                return RunResult(
                    outcome="insufficient_candidates",
                    reasons=["剩余调用预算不足以重选并复审，尚未排除歧义", *issues],
                )
            if checkpoint["searches"] < 2:
                extra = await tsz.candidates(
                    run.external_request_id, context, list(checkpoint["candidate_hashes"])
                )
                for candidate in extra:
                    previous = checkpoint["candidate_hashes"].get(candidate.candidate_ref)
                    if previous is not None and previous != digest(
                        candidate.model_dump(mode="json")
                    ):
                        raise TaskError("candidate_identity_conflict")
                pool = valid_pool(context, [*extra, *pool])[:40]
                await tsz.check_sources(run.external_request_id, [context.target, *pool])
                checkpoint["pool"] = [c.model_dump(mode="json") for c in pool]
                checkpoint["searches"] = 2
            checkpoint["semantic_reselection"] = 1
            checkpoint["semantic_feedback"] = {
                "issues": issues,
                "previous_distractors": [c.model_dump(mode="json") for c in distractors],
            }
            del checkpoint["proposal"]
            del checkpoint["semantic_review"]
            await save("reselecting", checkpoint)
            continue
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
