"""Deployment-only checks. Run inside the selected Agent image."""

import asyncio
import subprocess
import sys

import httpx
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from tsz_agent.config import Settings


class CheckError(Exception):
    pass


async def schema(settings: Settings, expected_database: str) -> tuple[str, str]:
    url = make_url(settings.database_url.get_secret_value())
    if url.database != expected_database:
        raise CheckError("unexpected_database")
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    head = script.get_current_head()
    if head is None:
        raise CheckError("missing_image_revision")
    revisions = {revision.revision for revision in script.walk_revisions()}
    engine = create_async_engine(url, connect_args={"connect_timeout": 5})
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SET LOCAL statement_timeout = '5s'"))
            database, has_runs, has_version = (
                await connection.execute(
                    text(
                        "SELECT current_database(), to_regclass('agent_runs') IS NOT NULL, "
                        "to_regclass('alembic_version') IS NOT NULL"
                    )
                )
            ).one()
            if database != expected_database or not has_runs or not has_version:
                raise CheckError("database_is_not_initialized_agent")
            current: object = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            if not isinstance(current, str) or current not in revisions:
                raise CheckError("unknown_database_revision")
            return current, head
    finally:
        await engine.dispose()


async def main(args: list[str]) -> None:
    settings = Settings()
    match args:
        case ["schema", expected_database]:
            print(*await schema(settings, expected_database))
        case ["migrate", expected_database, expected_revision]:
            current, _ = await schema(settings, expected_database)
            if current != expected_revision:
                raise CheckError("database_revision_changed")
            result = subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=120,
            )
            if result.returncode != 0:
                raise CheckError("alembic_upgrade_failed")
            current, head = await schema(settings, expected_database)
            if current != head:
                raise CheckError("database_revision_not_current")
            print(current)
        case ["ready"]:
            async with httpx.AsyncClient(trust_env=False, timeout=4) as client:
                response = await client.get(
                    "http://127.0.0.1:8390/readyz",
                    headers={"Authorization": f"Bearer {settings.api_token.get_secret_value()}"},
                )
                if response.status_code != 200 or response.json() != {"status": "ready"}:
                    raise CheckError("api_not_ready")
        case _:
            raise CheckError("invalid_deployment_arguments")


if __name__ == "__main__":
    try:
        asyncio.run(main(sys.argv[1:]))
    except CheckError as exc:
        print(f"Agent deployment check: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Agent deployment check failed: {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
