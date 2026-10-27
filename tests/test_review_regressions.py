import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr
from pydantic_ai.models.openai import OpenAIChatModel

from tsz_agent.app import create_app
from tsz_agent.capabilities.vocabulary_mcq.workflow import knowledge
from tsz_agent.clients.tsz import TaskError
from tsz_agent.models.budget import MemoryBudget
from tsz_agent.models.deepseek import DeepSeekModel
from tsz_agent.schemas import ReviewEvent, ReviewExample, Usage


async def mocked_model(settings, handler, previous=None, **kwargs):
    config = settings.model_copy(update={"deepseek_api_key": SecretStr("mock-no-network")})
    model = DeepSeekModel(config, previous or Usage(), **kwargs)
    await model.close()
    model.provider._client = AsyncOpenAI(
        api_key="mock-no-network",
        base_url="https://deepseek.test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    model.model = OpenAIChatModel(
        "deepseek-flash", provider=model.provider, profile=model.model.profile
    )
    return model


def completion(content=None, output_tokens=20):
    if content is None:
        content = {"verdict": "no_obvious_issue", "issues": []}
    return httpx.Response(
        200,
        json={
            "id": "mock",
            "object": "chat.completion",
            "created": 1,
            "model": "deepseek-flash",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(content)},
                }
            ],
            "usage": {
                "prompt_tokens": 50,
                "completion_tokens": output_tokens,
                "total_tokens": 50 + output_tokens,
            },
        },
    )


async def test_output_cap_uses_native_field_and_remaining_budget(settings):
    bodies = []

    async def respond(request):
        bodies.append(json.loads(request.content))
        return completion(output_tokens=100)

    model = await mocked_model(settings, respond, Usage(output_tokens=5900))
    try:
        await model.review({"original_prompt": "开始"})
        assert bodies[0].get("max_tokens") == 100
        assert "max_completion_tokens" not in bodies[0]
        with pytest.raises(TaskError, match="model_budget_exceeded"):
            await model.review({"original_prompt": "开始"})
        assert len(bodies) == 1
    finally:
        await model.close()


async def test_timeout_consumes_request_budget(settings):
    calls = []

    async def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic", request=request)

    config = settings.model_copy(update={"model_request_limit": 2})
    model = await mocked_model(config, timeout, Usage(requests=1))
    try:
        with pytest.raises(TaskError, match="model_unavailable"):
            await model.review({"original_prompt": "开始"})
        with pytest.raises(TaskError, match="model_budget_exceeded"):
            await model.review({"original_prompt": "开始"})
        assert len(calls) == 1
        assert model.usage().requests == 2
    finally:
        await model.close()


async def test_structured_output_repair_uses_new_remaining_allowance(settings):
    limits = []

    async def respond(request):
        limits.append(json.loads(request.content)["max_tokens"])
        return (
            completion({"invalid": True}, 60) if len(limits) == 1 else completion(output_tokens=40)
        )

    model = await mocked_model(settings, respond, Usage(output_tokens=5900))
    try:
        await model.review({"original_prompt": "开始"})
        assert limits == [100, 40]
        assert model.usage().requests == 2
        assert model.usage().output_tokens == 6000
    finally:
        await model.close()


async def test_response_without_usage_does_not_release_unknown_output_budget(settings):
    calls = []

    async def respond(request):
        calls.append(request)
        data = completion().json()
        del data["usage"]
        return httpx.Response(200, json=data)

    config = settings.model_copy(update={"output_token_limit": 3000})
    model = await mocked_model(config, respond)
    try:
        with pytest.warns(match="Some responses omitted usage information"):
            await model.review({"original_prompt": "开始"})
        assert model.usage().requests == 1 and model.usage().output_tokens == 0
        assert model.usage().cost is None
        with pytest.raises(TaskError, match="model_budget_exceeded"):
            await model.review({"original_prompt": "开始"})
        assert len(calls) == 1
    finally:
        await model.close()


async def test_cancellation_waits_for_already_received_usage(settings):
    recording, release = asyncio.Event(), asyncio.Event()

    class SlowReceipt(MemoryBudget):
        async def record(self, call_id, input_tokens, output_tokens):
            recording.set()
            await release.wait()
            await super().record(call_id, input_tokens, output_tokens)

    budget = SlowReceipt(Usage(), 4, 6000)

    async def respond(request):
        return completion(output_tokens=100)

    model = await mocked_model(settings, respond, budget=budget)
    task = asyncio.create_task(model.review({"original_prompt": "开始"}))
    try:
        await asyncio.wait_for(recording.wait(), timeout=3)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert budget.usage().requests == 1
        assert budget.usage().input_tokens == 50 and budget.usage().output_tokens == 100
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await model.close()


async def test_unexpected_exception_never_reaches_server(settings, caplog):
    # No database operations are performed by this endpoint.
    app = create_app(settings, SimpleNamespace())

    @app.get("/unexpected")
    async def unexpected():
        raise ValueError("synthetic-private-value-do-not-log")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app, raise_app_exceptions=True), base_url="http://agent.test"
    ) as client:
        response = await client.get("/unexpected")
        assert response.status_code == 500
        assert response.json()["code"] == "internal_error"
        assert response.headers["x-request-id"]
        assert "synthetic-private-value" not in response.text
    assert "synthetic-private-value" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


async def test_optional_example_denied_but_current_context_is_valid(context, pool):
    event = ReviewEvent(
        event_id=uuid4(),
        review_id=uuid4(),
        review_revision=1,
        source_run_id=uuid4(),
        question_revision_id=uuid4(),
        decision="approved",
        reason_codes=[],
        example=ReviewExample(
            target=context.target,
            question_level="A1",
            prompt="开始",
            distractors=pool,
            lesson="已审核案例",
        ),
    )

    class Examples:
        async def examples(self):
            return [event]

    class Source:
        async def check_sources(self, request_id, candidates):
            raise TaskError("authorization_revoked")

        async def context(self, request_id):
            return context

    assert await knowledge(Examples(), Source(), context) == []


@pytest.mark.parametrize("failure", ["authorization_revoked", "source_changed", "tsz_unavailable"])
async def test_example_denial_does_not_hide_actual_context_failure(context, pool, failure):
    event = ReviewEvent(
        event_id=uuid4(),
        review_id=uuid4(),
        review_revision=1,
        source_run_id=uuid4(),
        question_revision_id=uuid4(),
        decision="approved",
        reason_codes=[],
        example=ReviewExample(
            target=context.target,
            question_level="A1",
            prompt="开始",
            distractors=pool,
            lesson="案例",
        ),
    )

    class Examples:
        async def examples(self):
            return [event]

    class Source:
        async def check_sources(self, request_id, candidates):
            raise TaskError("authorization_revoked")

        async def context(self, request_id):
            if failure == "source_changed":
                return context.model_copy(update={"question_level": "A2"})
            raise TaskError(failure, retryable=failure == "tsz_unavailable")

    with pytest.raises(TaskError, match=failure) as caught:
        await knowledge(Examples(), Source(), context)
    assert caught.value.retryable == (failure == "tsz_unavailable")
