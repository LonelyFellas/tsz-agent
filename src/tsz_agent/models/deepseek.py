import json
from importlib.resources import files
from typing import Any, Protocol

from openai import AsyncOpenAI
from pydantic_ai import Agent, PromptedOutput, UsageLimits
from pydantic_ai.exceptions import (
    ModelConnectionError,
    ModelHTTPError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.usage import RunUsage

from tsz_agent.clients.tsz import TaskError
from tsz_agent.config import Settings
from tsz_agent.models.budget import BudgetedModel, CallBudget, MemoryBudget
from tsz_agent.schemas import Proposal, SemanticReview, StrictModel, Usage

PROMPT_VERSION = "mcq_zh_en_v2"


class GenerationOutput(StrictModel):
    proposal: Proposal


class QuestionModel(Protocol):
    def usage(self) -> Usage: ...
    async def generate(self, data: dict[str, Any]) -> Proposal: ...
    async def review(self, data: dict[str, Any]) -> SemanticReview: ...


class DeepSeekModel:
    def __init__(self, settings: Settings, previous_usage: Usage, budget: CallBudget | None = None):
        if settings.deepseek_api_key is None or not settings.deepseek_api_key.get_secret_value():
            raise TaskError("model_not_configured")
        self.settings = settings
        self.budget = budget or MemoryBudget(
            previous_usage, settings.model_request_limit, settings.output_token_limit
        )
        self.meter = RunUsage(
            requests=previous_usage.requests,
            input_tokens=previous_usage.input_tokens,
            output_tokens=previous_usage.output_tokens,
        )
        self.provider = DeepSeekProvider(
            openai_client=AsyncOpenAI(
                api_key=settings.deepseek_api_key.get_secret_value(),
                base_url="https://api.deepseek.com",
                max_retries=0,
                timeout=settings.request_timeout_seconds,
            )
        )
        self.model = OpenAIChatModel(
            settings.deepseek_model,
            provider=self.provider,
            profile=OpenAIModelProfile(
                openai_chat_thinking_false_body={"thinking": {"type": "disabled"}},
                openai_chat_supports_max_completion_tokens=False,
            ),
        )
        self.limits = UsageLimits(
            request_limit=None,
            output_tokens_limit=None,
            input_tokens_limit=40000,
        )
        self.model_settings = OpenAIChatModelSettings(
            thinking=False,
            max_tokens=3000,
            timeout=settings.request_timeout_seconds,
        )

    def usage(self) -> Usage:
        return self.budget.usage()

    @staticmethod
    def prompt(data: dict[str, Any]) -> str:
        value = json.dumps(data, ensure_ascii=False)
        if len(value.encode()) > 100_000:
            raise TaskError("context_too_large")
        return value

    async def generate(self, data: dict[str, Any]) -> Proposal:
        instructions = (
            files("tsz_agent.capabilities.vocabulary_mcq").joinpath("generate.txt").read_text()
        )
        agent: Agent[None, GenerationOutput] = Agent(
            BudgetedModel(self.model, self.budget, "generate"),
            output_type=PromptedOutput(GenerationOutput),
            instructions=instructions,
            model_settings=self.model_settings,
            retries=1,
        )
        try:
            response = await agent.run(
                self.prompt(data), usage=self.meter, usage_limits=self.limits
            )
            return response.output.proposal
        except UsageLimitExceeded as exc:
            raise TaskError("model_budget_exceeded") from exc
        except ModelHTTPError as exc:
            raise TaskError(
                "model_unavailable", retryable=exc.status_code == 429 or exc.status_code >= 500
            ) from exc
        except UnexpectedModelBehavior as exc:
            raise TaskError("model_output_invalid") from exc
        except ModelConnectionError as exc:
            raise TaskError("model_unavailable", retryable=True) from exc

    async def review(self, data: dict[str, Any]) -> SemanticReview:
        instructions = (
            files("tsz_agent.capabilities.vocabulary_mcq").joinpath("review.txt").read_text()
        )
        agent: Agent[None, SemanticReview] = Agent(
            BudgetedModel(self.model, self.budget, "review"),
            output_type=PromptedOutput(SemanticReview),
            instructions=instructions,
            model_settings=self.model_settings,
            retries=1,
        )
        try:
            response = await agent.run(
                self.prompt(data), usage=self.meter, usage_limits=self.limits
            )
            return response.output
        except UsageLimitExceeded as exc:
            raise TaskError("model_budget_exceeded") from exc
        except ModelHTTPError as exc:
            raise TaskError(
                "model_unavailable", retryable=exc.status_code == 429 or exc.status_code >= 500
            ) from exc
        except UnexpectedModelBehavior as exc:
            raise TaskError("model_output_invalid") from exc
        except ModelConnectionError as exc:
            raise TaskError("model_unavailable", retryable=True) from exc

    async def close(self) -> None:
        await self.provider.client.close()
