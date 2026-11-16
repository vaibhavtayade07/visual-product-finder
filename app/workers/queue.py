"""Postgres job queue (D6): FOR UPDATE SKIP LOCKED, retries with backoff."""
from __future__ import annotations

import socket

from psycopg.types.json import Jsonb

from ..context import AppContext

WORKER_ID = f"{socket.gethostname()}-{id(object()) % 10000}"

CLAIM_SQL = """
UPDATE jobs SET status='running', attempts=attempts+1, locked_at=now(), locked_by=%s, updated_at=now()
WHERE id = (
    SELECT id FROM jobs
    WHERE status='queued' AND run_after <= now()
    ORDER BY created_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1)
RETURNING id::text AS id, type, tenant_id::text AS tenant_id, payload, attempts, max_attempts
"""


async def enqueue_job(ctx: AppContext, job_type: str, tenant_id: str, payload: dict | None = None,
                      *, max_attempts: int = 3) -> str:
    row = await ctx.db.execute_returning(
        """INSERT INTO jobs (type, tenant_id, payload, status, attempts, max_attempts, run_after, created_at, updated_at)
           VALUES (%s, %s, %s, 'queued', 0, %s, now(), now(), now()) RETURNING id::text AS id""",
        (job_type, tenant_id, Jsonb(payload or {}), max_attempts),
    )
    return row["id"]


async def enqueue_job_deduped(ctx: AppContext, job_type: str, tenant_id: str,
                              payload: dict | None = None, *,
                              match_payload: dict | None = None) -> str | None:
    """Skip enqueue when the same job type is already queued/running for the tenant.

    `match_payload` narrows the check to jobs whose payload contains that subset
    (e.g. `{"mode": "full"}` vs `{"mode": "delta"}`), so distinct modes of the same
    job type never swallow each other.
    """
    if match_payload:
        existing = await ctx.db.fetch_one(
            """SELECT id::text AS id FROM jobs
               WHERE tenant_id=%s AND type=%s AND status IN ('queued','running')
                 AND payload @> %s::jsonb LIMIT 1""",
            (tenant_id, job_type, Jsonb(match_payload)),
        )
    else:
        existing = await ctx.db.fetch_one(
            "SELECT id::text AS id FROM jobs WHERE tenant_id=%s AND type=%s AND status IN ('queued','running') LIMIT 1",
            (tenant_id, job_type),
        )
    if existing:
        return None
    return await enqueue_job(ctx, job_type, tenant_id, payload)


async def claim_job(ctx: AppContext) -> dict | None:
    return await ctx.db.execute_returning(CLAIM_SQL, (WORKER_ID,))


async def complete_job(ctx: AppContext, job_id: str, result: dict | None = None) -> None:
    await ctx.db.execute(
        "UPDATE jobs SET status='done', result=%s, updated_at=now() WHERE id=%s",
        (Jsonb(result or {}), job_id),
    )


async def fail_job(ctx: AppContext, job: dict, error: str) -> None:
    attempts = int(job.get("attempts") or 0)
    max_attempts = int(job.get("max_attempts") or 3)
    if attempts < max_attempts:
        await ctx.db.execute(
            """UPDATE jobs SET status='queued', run_after=now() + make_interval(secs => %s),
                 last_error=%s, updated_at=now() WHERE id=%s""",
            (min(60, 5 * attempts), error[:2000], job["id"]),
        )
    else:
        await ctx.db.execute(
            "UPDATE jobs SET status='failed', last_error=%s, updated_at=now() WHERE id=%s",
            (error[:2000], job["id"]),
        )


async def queue_depth(ctx: AppContext) -> int:
    row = await ctx.db.fetch_one("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued','running')")
    return int(row["n"] or 0) if row else 0
