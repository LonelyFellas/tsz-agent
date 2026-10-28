"""Reserve before dispatch; settle known usage independently of task result ownership."""

import asyncio
from time import perf_counter
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import UUID, uuid4

from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

from tsz_agent.clients.tsz import TaskError
from tsz_agent.schemas import ModelCallMetrics, Usage

if TYPE_CHECKING:
    from tsz_agent.storage import Store


class CallBudget(Protocol):
    def usage(self) -> Usage: ...
    async def reserve(self, call_id: UUID, requested_output: int) -> int: ...
    async def record(self, call_id: UUID, input_tokens: int, output_tokens: int) -> None: ...
    async def record_metrics(self, call_id: UUID, metrics: ModelCallMetrics) -> None: ...


class MemoryBudget:
    """Standalone evaluation budget. Worker runs use the durable StoreBudget below."""

    def __init__(self, previous: Usage, request_limit: int, output_limit: int):
        self.current = previous.model_copy()
        self.request_limit, self.output_limit = request_limit, output_limit
        self.pending: dict[UUID, int] = {}
        self.receipts: dict[UUID, tuple[int, int]] = {}
        self.metrics: dict[UUID, ModelCallMetrics] = {}
        self.lock = asyncio.Lock()

    def usage(self) -> Usage:
        return self.current.model_copy()

    async def reserve(self, call_id: UUID, requested_output: int) -> int:
        async with self.lock:
            remaining = self.output_limit - self.current.output_tokens - sum(self.pending.values())
            if (
                self.current.requests >= self.request_limit
                or remaining <= 0
                or requested_output <= 0
            ):
                raise TaskError("model_budget_exceeded")
            allowance = min(requested_output, remaining)
            self.pending[call_id] = allowance
            self.current.requests += 1
            return allowance

    async def record(self, call_id: UUID, input_tokens: int, output_tokens: int) -> None:
        async with self.lock:
            receipt = (input_tokens, output_tokens)
            if call_id in self.receipts:
                if self.receipts[call_id] != receipt:
                    raise TaskError("usage_receipt_conflict")
                return
            if call_id not in self.pending or min(input_tokens, output_tokens) < 0:
                raise TaskError("usage_receipt_invalid")
            del self.pending[call_id]
            self.receipts[call_id] = receipt
            self.current.input_tokens += input_tokens
            self.current.output_tokens += output_tokens

    async def record_metrics(self, call_id: UUID, metrics: ModelCallMetrics) -> None:
        async with self.lock:
            if call_id not in self.pending and call_id not in self.receipts:
                raise TaskError("usage_receipt_invalid")
            if call_id in self.metrics and self.metrics[call_id] != metrics:
                raise TaskError("usage_receipt_conflict")
            self.metrics[call_id] = metrics.model_copy()


class StoreBudget:
    def __init__(
        self,
        store: "Store",
        run_id: UUID,
        lease_token: UUID,
        previous: Usage,
        request_limit: int,
        output_limit: int,
    ):
        self.store, self.run_id, self.lease_token = store, run_id, lease_token
        self.current = previous
        self.request_limit, self.output_limit = request_limit, output_limit

    def usage(self) -> Usage:
        return self.current.model_copy()

    async def reserve(self, call_id: UUID, requested_output: int) -> int:
        allowance, self.current = await self.store.reserve_model_call(
            self.run_id,
            self.lease_token,
            call_id,
            requested_output,
            self.request_limit,
            self.output_limit,
        )
        return allowance

    async def record(self, call_id: UUID, input_tokens: int, output_tokens: int) -> None:
        self.current = await self.store.record_model_usage(
            self.run_id,
            call_id,
            input_tokens,
            output_tokens,
        )

    async def record_metrics(self, call_id: UUID, metrics: ModelCallMetrics) -> None:
        await self.store.record_model_metrics(self.run_id, call_id, metrics)


class BudgetedModel(WrapperModel):
    """Guard every non-streaming request, including PydanticAI's repair retries."""

    def __init__(self, wrapped: Model, budget: CallBudget, phase: Literal["generate", "review"]):
        super().__init__(wrapped)
        self.budget = budget
        self.phase = phase

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        settings = model_settings.copy() if model_settings is not None else ModelSettings()
        call_id = uuid4()
        allowance = await self.budget.reserve(call_id, settings.get("max_tokens") or 3000)
        settings["max_tokens"] = allowance
        response = None
        outcome: Literal["response", "error", "cancelled"] = "error"
        started = perf_counter()
        try:
            response = await self.wrapped.request(messages, settings, model_request_parameters)
            outcome = "response"
            return response
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            metrics = ModelCallMetrics(
                phase=self.phase,
                outcome=outcome,
                duration_ms=(perf_counter() - started) * 1000,
            )
            if response is not None:
                # SDK cache_read_tokens defaults to zero even when the provider omitted it.
                # Only use the explicit DeepSeek counters retained in usage.details.
                hit = response.usage.details.get("prompt_cache_hit_tokens")
                miss = response.usage.details.get("prompt_cache_miss_tokens")
                if (
                    type(hit) is int
                    and type(miss) is int
                    and hit >= 0
                    and miss >= 0
                    and hit + miss == response.usage.input_tokens
                ):
                    metrics.cache_hit_tokens, metrics.cache_miss_tokens = hit, miss

            async def settle() -> None:
                # Missing usage, failures and cancellation keep the output reservation held.
                if response is not None and (
                    response.usage.input_tokens or response.usage.output_tokens
                ):
                    await self.budget.record(
                        call_id, response.usage.input_tokens, response.usage.output_tokens
                    )
                await self.budget.record_metrics(call_id, metrics)

            receipt = asyncio.create_task(settle())
            try:
                await asyncio.shield(receipt)
            except asyncio.CancelledError:
                # Persist observations and known usage before cancellation escapes.
                await receipt
                raise
