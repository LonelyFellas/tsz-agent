"""Native OpenCode text-only session generation; tool calls are never executed."""

import asyncio
import json
import logging
import re
from importlib.resources import files
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

import httpx
from pydantic import ValidationError

from tsz_agent.clients.tsz import TaskError
from tsz_agent.config import Settings
from tsz_agent.models.budget import CallBudget
from tsz_agent.models.deepseek import DeepSeekModel, GenerationOutput
from tsz_agent.models.inputs import generation_input, restore_references, review_input
from tsz_agent.schemas import ModelCallMetrics, ModelSelection, Proposal, SemanticReview, Usage

SESSION_PROTOCOL = "session-generate-budget-v3"
BUDGET_PLUGIN = "tsz.agent.session-budget-v1"
OUTPUT_TOKENS = 3000
PROBE_OUTPUT_TOKENS = 256
# Ask preserves native tool declarations. This client never approves permissions or runs a tool loop.
TOOL_PERMISSIONS = [{"action": "*", "resource": "*", "effect": "ask"}]


class OpenCodeRequestError(TaskError):
    def __init__(self, status_code: int):
        super().__init__("opencode_unavailable", retryable=status_code >= 500 or status_code == 429)
        self.status_code = status_code


def client(settings: Settings) -> httpx.AsyncClient:
    url, password = settings.opencode_server_url, settings.opencode_server_password
    if url is None or password is None or len(password.get_secret_value()) < 32:
        raise TaskError("opencode_not_configured")
    if (
        (
            url.scheme != "https"
            and not (url.scheme == "http" and url.host in {"localhost", "127.0.0.1", "[::1]"})
        )
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in {None, "/"}
    ):
        raise TaskError("opencode_not_configured")
    return httpx.AsyncClient(
        base_url=str(url).rstrip("/"),
        auth=("opencode", password.get_secret_value()),
        timeout=settings.request_timeout_seconds,
        follow_redirects=False,
        trust_env=False,
    )


async def request(
    http: httpx.AsyncClient,
    method: str,
    path: str,
    body: Any = None,
    *,
    expected_status: int = 200,
    unwrap: bool = True,
) -> Any:
    try:
        async with http.stream(method, path, json=body) as response:
            if response.status_code != expected_status:
                raise OpenCodeRequestError(response.status_code)
            if expected_status == 204:
                return None
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 2_000_000:
                    raise TaskError("opencode_response_too_large")
        value = json.loads(data)
        return value["data"] if unwrap else value
    except (httpx.TransportError, ValueError, KeyError, TypeError) as exc:
        raise TaskError("opencode_unavailable", retryable=True) from exc


def session_id(value: Any) -> str | None:
    identifier = value.get("id") if isinstance(value, dict) else None
    if isinstance(identifier, str) and re.fullmatch(r"ses_[A-Za-z0-9]+", identifier):
        return identifier
    return None


async def remove_session(http: httpx.AsyncClient, identifier: str) -> None:
    try:
        async with asyncio.timeout(5):
            try:
                await request(http, "DELETE", f"/api/session/{identifier}", expected_status=204)
            except OpenCodeRequestError as exc:
                if exc.status_code != 404:
                    raise
    except (TaskError, TimeoutError) as exc:
        raise TaskError("opencode_session_cleanup_failed", retryable=True) from exc


