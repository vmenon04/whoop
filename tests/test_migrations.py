from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from whoop_lab.models import Base


def _diff(connection: Connection) -> list[object]:
    context = MigrationContext.configure(connection, opts={"compare_type": True})
    return list(compare_metadata(context, Base.metadata))


async def test_migrations_match_models(engine: AsyncEngine) -> None:
    """Hand-written migrations must produce exactly the schema the models describe."""
    async with engine.connect() as conn:
        diff = await conn.run_sync(_diff)
    assert diff == []
