"""Bounded model requests and usage receipts shared by supervisor agents."""

import json
from typing import Any

from openai import AsyncOpenAI

from tsz_agent.config import Settings

OUTPUT_LIMIT = 2400


async def complete(
    settings: Settings, instruction: str, context: dict[str, Any]
) -> tuple[object, dict[str, Any]]:
    if not settings.deepseek_api_key:
        raise ValueError("model_unavailable")
    async with AsyncOpenAI(
        api_key=settings.deepseek_api_key.get_secret_value(),
        base_url=str(settings.supervisor_model_url),
        max_retries=0,
        timeout=30,
    ) as client:
        reply = await client.chat.completions.create(
            model=settings.deepseek_model,
            messages=[
                {"role": "system", "content": instruction},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
            max_tokens=OUTPUT_LIMIT,
            extra_body={"thinking": {"type": "disabled"}},
        )
    usage = reply.usage
    receipt = {
        "model": reply.model,
        "input_tokens": usage.prompt_tokens if usage else None,
        "output_tokens": usage.completion_tokens if usage else None,
    }
    if not reply.choices or reply.choices[0].finish_reason != "stop":
        return None, receipt
    try:
        return json.loads(reply.choices[0].message.content or ""), receipt
    except ValueError:
        return None, receipt
