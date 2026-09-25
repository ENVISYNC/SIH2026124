"""Async Postgres/PostGIS connection pool.

Raw SQL on purpose: the interesting part of this server IS the spatial SQL
(ST_DWithin / ST_Project / ST_SnapToGrid), so it stays visible rather than hidden
behind an ORM.
"""

from __future__ import annotations

from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from uip_central.config import settings

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "sql" / "001_schema.sql"

pool: AsyncConnectionPool | None = None


async def open_pool() -> AsyncConnectionPool:
    global pool
    pool = AsyncConnectionPool(
        settings.database_url,
        min_size=1,
        max_size=10,
        open=False,
        kwargs={"row_factory": dict_row},
    )
    await pool.open(wait=True, timeout=30)
    return pool


async def close_pool() -> None:
    if pool is not None:
        await pool.close()


async def apply_schema() -> None:
    """Run the idempotent schema on startup so there is no separate migration step."""
    assert pool is not None
    async with pool.connection() as conn:
        await conn.execute(SCHEMA_FILE.read_text())


def get_pool() -> AsyncConnectionPool:
    assert pool is not None, "connection pool is not open"
    return pool
