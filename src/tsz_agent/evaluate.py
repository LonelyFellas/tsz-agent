"""Explicitly opt-in paid evaluation against frozen, independently labelled fixtures."""

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from pydantic import Field

from tsz_agent.capabilities.vocabulary_mcq.validators import selected, valid_pool
from tsz_agent.clients.tsz import TaskError
from tsz_agent.config import Settings
from tsz_agent.models.deepseek import PROMPT_VERSION, DeepSeekModel
from tsz_agent.schemas import Candidate, DraftProposal, GenerationContext, StrictModel, Usage


class EvaluationCase(StrictModel):
    id: str
    label_source: str
    context: GenerationContext
    candidates: list[Candidate] = Field(max_length=40)
    forbidden_candidate_refs: list[str] = Field(default_factory=list)
    expect_needs_revision: bool | None = None


class Suite(StrictModel):
    cases: list[EvaluationCase] = Field(min_length=1, max_length=50)


async def evaluate(suite: Suite, output: Path) -> None:
    settings = Settings()
    report: dict[str, Any] = {"prompt_version": PROMPT_VERSION, "results": []}
    for case in suite.cases:
        model = DeepSeekModel(settings, Usage())
        row: dict[str, object] = {"id": case.id, "label_source": case.label_source}
        try:
            pool = valid_pool(case.context, case.candidates)
            if len(pool) < 3:
                row["outcome"] = "insufficient_candidates"
            else:
                proposal = await model.generate(
                    {
                        "context": case.context.model_dump(mode="json"),
                        "candidate_pool": [c.model_dump(mode="json") for c in pool],
                        "reviewed_examples": [],
                    }
                )
                row["proposal"] = proposal.model_dump(mode="json")
                if isinstance(proposal, DraftProposal):
                    choices = selected(case.context, pool, proposal)
                    review = await model.review(
                        {
                            "original_prompt": case.context.target.definition,
                            "part_of_speech": case.context.target.part_of_speech,
                            "question_level": case.context.question_level,
                            "options": [
                                c.model_dump(mode="json") for c in [case.context.target, *choices]
                            ],
                            "intended_answer": case.context.target.spelling,
                        }
                    )
                    row["review"] = review.model_dump(mode="json")
                    row["forbidden_selected"] = sorted(
                        set(case.forbidden_candidate_refs) & {c.candidate_ref for c in choices}
                    )
                    row["revision_expectation_met"] = (
                        case.expect_needs_revision is None
                        or case.expect_needs_revision
                        == (proposal.needs_prompt_revision or review.verdict == "needs_revision")
                    )
                row["outcome"] = proposal.outcome
        except TaskError as exc:
            row["error_code"] = exc.code
        finally:
            row["usage"] = model.usage().model_dump(mode="json")
            await model.close()
        report["results"].append(row)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("output/evaluation.json"))
    parser.add_argument("--live", action="store_true", help="explicitly allow paid DeepSeek calls")
    args = parser.parse_args()
    suite = Suite.model_validate_json(args.suite.read_text())
    if not args.live:
        print(
            json.dumps(
                {
                    "fixture_count": len(suite.cases),
                    "model_calls": 0,
                    "note": "Only fixture structure checked; --live makes paid API calls.",
                }
            )
        )
        return
    asyncio.run(evaluate(suite, args.output))


if __name__ == "__main__":
    main()
