import json

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr, ValidationError

from tsz_agent import form_candidates as candidates
from tsz_agent.lexicon_assistant import AssistantUnavailable


def request():
    return candidates.CandidatesInput(
        headword="bank",
        local_content="尚未填写发音",
        catalog=[candidates.AllowedPos(pos="noun", name="noun", form_types=["base", "plural"])],
    )


def payload():
    return {
        "pos": [
            {
                "pos": "noun",
                "forms": [
                    {
                        "form_type": "base",
                        "is_regular": True,
                        "uk": {"spelling": "bank", "dict_phonetic": "/bæŋk/"},
                        "us": {"spelling": "bank", "dict_phonetic": "/bæŋk/"},
                    }
                ],
            }
        ],
        "notes": ["AI 候选，请核对"],
    }


async def test_sdk_requests_json_and_returns_reviewable_candidates(settings, monkeypatch):
    sent = []

    def respond(req):
        body = json.loads(req.content)
        sent.append(body)
        return httpx.Response(
            200,
            json={
                "id": "candidate-fixture",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": json.dumps(payload())},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 150, "total_tokens": 250},
            },
        )

    def client(**kwargs):
        assert kwargs["max_retries"] == 0
        return AsyncOpenAI(
            **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        )

    monkeypatch.setattr(candidates, "AsyncOpenAI", client)
    config = settings.model_copy(update={"deepseek_api_key": SecretStr("synthetic-candidate-key")})
    result = await candidates.generate(config, request())
    assert result.pos[0].forms[0].uk.dict_phonetic == "/bæŋk/"
    assert result.input_tokens == 100 and result.output_tokens == 150
    assert sent[0]["response_format"] == {"type": "json_object"}
    assert json.loads(sent[0]["messages"][1]["content"])["local_content"] == "尚未填写发音"
    assert "actual_pron" not in result.model_dump_json()


@pytest.mark.parametrize(
    "change", ["unknown_pos", "unknown_form", "wrong_headword", "duplicate", "write_field"]
)
def test_invalid_proposals_do_not_enter_drafts(change):
    value = payload()
    if change == "unknown_pos":
        value["pos"][0]["pos"] = "unconfigured"
    if change == "unknown_form":
        value["pos"][0]["forms"][0]["form_type"] = "past"
    if change == "wrong_headword":
        value["pos"][0]["forms"][0]["uk"]["spelling"] = "river"
        value["pos"][0]["forms"][0]["us"]["spelling"] = "river"
    if change == "duplicate":
        value["pos"].append(value["pos"][0])
    if change == "write_field":
        value["save"] = True
    with pytest.raises((ValidationError, ValueError)):
        parsed = candidates.CandidatesPayload.model_validate(value)
        candidates.validate_catalog(parsed, request())


async def test_missing_model_does_not_generate_fake_candidates(settings):
    with pytest.raises(AssistantUnavailable):
        await candidates.generate(settings.model_copy(update={"deepseek_api_key": None}), request())
