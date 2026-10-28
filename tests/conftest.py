import os
from collections.abc import AsyncIterator
from urllib.parse import urlsplit
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from tsz_agent.config import Settings
from tsz_agent.schemas import Candidate, GenerationContext, SourceRef
from tsz_agent.storage import Store


def candidate(number: int, word: str, definition: str, **changes):
    base = dict(
        candidate_ref=f"candidate_{number}",
        source=SourceRef(
            entry_id=UUID(int=number),
            publication_id=UUID(int=number + 100),
            pos_id=UUID(int=number + 200),
            sense_id=UUID(int=number + 300),
            definition_id=UUID(int=number + 400),
            form_id=UUID(int=number + 500),
        ),
        content_fingerprint=f"{number:064x}",
        spelling=word,
        part_of_speech="verb",
        level="A1",
        definition=definition,
        answer_profile="BrE",
        teaching_eligible=True,
    )
    base.update(changes)
    return Candidate(**base)


@pytest.fixture
def context():
    return GenerationContext(
        external_request_id=UUID(int=999),
        target=candidate(1, "begin", "开始"),
        question_level="A1",
        answer_profile="BrE",
    )


@pytest.fixture
def pool():
    return [
        candidate(2, "finish", "完成"),
        candidate(3, "wait", "等待"),
        candidate(4, "stop", "停止"),
    ]


@pytest.fixture
def generation_data(context, pool):
    return {
        "context": context.model_dump(mode="json"),
        "candidate_pool": [c.model_dump(mode="json") for c in pool],
        "reviewed_examples": [],
    }


@pytest.fixture
async def store() -> AsyncIterator[Store]:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for isolated PostgreSQL tests")
    parsed = urlsplit(url)
    if parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.path != "/tsz_agent_test":
        pytest.fail("tests only accept loopback /tsz_agent_test; never use a business database")
    result = Store(url)
    async with result.sessions.begin() as session:
        await session.execute(
            text(
                "TRUNCATE agent_model_setting, agent_model_calls, agent_review_events, agent_review_current, agent_runs"
            )
        )
    yield result
    await result.close()


@pytest.fixture
def settings():
    return Settings(
        database_url=SecretStr(
            os.getenv("TEST_DATABASE_URL", "postgresql+psycopg://test@localhost/tsz_agent_test")
        ),
        _env_file=None,
        DEEPSEEK_API_KEY=None,
        api_token=SecretStr("test-agent-token-" + "x" * 32),
        tsz_base_url="http://tsz.test",
        tsz_service_token=SecretStr("test-tsz-token-" + "x" * 32),
    )
