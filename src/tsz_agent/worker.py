import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from tsz_agent.capabilities.vocabulary_mcq.workflow import execute
from tsz_agent.clients.tsz import TaskError, TszClient
from tsz_agent.config import Settings
from tsz_agent.models.budget import StoreBudget
from tsz_agent.models.deepseek import DeepSeekModel, QuestionModel
from tsz_agent.models.opencode import (
    FLINT_JOB_TIMEOUT_SECONDS,
    FLINT_MODEL,
    FLINT_TASK_OUTPUT_TOKENS,
    OpenCodeModel,
)
from tsz_agent.schemas import ModelSelection, Usage
from tsz_agent.storage import Store

logger = logging.getLogger("tsz_agent.worker")


async def run_once(
    settings: Settings,
    store: Store,
    tsz: TszClient,
    factory: Callable[[Usage], QuestionModel] | None = None,
) -> bool:
    run = await store.claim(settings.lease_seconds, settings.max_attempts)
    if run is None:
        return False
    token = run.lease_token
    assert token is not None
    previous = Usage.model_validate(run.usage)
    flint = previous.model == FLINT_MODEL
    output_limit = FLINT_TASK_OUTPUT_TOKENS if flint else settings.output_token_limit
    job_timeout = FLINT_JOB_TIMEOUT_SECONDS if flint else settings.job_timeout_seconds
    model: QuestionModel | None = None
    lease_lost = False

    def get_model() -> QuestionModel:
        nonlocal model
        if model is None:
            budget = StoreBudget(
                store,
                run.id,
                token,
                previous,
                settings.model_request_limit,
                output_limit,
            )
            if factory:
                model = factory(previous)
            elif previous.model.startswith("opencode/"):
                _, provider_id, model_id = previous.model.split("/", 2)
                model = OpenCodeModel(
                    settings,
                    ModelSelection(
                        source="opencode",
                        provider_id=provider_id,
                        model_id=model_id,
                    ),
                    budget,
                )
            else:
                model = DeepSeekModel(settings, previous, budget)
        return model

    async def save(stage: str, checkpoint: dict[str, Any]) -> None:
        if not await store.checkpoint(run.id, token, stage, checkpoint):
            raise TaskError("lease_lost")

    async def process() -> None:
        started_at = datetime.now(UTC)
        try:
            async with asyncio.timeout(job_timeout):
                result = await execute(
                    run, store, tsz, get_model, save, request_limit=settings.model_request_limit
                )
                await store.complete(run.id, token, result)
        except TaskError as exc:
            if exc.code in {
                "model_unavailable",
                "opencode_unavailable",
                "opencode_version_unsupported",
                "opencode_budget_not_configured",
                "model_output_invalid",
                "model_response_empty",
            }:
                await store.invalidate_probe(previous.model, started_at, (run.id, token))
            await store.fail(run.id, token, exc.code, exc.retryable, settings.max_attempts)
        except TimeoutError:
            await store.fail(run.id, token, "job_timeout", True, settings.max_attempts)
        except Exception as exc:
            # Never print model/source payloads, credentials or raw exception messages.
            logger.error("run_failed run_id=%s error_kind=%s", run.id, type(exc).__name__)
            await store.fail(run.id, token, "execution_error", False, settings.max_attempts)

    task = asyncio.create_task(process())

    async def heartbeat() -> None:
        nonlocal lease_lost
        while not task.done():
            await asyncio.sleep(settings.lease_seconds / 3)
            if task.done():
                return
            try:
                valid = await store.renew(run.id, token, settings.lease_seconds)
            except Exception:
                valid = False
            if not valid:
                lease_lost = True
                task.cancel()
                return

    renewal = asyncio.create_task(heartbeat())
    try:
        await task
    except asyncio.CancelledError:
        if not lease_lost:
            raise
    finally:
        renewal.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await renewal
        if isinstance(model, (DeepSeekModel, OpenCodeModel)):
            await model.close()
    return True


async def main() -> None:
    settings = Settings()
    if settings.deepseek_api_key is None and settings.opencode_server_url is None:
        raise SystemExit("DeepSeek or OpenCode must be configured for the worker")
    store = Store(settings.database_url.get_secret_value())
    try:
        await store.ready()
        async with httpx.AsyncClient(
            base_url=str(settings.tsz_base_url).rstrip("/"),
            headers={"Authorization": f"Bearer {settings.tsz_service_token.get_secret_value()}"},
            timeout=20,
            follow_redirects=False,
            trust_env=False,
        ) as http:
            while True:
                try:
                    worked = await run_once(settings, store, TszClient(http))
                except Exception as exc:
                    logger.error("worker_unavailable error_kind=%s", type(exc).__name__)
                    worked = False
                if not worked:
                    await asyncio.sleep(settings.poll_seconds)
    finally:
        await store.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
