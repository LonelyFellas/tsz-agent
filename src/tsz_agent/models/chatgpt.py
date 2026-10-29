"""Text-only Responses inference authorized for a personal ChatGPT plan."""

import asyncio
import json
from importlib.resources import files
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

import httpx
from pydantic import ValidationError

from tsz_agent.chatgpt_auth import RESOURCE, AuthStore
from tsz_agent.clients.tsz import TaskError
from tsz_agent.config import Settings
from tsz_agent.models.budget import CallBudget
from tsz_agent.models.deepseek import DeepSeekModel, GenerationOutput
from tsz_agent.models.inputs import generation_input, restore_references, review_input
from tsz_agent.schemas import ModelCallMetrics, ModelSelection, Proposal, SemanticReview, Usage

PROTOCOL = "chatgpt-plan-responses-v1"


def response_error(status: int, code: Any = None) -> TaskError:
    if code == "subscription_sharing_usage_limit_exceeded" or status == 429:
        return TaskError("chatgpt_usage_limit_exceeded")
    if status == 401 or code == "subscription_sharing_invalid_user":
        return TaskError("chatgpt_login_required")
    if status == 403 or code in {
        "subscription_sharing_user_not_eligible",
        "subscription_sharing_route_not_supported",
        "chatpass_v2_scope_not_authorized",
        "chatpass_v2_invalid_authorization_context",
    }:
        return TaskError("chatgpt_access_denied")
    return TaskError("chatgpt_unavailable", retryable=status >= 500)


async def http_error(response: httpx.Response) -> TaskError:
    data = bytearray()
    async for chunk in response.aiter_bytes():
        data.extend(chunk)
        if len(data) > 16_384:
            return response_error(response.status_code)
    try:
        value = json.loads(data)
        error = value.get("error")
        return response_error(
            response.status_code, error.get("code") if isinstance(error, dict) else None
        )
    except (ValueError, AttributeError):
        return response_error(response.status_code)


async def catalog(auth: AuthStore, client_id: str) -> list[dict[str, str]]:
    token = await auth.token(client_id)
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as http:
            async with http.stream(
                "GET",
                RESOURCE + "/models",
                headers={"Authorization": "Bearer " + token.get_secret_value()},
            ) as response:
                if response.status_code != 200:
                    raise await http_error(response)
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 2_000_000:
                        raise TaskError("chatgpt_catalog_invalid")
        value = json.loads(data)
        return [
            {"model_id": m["slug"], "name": m["display_name"]}
            for m in value["models"]
            if m.get("visibility") == "list"
            and isinstance(m.get("slug"), str)
            and m["slug"]
            and isinstance(m.get("display_name"), str)
        ]
    except httpx.TransportError as exc:
        raise TaskError("chatgpt_unavailable", retryable=True) from exc
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise TaskError("chatgpt_catalog_invalid") from exc


async def invoke(
    http: httpx.AsyncClient,
    auth: AuthStore,
    selection: ModelSelection,
    instructions: str,
    prompt: str,
) -> tuple[str, dict[str, Any]]:
    token = await auth.token(selection.provider_id)
    try:
        async with http.stream(
            "POST",
            RESOURCE + "/responses",
            headers={"Authorization": "Bearer " + token.get_secret_value()},
            json={
                "model": selection.model_id,
                "instructions": instructions,
                "input": [{"role": "user", "content": prompt}],
                "store": False,
                "stream": True,
            },
        ) as response:
            if response.status_code != 200:
                raise await http_error(response)
            buffer = b""
            event_data: list[str] = []
            size = 0
            # Count bytes before parsing lines, including reasoning and all SSE framing.
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > 2_000_000:
                    raise TaskError("chatgpt_response_too_large")
                # Buffer bytes until complete lines to preserve split UTF-8 sequences.
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    text = line.rstrip(b"\r").decode("utf-8")
                    if text.startswith("data:"):
                        event_data.append(text[5:].lstrip(" "))
                    elif not text and event_data:
                        payload = "\n".join(event_data)
                        event_data.clear()
                        if payload == "[DONE]":
                            continue
                        event = json.loads(payload)
                        kind = event.get("type")
                        if kind in {"response.completed", "response.incomplete", "response.failed"}:
                            if not isinstance(event.get("response"), dict):
                                raise TaskError("model_output_invalid")
                            return kind, event["response"]
                        if kind == "error":
                            raise response_error(
                                503
                                if event.get("code")
                                in {
                                    "subscription_sharing_usage_unavailable",
                                    "subscription_sharing_user_unavailable",
                                }
                                else 400,
                                event.get("code"),
                            )
    except httpx.TransportError as exc:
        raise TaskError("chatgpt_unavailable", retryable=True) from exc
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError) as exc:
        raise TaskError("model_output_invalid") from exc
    raise TaskError("chatgpt_stream_interrupted", retryable=True)


