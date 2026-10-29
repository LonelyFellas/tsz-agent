"""Explicit model probes. Catalog membership is not a successful inference receipt."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

import httpx
from pydantic import Field

from tsz_agent.chatgpt_auth import AuthStore
from tsz_agent.clients.tsz import TaskError
from tsz_agent.config import Settings
from tsz_agent.models import chatgpt
from tsz_agent.models.opencode import (
    PROBE_OUTPUT_TOKENS,
    SESSION_PROTOCOL,
    OpenCodeRequestError,
    client,
    generate_text,
    request,
)
from tsz_agent.schemas import ModelSelection, StrictModel

if TYPE_CHECKING:
    from tsz_agent.storage import Store

CONCURRENCY = 2
CHATGPT_CATALOG_TIMEOUT_SECONDS = 5
PROMPT = "Reply with only OK."


def now() -> datetime:
    return datetime.now(UTC)


class ModelProbe(ModelSelection):
    name: str
    enabled: bool
    status: Literal["unverified", "checking", "available", "unavailable"] = "unverified"
    checked_at: datetime | None = None
    duration_ms: float | None = Field(default=None, ge=0)
    http_status: int | None = None
    error: (
        Literal[
            "disabled",
            "not_configured",
            "timeout",
            "request_failed",
            "invalid_response",
            "interrupted",
            "runtime_failed",
        ]
        | None
    ) = None
    cost: str | None = None


class ModelProbeReport(StrictModel):
    batch_id: UUID | None = None
    state: Literal["idle", "running", "completed", "interrupted"] = "idle"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    models: list[ModelProbe] = Field(default_factory=list)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def catalog(settings: Settings) -> tuple[list[ModelProbe], str]:
    key = settings.deepseek_api_key
    models = [
        ModelProbe(name="deepseek-flash（直连）", enabled=bool(key and key.get_secret_value()))
    ]
    native: list[Any] = []
    integrations: list[Any] = []
    chatgpt_context: Any = None
    if settings.chatgpt_enabled:
        try:
            async with asyncio.timeout(CHATGPT_CATALOG_TIMEOUT_SECONDS):
                auth = AuthStore(settings.chatgpt_auth_dir)
                accounts = await auth.accounts()
                account = accounts.accounts.get(accounts.active or "")
                if account and account.public()["plan_enabled"]:
                    chatgpt_context = {
                        "client_id": account.client_id,
                        "session_id": account.session_id,
                        "subject": account.subject,
                        "scopes": sorted(account.scopes),
                        "host_id": accounts.host_id,
                        "protocol": chatgpt.PROTOCOL,
                    }
                    native_chatgpt = await chatgpt.catalog(auth, account.client_id)
                    models.extend(
                        ModelProbe(
                            source="chatgpt",
                            provider_id=account.client_id,
                            model_id=m["model_id"],
                            name=m["name"] + "（ChatGPT）",
                            enabled=True,
                        )
                        for m in native_chatgpt
                    )
        except (TaskError, TimeoutError) as exc:
            # A slow or unavailable personal provider must not hide other platforms.
            chatgpt_context = chatgpt_context or {}
            chatgpt_context["error"] = exc.code if isinstance(exc, TaskError) else "timeout"
    if settings.opencode_server_url is not None:
        try:
            async with asyncio.timeout(5), client(settings) as http:
                native = await request(http, "GET", "/api/model")
                integrations = await request(http, "GET", "/api/integration")
        except TimeoutError as exc:
            raise TaskError("model_catalog_unavailable", retryable=True) from exc
        if not isinstance(native, list) or not isinstance(integrations, list):
            raise TaskError("model_catalog_invalid")
        try:
            models.extend(
                ModelProbe(
                    source="opencode",
                    provider_id=m["providerID"],
                    model_id=m["id"],
                    name=m["name"],
                    enabled=m["enabled"],
                )
                for m in native
                if "text" in m["capabilities"]["output"]
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise TaskError("model_catalog_invalid") from exc
    # Retain only an opaque fingerprint. Connection IDs detect Key replacement;
    # tokens, headers, credential labels and upstream error messages are never stored.
    connections = sorted(
        (
            p["id"],
            list(
                (
                    c.get("type", ""),
                    c.get("id", ""),
                    c.get("name", ""),
                    c.get("status", {}).get("status", ""),
                )
                for c in p.get("connections", [])
            ),
        )
        for p in integrations
    )
    context = fingerprint(
        {
            "models": [
                m.model_dump(mode="json")
                for m in sorted(models, key=lambda model: model.usage_model())
            ],
            "connections": connections,
            "deepseek_key": fingerprint(key.get_secret_value()) if key else None,
            "gateway": str(settings.opencode_server_url),
            "gateway_protocol": SESSION_PROTOCOL,
            "gateway_key": fingerprint(settings.opencode_server_password.get_secret_value())
            if settings.opencode_server_password
            else None,
            "chatgpt": chatgpt_context,
        }
    )
    return models, context


async def probe_one(settings: Settings, model: ModelProbe) -> ModelProbe:
    result = model.model_copy(update={"status": "unavailable", "checked_at": now(), "cost": None})
    if not model.enabled:
        result.error = "disabled"
        return result
    started = perf_counter()
    try:
        async with asyncio.timeout(settings.request_timeout_seconds):
            if model.source == "chatgpt":
                if not settings.chatgpt_enabled:
                    result.error = "not_configured"
                    return result
                async with httpx.AsyncClient(
                    timeout=settings.request_timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                ) as http:
                    kind, value = await chatgpt.invoke(
                        http,
                        AuthStore(settings.chatgpt_auth_dir),
                        model,
                        "Reply briefly as plain text.",
                        PROMPT,
                    )
                    if kind != "response.completed":
                        result.error = "request_failed"
                        return result
                    chatgpt.final_text(value)
                result.http_status = 200
                result.status = "available"
                return result
            elif model.source == "opencode":
                async with client(settings) as http:
                    await generate_text(http, model, PROMPT, max_output_tokens=PROBE_OUTPUT_TOKENS)
                result.http_status = 200
                result.status = "available"
                return result
            else:
                key = settings.deepseek_api_key
                if key is None or not key.get_secret_value():
                    result.error = "not_configured"
                    return result
                http = httpx.AsyncClient(
                    base_url="https://api.deepseek.com",
                    headers={"Authorization": "Bearer " + key.get_secret_value()},
                    timeout=settings.request_timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                )
                path = "/chat/completions"
                body = {
                    "model": model.model_id,
                    "messages": [{"role": "user", "content": PROMPT}],
                    "max_tokens": 16,
                    "thinking": {"type": "disabled"},
                }
            async with http:
                async with http.stream("POST", path, json=body) as response:
                    result.http_status = response.status_code
                    if response.status_code != 200:
                        result.error = "request_failed"
                        return result
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 2_000_000:
                            result.error = "invalid_response"
                            return result
                value = json.loads(data)
                content = value["choices"][0]["message"]["content"]
                if not isinstance(content, str) or not content.strip():
                    result.error = "invalid_response"
                    return result
                result.status = "available"
    except (TimeoutError, httpx.TimeoutException):
        result.error = "timeout"
    except httpx.TransportError:
        result.error = "request_failed"
    except (ValueError, KeyError, TypeError, IndexError):
        result.error = "invalid_response"
    except OpenCodeRequestError as exc:
        result.http_status = exc.status_code
        result.error = "request_failed"
    except TaskError as exc:
        if exc.code in {
            "opencode_not_configured",
            "opencode_budget_not_configured",
            "chatgpt_login_required",
            "chatgpt_plan_not_authorized",
            "chatgpt_not_configured",
        }:
            result.error = "not_configured"
        elif exc.code in {
            "model_output_invalid",
            "model_response_empty",
            "opencode_response_too_large",
        }:
            result.error = "invalid_response"
        elif isinstance(exc.__cause__, httpx.TimeoutException):
            result.error = "timeout"
        else:
            result.error = "request_failed"
    finally:
        result.checked_at = now()
        result.duration_ms = (perf_counter() - started) * 1000
    return result


class ProbeService:
    def __init__(self, settings: Settings, store: Store):
        self.settings, self.store = settings, store
        self.tasks: set[asyncio.Task[None]] = set()

    async def report(self) -> ModelProbeReport:
        models, context = await catalog(self.settings)
        packet = await self.store.probe_packet()
        if packet is None:
            return ModelProbeReport(models=models)
        report = ModelProbeReport.model_validate(packet["report"])
        if report.state == "running" and datetime.fromisoformat(packet["deadline"]) < now():
            await self.store.finish_probe(report.batch_id, interrupted=True)
            packet = await self.store.probe_packet()
            assert packet is not None
            report = ModelProbeReport.model_validate(packet["report"])
        if packet["context"] != context:
            return ModelProbeReport(
                batch_id=report.batch_id,
                state="running" if report.state == "running" else "idle",
                started_at=report.started_at,
                models=models,
            )
        return report

    async def start(self) -> ModelProbeReport:
        models, context = await catalog(self.settings)
        report = ModelProbeReport(
            batch_id=uuid4(), state="running", started_at=now(), models=models
        )
        deadline = now() + timedelta(
            seconds=((len(models) + CONCURRENCY - 1) // CONCURRENCY)
            * (self.settings.request_timeout_seconds + 6)
            + 30
        )
        packet, started = await self.store.start_probe(
            {
                "context": context,
                "deadline": deadline.isoformat(),
                "report": report.model_dump(mode="json"),
            }
        )
        report = ModelProbeReport.model_validate(packet["report"])
        if not started and packet["context"] != context:
            return ModelProbeReport(
                batch_id=report.batch_id,
                state="running",
                started_at=report.started_at,
                models=models,
            )
        if started:
            task = asyncio.create_task(self.run(report, context))
            self.tasks.add(task)

            def done(task: asyncio.Task[None]) -> None:
                self.tasks.discard(task)
                if not task.cancelled() and (error := task.exception()) is not None:
                    logging.getLogger("tsz_agent.probes").error(
                        "probe_failed error_kind=%s", type(error).__name__
                    )

            task.add_done_callback(done)
        return report

    async def run(self, report: ModelProbeReport, context: str) -> None:
        semaphore = asyncio.Semaphore(CONCURRENCY)
        context_changed = False

        async def check(model: ModelProbe) -> None:
            nonlocal context_changed
            async with semaphore:
                if not await self.store.record_probe(
                    report.batch_id, model.model_copy(update={"status": "checking"})
                ):
                    return
                _, current_context = await catalog(self.settings)
                if current_context != context:
                    context_changed = True
                    return
                await self.store.record_probe(
                    report.batch_id, await probe_one(self.settings, model)
                )

        completed = False
        try:
            async with asyncio.TaskGroup() as group:
                for model in report.models:
                    group.create_task(check(model))
            completed = True
        finally:
            await self.store.finish_probe(
                report.batch_id, interrupted=not completed or context_changed
            )

    async def require_available(self, selection: ModelSelection) -> str:
        packet = await self.store.probe_packet()
        if packet is None:
            raise TaskError("model_not_verified")
        _, context = await catalog(self.settings)
        if packet["context"] != context:
            raise TaskError("model_not_verified")
        report = ModelProbeReport.model_validate(packet["report"])
        if not any(
            m.usage_model() == selection.usage_model() and m.enabled and m.status == "available"
            for m in report.models
        ):
            raise TaskError("model_not_verified")
        return context

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
