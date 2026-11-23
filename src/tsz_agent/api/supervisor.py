from fastapi import APIRouter

from tsz_agent.config import Settings
from tsz_agent.storage import Store
from tsz_agent.supervisor import lexicon, workflow
from tsz_agent.supervisor.runtime import AdmissionLimit

from .problems import Problem


def build_router(settings: Settings, store: Store) -> APIRouter:
    router = APIRouter(prefix="/v2")

    @router.get("/lexicon/capability")
    async def supervisor_capability() -> dict[str, bool]:
        return {"generation": settings.supervisor_enabled and bool(settings.deepseek_api_key)}

    @router.post("/lexicon/advance")
    async def advance_lexicon(body: lexicon.Input) -> lexicon.View:
        if not settings.supervisor_enabled or not settings.deepseek_api_key:
            raise Problem(503, "generation_unavailable")
        try:
            return await workflow.advance(store, settings, body)
        except AdmissionLimit as exc:
            raise Problem(429, "run_limit") from exc

    return router
