"""Model-only views. Authoritative identities remain in the workflow checkpoint."""

from typing import Any

from tsz_agent.clients.tsz import TaskError
from tsz_agent.schemas import Candidate, DraftProposal, GenerationContext, Proposal, ReviewEvent


def candidate_view(candidate: Candidate) -> dict[str, Any]:
    return candidate.model_dump(
        mode="json",
        include={
            "spelling",
            "part_of_speech",
            "level",
            "definition",
            "other_definitions",
            "accepted_spellings",
            "answer_profile",
        },
    )


def generation_input(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    context = GenerationContext.model_validate(data["context"])
    pool = [Candidate.model_validate(item) for item in data["candidate_pool"]]
    references = {f"c{i:02}": candidate.candidate_ref for i, candidate in enumerate(pool, 1)}
    examples = []
    for raw in data["reviewed_examples"]:
        event = ReviewEvent.model_validate(raw)
        example = event.example
        if event.decision == "revoked" or example is None:
            continue
        examples.append(
            {
                "decision": event.decision,
                "reason_codes": event.reason_codes,
                "comment": event.comment,
                "example": {
                    "target": candidate_view(example.target),
                    "question_level": example.question_level,
                    "prompt": example.prompt,
                    "distractors": [candidate_view(c) for c in example.distractors],
                    "lesson": example.lesson,
                },
            }
        )
    result = {
        "candidate_pool": [
            {"candidate_ref": alias, **candidate_view(candidate)}
            for alias, candidate in zip(references, pool, strict=True)
        ],
        "context": {
            "target": candidate_view(context.target),
            "question_level": context.question_level,
            "answer_profile": context.answer_profile,
        },
        "reviewed_examples": examples,
    }
    feedback = data.get("semantic_feedback")
    if feedback:
        result["semantic_feedback"] = {
            "issues": feedback["issues"],
            "previous_distractors": [
                candidate_view(Candidate.model_validate(c))
                for c in feedback["previous_distractors"]
            ],
        }
    return result, references


def restore_references(proposal: Proposal, references: dict[str, str]) -> Proposal:
    if not isinstance(proposal, DraftProposal):
        return proposal
    choices = [choice.candidate_ref for choice in proposal.distractors]
    if len(set(choices)) != 3 or any(choice not in references for choice in choices):
        raise TaskError("invalid_model_selection")
    return proposal.model_copy(
        update={
            "distractors": [
                choice.model_copy(update={"candidate_ref": references[choice.candidate_ref]})
                for choice in proposal.distractors
            ]
        }
    )


def review_input(data: dict[str, Any]) -> dict[str, Any]:
    result = dict(data)
    if "options" in result:
        result["options"] = [
            {
                "option_id": option["option_id"],
                "candidate": candidate_view(Candidate.model_validate(option["candidate"])),
            }
            for option in result["options"]
        ]
    return result
