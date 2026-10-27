"""Export contracts without production configuration, network or database access."""

import argparse
import asyncio
import json
from pathlib import Path

from pydantic import BaseModel, SecretStr
from pydantic.json_schema import models_json_schema

from tsz_agent.app import create_app
from tsz_agent.config import Settings
from tsz_agent.schemas import CandidatePage, CandidateSearch, EntriesRequest, GenerationContext
from tsz_agent.storage import Store


async def export(check: bool) -> None:
    settings = Settings(
        database_url=SecretStr("postgresql+psycopg://schema@localhost/tsz_agent"),
        api_token=SecretStr("openapi-only-not-a-real-service-token"),
        tsz_base_url="http://localhost:8383",
        tsz_service_token=SecretStr("openapi-only-not-a-real-service-token"),
        _env_file=None,
    )
    store = Store(settings.database_url.get_secret_value())
    try:
        spec = create_app(settings, store).openapi()
    finally:
        await store.close()
    source_models: list[type[BaseModel]] = [
        GenerationContext,
        CandidateSearch,
        CandidatePage,
        EntriesRequest,
    ]
    _, source = models_json_schema(
        [(model, "validation") for model in source_models],
        title="Proposed tsz-rust internal Agent contract v1",
    )
    for name, content in [("openapi.json", spec), ("tsz-input.schema.json", source)]:
        path = Path("docs") / name
        output = json.dumps(content, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if check:
            if not path.exists() or path.read_text() != output:
                raise SystemExit(f"contract drift: {path}")
        else:
            path.parent.mkdir(exist_ok=True)
            path.write_text(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    asyncio.run(export(parser.parse_args().check))
