"""Postgres access via a psycopg3 async pool. Raw SQL only (architecture D1)."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        # psycopg accepts plain postgresql:// URLs for psycopg3 driver.
        self._pool = AsyncConnectionPool(
            url, min_size=1, max_size=12, open=False, kwargs={"row_factory": dict_row}
        )

    async def open(self) -> None:
        await self._pool.open()

    async def close(self) -> None:
        await self._pool.close()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Any]:
        async with self._pool.connection() as conn:
            yield conn

    async def fetch_all(self, sql: str, params: tuple | list = ()) -> list[dict]:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                return await cur.fetchall()

    async def fetch_one(self, sql: str, params: tuple | list = ()) -> dict | None:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                return await cur.fetchone()

    async def execute(self, sql: str, params: tuple | list = ()) -> None:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
            await conn.commit()

    async def execute_returning(self, sql: str, params: tuple | list = ()) -> dict | None:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                row = await cur.fetchone()
            await conn.commit()
            return row

    async def ping(self) -> bool:
        try:
            await self.fetch_one("SELECT 1 AS ok")
            return True
        except Exception:
            return False


def j(value: Any) -> Jsonb:
    """Wrap a Python value for a jsonb column."""
    return Jsonb(value)