async def generate_text(
    http: httpx.AsyncClient,
    selection: ModelSelection,
    prompt: str,
    *,
    max_output_tokens: int = OUTPUT_TOKENS,
) -> str:
    # The no-execution guarantee is verified against this native session.generate implementation.
    info = await request(http, "GET", "/api/info", unwrap=False)
    if not isinstance(info, dict) or info.get("version") != "2.1.4":
        raise TaskError("opencode_version_unsupported")
    plugins = await request(http, "GET", "/api/plugin")
    if not isinstance(plugins, list) or not any(
        isinstance(p, dict)
        and p.get("id") == BUDGET_PLUGIN
        and isinstance(p.get("state"), dict)
        and p["state"].get("status") == "active"
        for p in plugins
    ):
        raise TaskError("opencode_budget_not_configured")
    if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= OUTPUT_TOKENS:
        raise TaskError("model_budget_exceeded")
    metadata = {"tszAgent": {"protocol": SESSION_PROTOCOL, "maxOutputTokens": max_output_tokens}}
    model = {"providerID": selection.provider_id, "id": selection.model_id, "variant": "default"}

    async def create() -> Any:
        async with asyncio.timeout(5):
            return await request(
                http,
                "POST",
                "/api/session",
                {
                    "title": "TSZ generation",
                    "agent": "build",
                    "model": model,
                    "permissions": TOOL_PERMISSIONS,
                    "metadata": metadata,
                },
            )

    # Let an in-flight creation finish on cancellation so its server-issued ID can be cleaned up.
    creation = asyncio.create_task(create())
    failed = True
    try:
        try:
            value = await asyncio.shield(creation)
        except asyncio.CancelledError:
            await asyncio.gather(creation, return_exceptions=True)
            raise
        identifier = session_id(value)
        if (
            identifier is None
            or value.get("model") != model
            or value.get("permissions") != TOOL_PERMISSIONS
            or value.get("metadata") != metadata
        ):
            raise TaskError("model_output_invalid")
        # This API prepares normal session context but never executes the returned tool calls.
        value = await request(
            http, "POST", f"/api/session/{identifier}/generate", {"prompt": prompt}
        )
        if not isinstance(value, dict) or not isinstance(value.get("text"), str):
            raise TaskError("model_output_invalid")
        if not value["text"].strip():
            raise TaskError("model_response_empty")
        failed = False
        return str(value["text"])
    finally:
        if creation.done() and not creation.cancelled() and creation.exception() is None:
            identifier = session_id(creation.result())
            if identifier is not None:
                cleanup = asyncio.create_task(remove_session(http, identifier))
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                    raise
                except TaskError:
                    logging.getLogger(__name__).error("opencode_session_cleanup_failed")
                    if not failed:
                        raise


class OpenCodeModel:
    def __init__(self, settings: Settings, selection: ModelSelection, budget: CallBudget):
        self.http = client(settings)
        self.selection, self.budget = selection, budget

    @staticmethod
    async def validate_selection(settings: Settings, selection: ModelSelection) -> None:
        async with client(settings) as http:
            models = await request(http, "GET", "/api/model")
        if not isinstance(models, list) or not any(
            isinstance(m, dict)
            and m.get("providerID") == selection.provider_id
            and m.get("id") == selection.model_id
            and m.get("enabled") is True
            and "text" in m.get("capabilities", {}).get("output", [])
            for m in models
        ):
            raise TaskError("model_unavailable")

    def usage(self) -> Usage:
        return self.budget.usage()

    async def output(self, data: dict[str, Any], phase: Literal["generate", "review"]) -> Any:
        schema = GenerationOutput if phase == "generate" else SemanticReview
        instructions = (
            files("tsz_agent.capabilities.vocabulary_mcq").joinpath(f"{phase}.txt").read_text()
        )
        prompt = (
            instructions
            + "\nReturn JSON only matching this schema:\n"
            + json.dumps(schema.model_json_schema())
            + "\n"
            + DeepSeekModel.prompt(data)
        )
        call_id = uuid4()
        # The native plugin applies this allowance; missing usage keeps the reservation held.
        allowance = await self.budget.reserve(call_id, OUTPUT_TOKENS)
        outcome: Literal["response", "error", "cancelled"] = "error"
        started = perf_counter()
        try:
            value = await generate_text(
                self.http, self.selection, prompt, max_output_tokens=allowance
            )
            outcome = "response"
            try:
                return schema.model_validate_json(value)
            except ValidationError as exc:
                raise TaskError("model_output_invalid") from exc
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except TaskError as exc:
            if exc.code == "model_response_empty":
                outcome = "response"
            raise
        finally:
            task = asyncio.create_task(
                self.budget.record_metrics(
                    call_id,
                    ModelCallMetrics(
                        phase=phase,
                        outcome=outcome,
                        duration_ms=(perf_counter() - started) * 1000,
                    ),
                )
            )
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    async def generate(self, data: dict[str, Any]) -> Proposal:
        model_input, references = generation_input(data)
        result: GenerationOutput = await self.output(model_input, "generate")
        return restore_references(result.proposal, references)

    async def review(self, data: dict[str, Any]) -> SemanticReview:
        result: SemanticReview = await self.output(review_input(data), "review")
        return result

    async def close(self) -> None:
        await self.http.aclose()
