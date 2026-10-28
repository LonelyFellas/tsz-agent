import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

import httpx

from tsz_agent.capabilities.vocabulary_mcq.workflow import execute
from tsz_agent.clients.tsz import TaskError, TszClient
from tsz_agent.config import Settings
from tsz_agent.models.budget import StoreBudget
from tsz_agent.models.deepseek import DeepSeekModel, QuestionModel
from tsz_agent.schemas import Usage
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
    model: QuestionModel | None = None
    lease_lost = False

    def get_model() -> QuestionModel:
        nonlocal model
        if model is None:
            model = (
                factory(previous)
                if factory
                else DeepSeekModel(
                    settings,
                    previous,
                    StoreBudget(
                        store,
                        run.id,
                        token,
                        previous,
                        settings.model_request_limit,
                        settings.output_token_limit,
                    ),
                )
            )
        return model

    async def save(stage: str, checkpoint: dict[str, Any]) -> None:
        if not await store.checkpoint(run.id, token, stage, checkpoint):
            raise TaskError("lease_lost")

    async def process() -> None:
        try:
            async with asyncio.timeout(settings.job_timeout_seconds):
                result = await execute(run, store, tsz, get_model, save)
                await store.complete(run.id, token, result)
        except TaskError as exc:
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
        if isinstance(model, DeepSeekModel):
            await model.close()
    return True


async def main() -> None:
    settings = Settings()
    if settings.deepseek_api_key is None or not settings.deepseek_api_key.get_secret_value():
        raise SystemExit("DEEPSEEK_API_KEY is required for the worker")
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
