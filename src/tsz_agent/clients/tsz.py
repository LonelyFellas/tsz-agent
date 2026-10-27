from typing import Any
from uuid import UUID

import httpx
from pydantic import ValidationError

from tsz_agent.schemas import (
    Candidate,
    CandidatePage,
    CandidateSearch,
    EntriesRequest,
    EntryRead,
    GenerationContext,
)


class TaskError(Exception):
    def __init__(self, code: str, *, retryable: bool = False):
        self.code, self.retryable = code, retryable
        super().__init__(code)


class TszClient:
    """Only delegated, versioned lexicon reads; never a generic URL/SQL tool."""

    def __init__(self, http: httpx.AsyncClient):
        self.http = http

    @staticmethod
    def path(request_id: UUID, suffix: str) -> str:
        return f"/api/internal/agent/v1/requests/{request_id}/{suffix}"

    async def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            response = await self.http.request(method, path, json=body)
        except httpx.TransportError as exc:
            raise TaskError("tsz_unavailable", retryable=True) from exc
        if response.status_code in {401, 403}:
            raise TaskError("authorization_revoked")
        if response.status_code in {404, 409, 410}:
            raise TaskError("source_changed")
        if response.status_code == 429 or response.status_code >= 500:
            raise TaskError("tsz_unavailable", retryable=True)
        if response.status_code != 200:
            raise TaskError("source_contract_invalid")
        try:
            if len(response.content) > 2_000_000:
                raise TaskError("source_response_too_large")
            return response.json()
        except ValueError as exc:
            raise TaskError("source_contract_invalid") from exc

    async def context(self, request_id: UUID) -> GenerationContext:
        try:
            value = GenerationContext.model_validate(
                await self.request("GET", self.path(request_id, "context"))
            )
        except ValidationError as exc:
            raise TaskError("source_contract_invalid") from exc
        if value.external_request_id != request_id:
            raise TaskError("request_identity_mismatch")
        return value

    async def candidates(
        self, request_id: UUID, context: GenerationContext, exclude: list[str] | None = None
    ) -> list[Candidate]:
        raw = await self.request(
            "POST",
            self.path(request_id, "candidates/search"),
            CandidateSearch(
                target_source=context.target.source,
                question_level=context.question_level,
                answer_profile=context.answer_profile,
                exclude_candidate_refs=exclude or [],
            ).model_dump(mode="json"),
        )
        try:
            items = CandidatePage.model_validate(raw).items
            if len(items) > 40:
                raise TaskError("source_contract_invalid")
            return items
        except ValidationError as exc:
            raise TaskError("source_contract_invalid") from exc

    async def check_sources(self, request_id: UUID, candidates: list[Candidate]) -> None:
        raw = await self.request(
            "POST",
            self.path(request_id, "entries/batch-get"),
            EntriesRequest(
                items=[
                    EntryRead(
                        candidate_ref=c.candidate_ref,
                        source=c.source,
                        answer_profile=c.answer_profile,
                    )
                    for c in candidates
                ]
            ).model_dump(mode="json"),
        )
        try:
            current = CandidatePage.model_validate(raw).items
        except ValidationError as exc:
            raise TaskError("source_contract_invalid") from exc
        by_ref = {c.candidate_ref: c for c in current}
        if len(by_ref) != len(current) or set(by_ref) != {c.candidate_ref for c in candidates}:
            raise TaskError("source_changed")
        for old in candidates:
            new = by_ref[old.candidate_ref]
            # V1 deliberately requires exact publications; no inferred audio-only exemption.
            if old != new or not new.teaching_eligible:
                raise TaskError("source_changed")
