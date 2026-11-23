"""Existing assistant, account and quiz contracts for v1 callers."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from tsz_agent import form_candidates, lexicon_assistant
from tsz_agent.chatgpt_login import (
    ChatGPTAccountInput,
    ChatGPTAccounts,
    ChatGPTLogin,
    ChatGPTLoginService,
    ChatGPTLoginStatus,
    ChatGPTLogout,
    ChatGPTSetup,
)
from tsz_agent.config import Settings
from tsz_agent.models.probes import ModelProbeReport, ProbeService
from tsz_agent.schemas import (
    CreateRun,
    ModelCallPurpose,
    ModelCostLedgerPage,
    ModelCostOverview,
    ModelLedgerPage,
    ModelSelection,
    ReviewEvent,
    ReviewReceipt,
    RunModelMetrics,
    RunView,
)
from tsz_agent.storage import Store

from .problems import Problem


def build_router(
    settings: Settings, store: Store, probes: ProbeService, chatgpt: ChatGPTLoginService
) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/lexicon-assistant/capability")
    async def assistant_capability(request: Request) -> dict[str, object]:
        if request.headers.get("x-tsz-assistant-version") == "2":
            return {
                "available": bool(settings.deepseek_api_key),
                "tool_selection": True,
                "rules_version": lexicon_assistant.RULES_VERSION,
            }
        key = settings.deepseek_api_key
        return {"available": key is not None and bool(key.get_secret_value())}

    @router.post(
        "/v1/lexicon-assistant/form-candidates",
        response_model=form_candidates.CandidatesOutput,
    )
    async def generate_form_candidates(
        request: Request,
        body: form_candidates.CandidatesInput,
    ) -> form_candidates.CandidatesOutput:
        try:
            return await form_candidates.generate(
                settings, body, store, request_id=request.state.request_id
            )
        except lexicon_assistant.AssistantUnavailable as exc:
            raise Problem(503, str(exc)) from exc

    @router.post(
        "/v1/lexicon-assistant/step",
        response_model=lexicon_assistant.AssistantStepOutput,
    )
    async def assistant_step(
        body: lexicon_assistant.AssistantStepInput,
    ) -> lexicon_assistant.AssistantStepOutput:
        try:
            return await lexicon_assistant.step(settings, body, store)
        except lexicon_assistant.AssistantUnavailable as exc:
            raise Problem(503, str(exc)) from exc

    @router.post("/v1/lexicon-assistant/step/stream")
    async def assistant_stream(body: lexicon_assistant.AssistantStepInput) -> StreamingResponse:
        return StreamingResponse(
            lexicon_assistant.stream_step(settings, body, store),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @router.get("/v1/chatgpt/setup")
    async def chatgpt_setup() -> ChatGPTSetup:
        return ChatGPTSetup(login_method=settings.chatgpt_login_mode)

    @router.get("/v1/chatgpt/accounts")
    async def chatgpt_accounts() -> ChatGPTAccounts:
        return await chatgpt.accounts()

    @router.post("/v1/chatgpt/login")
    async def chatgpt_login(body: ChatGPTAccountInput) -> ChatGPTLogin:
        if settings.chatgpt_login_mode == "ssh":
            raise Problem(422, "chatgpt_remote_setup_required")
        return await chatgpt.start(body.client_id)

    @router.get("/v1/chatgpt/login/{attempt_id}")
    async def chatgpt_login_status(attempt_id: UUID) -> ChatGPTLoginStatus:
        return chatgpt.status(attempt_id)

    @router.delete("/v1/chatgpt/login/{attempt_id}")
    async def chatgpt_cancel_login(attempt_id: UUID) -> ChatGPTLoginStatus:
        return await chatgpt.cancel(attempt_id)

    @router.put("/v1/chatgpt/account")
    async def chatgpt_use_account(body: ChatGPTAccountInput) -> ChatGPTAccounts:
        chatgpt.require_enabled()
        if body.client_id is None:
            raise Problem(422, "chatgpt_account_not_found")
        return await chatgpt.activate(body.client_id)

    @router.delete("/v1/chatgpt/accounts/{client_id}")
    async def chatgpt_logout(client_id: str) -> ChatGPTLogout:
        chatgpt.require_enabled()
        return await chatgpt.logout(client_id)

    @router.get("/v1/model-probes")
    async def get_model_probes() -> ModelProbeReport:
        from tsz_agent.clients.tsz import TaskError

        try:
            return await probes.report()
        except TaskError as exc:
            raise Problem(503, exc.code) from exc

    @router.post("/v1/model-probes", status_code=202)
    async def start_model_probes() -> ModelProbeReport:
        from tsz_agent.clients.tsz import TaskError

        try:
            return await probes.start()
        except TaskError as exc:
            raise Problem(503, exc.code) from exc

    @router.get("/v1/model-selection")
    async def get_model_selection() -> ModelSelection:
        return await store.model_selection()

    @router.put("/v1/model-selection")
    async def set_model_selection(body: ModelSelection) -> ModelSelection:
        from tsz_agent.clients.tsz import TaskError
        from tsz_agent.models.opencode import OpenCodeModel

        try:
            context = await probes.require_available(body)
        except TaskError as exc:
            raise Problem(503 if exc.retryable else 422, exc.code) from exc
        if body.source == "direct":
            if (
                settings.deepseek_api_key is None
                or not settings.deepseek_api_key.get_secret_value()
            ):
                raise Problem(422, "model_not_configured")
        elif body.source == "opencode":
            try:
                await OpenCodeModel.validate_selection(settings, body)
            except TaskError as exc:
                raise Problem(503 if exc.retryable else 422, exc.code) from exc
        try:
            return await store.set_model_selection(body, probe_context=context)
        except TaskError as exc:
            raise Problem(422, exc.code) from exc

    @router.get(
        "/v1/model-calls",
        response_model=ModelLedgerPage,
        response_model_exclude_none=False,
    )
    async def get_model_ledger(
        request: Request,
        page: Annotated[int, Query(ge=1, le=1_000_000)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
        run_id: UUID | None = None,
        purpose: ModelCallPurpose | None = None,
    ) -> JSONResponse:
        extended = request.headers.get("x-tsz-ledger-version") == "2"
        result = await store.model_ledger(
            page, page_size, run_id, service_calls=extended, purpose=purpose if extended else None
        )
        payload = result.model_dump(mode="json")
        if extended:
            payload["purpose_filter_supported"] = True
        else:
            payload.pop("purpose_filter_supported", None)
            for item in payload["items"]:
                item.pop("purpose", None)
        return JSONResponse(payload)

    @router.get("/v1/model-costs", response_model=ModelCostLedgerPage)
    async def get_cost_ledger(
        request: Request,
        page: Annotated[int, Query(ge=1, le=1_000_000)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
        run_id: UUID | None = None,
        purpose: ModelCallPurpose | None = None,
    ) -> JSONResponse:
        extended = request.headers.get("x-tsz-ledger-version") == "2"
        result = await store.model_cost_ledger(
            page, page_size, run_id, service_calls=extended, purpose=purpose if extended else None
        )
        payload = result.model_dump(mode="json")
        if extended:
            payload["ledger"]["purpose_filter_supported"] = True
        else:
            payload["ledger"].pop("purpose_filter_supported", None)
            for item in payload["ledger"]["items"]:
                item.pop("purpose", None)
        return JSONResponse(payload)

    @router.get(
        "/v1/model-costs/overview",
        response_model=ModelCostOverview,
    )
    async def get_cost_overview(request: Request) -> JSONResponse:
        extended = request.headers.get("x-tsz-ledger-version") == "2"
        result = await store.model_cost_overview(service_calls=extended)
        payload = result.model_dump(mode="json")
        if not extended:
            payload.pop("by_purpose", None)
        return JSONResponse(payload)

    @router.post("/v1/runs", response_model=RunView, status_code=202)
    async def create_run(body: CreateRun, idempotency_key: Annotated[UUID, Header()]) -> RunView:
        return await store.create(body, idempotency_key)

    @router.get("/v1/runs/{run_id}", response_model=RunView)
    async def get_run(run_id: UUID) -> RunView:
        result = await store.get(run_id)
        if result is None:
            raise Problem(404, "run_not_found")
        return result

    @router.get(
        "/v1/runs/{run_id}/model-metrics",
        response_model=RunModelMetrics,
    )
    async def get_model_metrics(run_id: UUID) -> RunModelMetrics:
        result = await store.model_metrics(run_id)
        if result is None:
            raise Problem(404, "run_not_found")
        return result

    @router.post("/v1/runs/{run_id}/cancel", response_model=RunView)
    async def cancel_run(run_id: UUID) -> RunView:
        result = await store.cancel(run_id)
        if result is None:
            raise Problem(404, "run_not_found")
        return result

    @router.post("/v1/review-events")
    async def review_event(event: ReviewEvent) -> ReviewReceipt:
        return ReviewReceipt(applied=await store.feedback(event))

    return router
