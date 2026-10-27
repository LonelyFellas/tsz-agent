import unicodedata

from tsz_agent.clients.tsz import TaskError
from tsz_agent.schemas import Candidate, DraftProposal, GenerationContext

LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2")


def normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().replace("’", "'").split())


def valid_pool(context: GenerationContext, candidates: list[Candidate]) -> list[Candidate]:
    target = context.target
    if LEVELS.index(target.level) > LEVELS.index(context.question_level):
        raise TaskError("target_level_mismatch")
    answers = {normalize(target.spelling), *(normalize(s) for s in target.accepted_spellings)}
    seen_refs: dict[str, Candidate] = {target.candidate_ref: target}
    seen_words = set(answers)
    seen_entries = {target.source.entry_id}
    result = []
    for candidate in candidates:
        prior = seen_refs.get(candidate.candidate_ref)
        if prior and prior != candidate:
            raise TaskError("candidate_identity_conflict")
        seen_refs[candidate.candidate_ref] = candidate
        keys = {
            normalize(candidate.spelling),
            *(normalize(s) for s in candidate.accepted_spellings),
        }
        if (
            not candidate.teaching_eligible
            or candidate.answer_profile != context.answer_profile
            or candidate.part_of_speech != target.part_of_speech
            or LEVELS.index(candidate.level) > LEVELS.index(context.question_level)
            or candidate.source.entry_id in seen_entries
            or keys & seen_words
        ):
            continue
        # Exact matching is a sufficient rejection signal, not proof that other meanings differ.
        if normalize(target.definition) in {
            normalize(candidate.definition),
            *(normalize(d) for d in candidate.other_definitions),
        }:
            continue
        seen_words.update(keys)
        seen_entries.add(candidate.source.entry_id)
        result.append(candidate)
    return result


def selected(
    context: GenerationContext, pool: list[Candidate], proposal: DraftProposal
) -> list[Candidate]:
    choices = [c.candidate_ref for c in proposal.distractors]
    allowed = {c.candidate_ref: c for c in valid_pool(context, pool)}
    if len(set(choices)) != 3 or any(key not in allowed for key in choices):
        raise TaskError("invalid_model_selection")
    return [allowed[key] for key in choices]
