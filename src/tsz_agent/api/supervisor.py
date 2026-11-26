import asyncio
from contextlib import suppress

from fastapi import APIRouter, Request

from tsz_agent.config import Settings
from tsz_agent.storage import Store
from tsz_agent.supervisor import candidates, lexicon, workflow
from tsz_agent.supervisor.runtime import AdmissionLimit

from .problems import Problem

SECTION_CANDIDATES_HEADER = "x-tsz-next-section-candidates"
SHARED_CANDIDATES_HEADER = "x-tsz-next-shared-candidates"


def candidate_mode(request: Request) -> candidates.CandidateMode:
    if request.headers.get(SHARED_CANDIDATES_HEADER) == "v1":
        return "shared"
    if request.headers.get(SECTION_CANDIDATES_HEADER) == "v1":
        return "personal"
    return "legacy"


def build_router(settings: Settings, store: Store) -> APIRouter:
    router = APIRouter(prefix="/v2")

    def enabled() -> bool:
        return settings.supervisor_enabled and bool(settings.deepseek_api_key)

    @router.get("/lexicon/capability")
    async def supervisor_capability() -> dict[str, bool]:
        active = enabled()
        return {
            "generation": active,
            "generation_candidates": active,
            "grammar_sub_pos": active,
            "section_generation_candidates": active,
            "dictionary_refresh": active,
        }

    @router.post("/lexicon/dictionary-reference")
    async def dictionary_reference(body: lexicon.DictionaryLookup) -> lexicon.Material:
        if not enabled():
            raise Problem(503, "generation_unavailable")
        (reference,) = await workflow.fetch_dictionaries(settings, body.headword, (body.source,))
        return reference

    async def execute(
        request: Request, body: lexicon.Input, *, refresh: bool = False
    ) -> lexicon.View:
        if not enabled():
            raise Problem(503, "generation_unavailable")
        disconnected = asyncio.Event()

        async def watch_disconnect() -> None:
            # FastAPI has already consumed the request body. Wait for the ASGI event
            # so a disconnect isn't missed by a nonblocking probe through middleware.
            if (await request.receive())["type"] == "http.disconnect":
                disconnected.set()

        watcher = asyncio.create_task(watch_disconnect())
        try:
            view = await workflow.advance(
                store,
                settings,
                body,
                force_refresh=refresh,
                mode=candidate_mode(request),
                disconnected=disconnected.is_set,
            )
            # A replay can return a completed run generated before the current rules.
            for row in view.proposals.grammar:
                lexicon.retain_grammar_sub_pos(row, body.scope)
            return view
        except AdmissionLimit as exc:
            raise Problem(429, "run_limit") from exc
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher

    @router.post("/lexicon/advance")
    async def advance_lexicon(request: Request, body: lexicon.Input) -> lexicon.View:
        return await execute(request, body)

    @router.post("/lexicon/refresh")
    async def refresh_lexicon(request: Request, body: lexicon.Input) -> lexicon.View:
        return await execute(request, body, refresh=True)

    @router.post("/lexicon/candidates")
    async def lexicon_candidates(
        request: Request, body: lexicon.Input
    ) -> candidates.CandidateLookup:
        if not enabled():
            raise Problem(503, "generation_unavailable")
        return await workflow.lookup(store, settings, body, mode=candidate_mode(request))

    return router