def final_text(value: dict[str, Any]) -> str:
    text = ""
    try:
        for item in value["output"]:
            if item["type"] == "reasoning":
                continue
            if item["type"] != "message" or item.get("role") != "assistant":
                raise TaskError("model_output_invalid")
            for part in item["content"]:
                if part["type"] != "output_text":
                    raise TaskError("model_output_invalid")
                text += part["text"]
    except (KeyError, TypeError) as exc:
        raise TaskError("model_output_invalid") from exc
    if not text.strip():
        raise TaskError("model_response_empty")
    return text


class ChatGPTModel:
    def __init__(self, settings: Settings, selection: ModelSelection, budget: CallBudget):
        if not settings.chatgpt_enabled:
            raise TaskError("chatgpt_not_configured")
        self.auth = AuthStore(settings.chatgpt_auth_dir)
        self.http = httpx.AsyncClient(
            timeout=settings.request_timeout_seconds, follow_redirects=False, trust_env=False
        )
        self.selection, self.budget = selection, budget

    def usage(self) -> Usage:
        return self.budget.usage()

    async def output(self, data: dict[str, Any], phase: Literal["generate", "review"]) -> Any:
        schema = GenerationOutput if phase == "generate" else SemanticReview
        instructions = (
            files("tsz_agent.capabilities.vocabulary_mcq").joinpath(f"{phase}.txt").read_text()
        )
        instructions += "\nReturn JSON only matching this schema:\n" + json.dumps(
            schema.model_json_schema()
        )
        prompt = DeepSeekModel.prompt(data)
        # Authenticate before reserving. Never fall back to another account or billing route.
        await self.auth.token(self.selection.provider_id)
        call_id = uuid4()
        allowance = await self.budget.reserve(call_id, 3000)
        started = perf_counter()
        outcome: Literal["response", "error", "cancelled"] = "error"
        receipt: tuple[int, int] | None = None
        hit = miss = None
        try:
            kind, value = await invoke(self.http, self.auth, self.selection, instructions, prompt)
            outcome = "response"
            usage = value.get("usage")
            if isinstance(usage, dict):
                incoming, outgoing = usage.get("input_tokens"), usage.get("output_tokens")
                if type(incoming) is int and type(outgoing) is int and min(incoming, outgoing) >= 0:
                    receipt = incoming, outgoing
                    details = usage.get("input_tokens_details")
                    cached = details.get("cached_tokens") if isinstance(details, dict) else None
                    if type(cached) is int and 0 <= cached <= incoming:
                        hit, miss = cached, incoming - cached
            if kind == "response.failed":
                error = value.get("error") or {}
                raise response_error(
                    503
                    if error.get("code")
                    in {
                        "subscription_sharing_usage_unavailable",
                        "subscription_sharing_user_unavailable",
                    }
                    else 400,
                    error.get("code"),
                )
            if kind != "response.completed":
                raise TaskError("chatgpt_response_incomplete")
            if receipt is not None and receipt[1] > allowance:
                raise TaskError("model_budget_exceeded")
            return schema.model_validate_json(final_text(value))
        except ValidationError as exc:
            raise TaskError("model_output_invalid") from exc
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            metrics = ModelCallMetrics(
                phase=phase,
                outcome=outcome,
                duration_ms=(perf_counter() - started) * 1000,
                cache_hit_tokens=hit,
                cache_miss_tokens=miss,
            )

            async def settle() -> None:
                if receipt is not None:
                    await self.budget.record(call_id, *receipt)
                await self.budget.record_metrics(call_id, metrics)

            task = asyncio.create_task(settle())
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    async def generate(self, data: dict[str, Any]) -> Proposal:
        value, references = generation_input(data)
        result: GenerationOutput = await self.output(value, "generate")
        return restore_references(result.proposal, references)

    async def review(self, data: dict[str, Any]) -> SemanticReview:
        result: SemanticReview = await self.output(review_input(data), "review")
        return result

    async def close(self) -> None:
        await self.http.aclose()
