import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import Response

from tsz_agent.config import Settings
from tsz_agent.models.probes import ModelProbeReport, ProbeService
from tsz_agent.schemas import (
    CreateRun,
    ModelCostLedgerPage,
    ModelCostOverview,
    ModelLedgerPage,
    ModelSelection,
    ProblemDetails,
    ReviewEvent,
    ReviewReceipt,
    RunModelMetrics,
    RunView,
)
from tsz_agent.storage import Conflict, Store


class Problem(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code


def create_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    settings = settings or Settings()
    owned_store = store is None
    store = store or Store(settings.database_url.get_secret_value())

    probes = ProbeService(settings, store)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await probes.close()
        if owned_store:
            await store.close()

    app = FastAPI(
        title="TSZ Agent",
        version="1.0.0",
        lifespan=lifespan,
        responses={
            status: {
                "description": "Problem Details",
                "content": {
                    "application/problem+json": {"schema": ProblemDetails.model_json_schema()}
                },
            }
            for status in [401, 404, 409, 422, 500, 503]
        },
    )
    bearer = HTTPBearer(auto_error=False)

    async def authorized(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        if credentials is None or not secrets.compare_digest(
            credentials.credentials.encode(), settings.api_token.get_secret_value().encode()
        ):
            raise Problem(401, "invalid_service_token")

    @app.middleware("http")
    async def request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = str(uuid4())
        try:
            response = await call_next(request)
        except Exception as exc:
            response = await unexpected_response(request, exc)
        response.headers["x-request-id"] = request.state.request_id
        return response

    @app.exception_handler(Problem)
    async def problem_response(_: Request, exc: Problem) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return JSONResponse(
            status_code=exc.status,
            media_type="application/problem+json",
            headers=headers,
            content={
                "type": f"urn:tsz-agent:problem:{exc.code}",
                "title": exc.code,
                "status": exc.status,
                "detail": exc.code,
                "code": exc.code,
            },
        )

    @app.exception_handler(Conflict)
    async def conflict_response(request: Request, exc: Conflict) -> JSONResponse:
        return await problem_response(request, Problem(409, str(exc)))

    @app.exception_handler(RequestValidationError)
    async def validation_response(request: Request, _: RequestValidationError) -> JSONResponse:
        return await problem_response(request, Problem(422, "invalid_request"))

    @app.exception_handler(SQLAlchemyError)
    async def database_response(request: Request, _: SQLAlchemyError) -> JSONResponse:
        return await problem_response(request, Problem(503, "database_unavailable"))

    async def unexpected_response(request: Request, exc: Exception) -> JSONResponse:
        logging.getLogger("tsz_agent.api").error("request_failed error_kind=%s", type(exc).__name__)
        return await problem_response(request, Problem(500, "internal_error"))

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", dependencies=[Depends(authorized)])
    async def ready() -> dict[str, str]:
        try:
            await store.ready()
        except Exception as exc:
            raise Problem(503, "database_unavailable") from exc
        return {"status": "ready"}

    @app.get("/v1/model-probes", dependencies=[Depends(authorized)])
    async def get_model_probes() -> ModelProbeReport:
        from tsz_agent.clients.tsz import TaskError

        try:
            return await probes.report()
        except TaskError as exc:
            raise Problem(503, exc.code) from exc

    @app.post("/v1/model-probes", status_code=202, dependencies=[Depends(authorized)])
    async def start_model_probes() -> ModelProbeReport:
        from tsz_agent.clients.tsz import TaskError

        try:
            return await probes.start()
        except TaskError as exc:
            raise Problem(503, exc.code) from exc

    @app.get("/v1/model-selection", dependencies=[Depends(authorized)])
    async def get_model_selection() -> ModelSelection:
        return await store.model_selection()

    @app.put("/v1/model-selection", dependencies=[Depends(authorized)])
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
        else:
            try:
                await OpenCodeModel.validate_selection(settings, body)
            except TaskError as exc:
                raise Problem(503 if exc.retryable else 422, exc.code) from exc
        try:
            return await store.set_model_selection(body, probe_context=context)
        except TaskError as exc:
            raise Problem(422, exc.code) from exc

    @app.get("/v1/model-calls", response_model=ModelLedgerPage, dependencies=[Depends(authorized)])
    async def get_model_ledger(
        page: Annotated[int, Query(ge=1, le=1_000_000)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
        run_id: UUID | None = None,
    ) -> ModelLedgerPage:
        return await store.model_ledger(page, page_size, run_id)

    @app.get(
        "/v1/model-costs", response_model=ModelCostLedgerPage, dependencies=[Depends(authorized)]
    )
    async def get_cost_ledger(
        page: Annotated[int, Query(ge=1, le=1_000_000)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
        run_id: UUID | None = None,
    ) -> ModelCostLedgerPage:
        return await store.model_cost_ledger(page, page_size, run_id)

    @app.get(
        "/v1/model-costs/overview",
        response_model=ModelCostOverview,
        dependencies=[Depends(authorized)],
    )
    async def get_cost_overview() -> ModelCostOverview:
        return await store.model_cost_overview()

    @app.post(
        "/v1/runs", response_model=RunView, status_code=202, dependencies=[Depends(authorized)]
    )
    async def create_run(body: CreateRun, idempotency_key: Annotated[UUID, Header()]) -> RunView:
        return await store.create(body, idempotency_key)

    @app.get("/v1/runs/{run_id}", response_model=RunView, dependencies=[Depends(authorized)])
    async def get_run(run_id: UUID) -> RunView:
        result = await store.get(run_id)
        if result is None:
            raise Problem(404, "run_not_found")
        return result

    @app.get(
        "/v1/runs/{run_id}/model-metrics",
        response_model=RunModelMetrics,
        dependencies=[Depends(authorized)],
    )
    async def get_model_metrics(run_id: UUID) -> RunModelMetrics:
        result = await store.model_metrics(run_id)
        if result is None:
            raise Problem(404, "run_not_found")
        return result

    @app.post(
        "/v1/runs/{run_id}/cancel", response_model=RunView, dependencies=[Depends(authorized)]
    )
    async def cancel_run(run_id: UUID) -> RunView:
        result = await store.cancel(run_id)
        if result is None:
            raise Problem(404, "run_not_found")
        return result

    @app.post("/v1/review-events", dependencies=[Depends(authorized)])
    async def review_event(event: ReviewEvent) -> ReviewReceipt:
        return ReviewReceipt(applied=await store.feedback(event))

    return app
