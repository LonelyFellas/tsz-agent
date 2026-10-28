"""Stateless OpenCode v2 generations, without giving the model filesystem tools."""

import asyncio
import json
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


async def request(http: httpx.AsyncClient, method: str, path: str, body: Any = None) -> Any:
    try:
        async with http.stream(method, path, json=body) as response:
            if response.status_code != 200:
                raise TaskError(
                    "opencode_unavailable",
                    retryable=response.status_code >= 500 or response.status_code == 429,
                )
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 2_000_000:
                    raise TaskError("opencode_response_too_large")
        return json.loads(data)["data"]
    except (httpx.TransportError, ValueError, KeyError, TypeError) as exc:
        raise TaskError("opencode_unavailable", retryable=True) from exc


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
        # v2.1.4 exposes neither usage nor a per-call token cap. Keep the reservation
        # held; never fabricate usage or apply DeepSeek prices to gateway calls.
        await self.budget.reserve(call_id, 3000)
        outcome: Literal["response", "error", "cancelled"] = "error"
        started = perf_counter()
        try:
            value = await request(
                self.http,
                "POST",
                "/api/experimental/generate",
                {
                    "prompt": prompt,
                    "model": {
                        "providerID": self.selection.provider_id,
                        "id": self.selection.model_id,
                    },
                },
            )
            if not isinstance(value, dict) or not isinstance(value.get("text"), str):
                raise TaskError("model_output_invalid")
            outcome = "response"
            try:
                return schema.model_validate_json(value["text"])
            except ValidationError as exc:
                raise TaskError("model_output_invalid") from exc
        except asyncio.CancelledError:
            outcome = "cancelled"
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
