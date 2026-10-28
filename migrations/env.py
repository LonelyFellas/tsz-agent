import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from tsz_agent.storage import Base


def migrate(connection):
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


async def main():
    engine = create_async_engine(os.environ["TSZ_AGENT_DATABASE_URL"])
    async with engine.connect() as connection:
        await connection.run_sync(migrate)
    await engine.dispose()


asyncio.run(main())
