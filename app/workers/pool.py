"""In-process worker pool: job consumers (Postgres queue) + query consumers (asyncio
queue) + stale-query recovery. One process, asyncio tasks (architecture D1/D6/§2)."""
from __future__ import annotations

import asyncio
import logging

from psycopg.types.json import Jsonb

from ..context import AppContext
from ..services import ingest, retention
from .queue import claim_job, complete_job, fail_job

log = logging.getLogger(__name__)

JOB_HANDLERS = {
    "ingest": ingest.handle_ingest,
    "delta_scan": ingest.handle_delta_scan,
    "reindex_sku": ingest.handle_reindex_sku,
    "retention_sweep": retention.handle_retention_sweep,
    "orphan_gc": retention.handle_orphan_gc,
}

QUERY_WALL_S = 65
STALE_PENDING_S = 20
STALE_RUNNING_S = 600


class WorkerPool:
    def __init__(self, ctx: AppContext, job_workers: int = 2, query_workers: int = 2) -> None:
        self.ctx = ctx
        self.job_workers = job_workers
        self.query_workers = query_workers
        self._tasks: list[asyncio.Task] = []
        self._stopping = False

    async def start(self) -> None:
        for i in range(self.job_workers):
            self._tasks.append(asyncio.create_task(self._job_loop(i), name=f"job-worker-{i}"))
        for i in range(self.query_workers):
            self._tasks.append(asyncio.create_task(self._query_loop(i), name=f"query-worker-{i}"))
        self._tasks.append(asyncio.create_task(self._recovery_loop(), name="query-recovery"))
        log.info("worker pool started (job_workers=%s query_workers=%s)", self.job_workers, self.query_workers)

    async def stop(self) -> None:
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    # ---- jobs ---------------------------------------------------------------
    async def _job_loop(self, index: int) -> None:
        while not self._stopping:
            try:
                job = await claim_job(self.ctx)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("job claim failed")
                await asyncio.sleep(1.0)
                continue
            if not job:
                await asyncio.sleep(0.25)
                continue
            handler = JOB_HANDLERS.get(job["type"])
            if handler is None:
                await fail_job(self.ctx, job, f"unknown job type: {job['type']}")
                continue
            try:
                result = await handler(self.ctx, job)
                await complete_job(self.ctx, job["id"], result if isinstance(result, dict) else {})
            except asyncio.CancelledError:
                await fail_job(self.ctx, job, "worker cancelled; job requeued")
                raise
            except Exception as exc:
                log.exception("job %s (%s) failed", job["id"], job["type"])
                await fail_job(self.ctx, job, f"{type(exc).__name__}: {exc}")

    # ---- queries ------------------------------------------------------------
    async def _query_loop(self, index: int) -> None:
        from ..services.query_pipeline import handle_query, FINAL_STATUSES  # noqa: PLC0415

        while not self._stopping:
            query_id = await self.ctx.query_queue.get()
            if query_id is None:
                continue
            try:
                await asyncio.wait_for(handle_query(self.ctx, query_id), timeout=QUERY_WALL_S)
            except asyncio.TimeoutError:
                log.error("query %s exceeded internal wall", query_id)
                await self.ctx.db.execute(
                    """UPDATE search_queries SET status='failed', error_code='TIMEOUT',
                         result_payload=%s WHERE id=%s AND status <> ALL(%s)""",
                    (Jsonb({"query_id": query_id, "status": "failed",
                            "error": {"code": "TIMEOUT", "message": "Matching timed out. Please retry.",
                                      "details": {}}}),
                     query_id, list(FINAL_STATUSES)),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("query pipeline crashed for %s", query_id)
                try:
                    await self.ctx.db.execute(
                        """UPDATE search_queries SET status='failed', error_code='INTERNAL'
                           WHERE id=%s AND status NOT IN ('done','failed')""",
                        (query_id,),
                    )
                except Exception:
                    log.exception("failed to mark query %s failed", query_id)

    # ---- recovery -----------------------------------------------------------
    async def _recovery_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(30)
            try:
                stale = await self.ctx.db.fetch_all(
                    """SELECT id::text AS id FROM search_queries
                       WHERE status='pending' AND created_at < now() - make_interval(secs => %s)
                       LIMIT 100""",
                    (STALE_PENDING_S,),
                )
                for row in stale:
                    await self.ctx.query_queue.put(row["id"])
                stuck = await self.ctx.db.execute_returning(
                    """UPDATE search_queries SET status='failed', error_code='INTERRUPTED'
                       WHERE status IN ('pending','embedding','retrieving','reranking')
                         AND created_at < now() - make_interval(secs => %s)
                       RETURNING COUNT(*) AS n""",
                    (STALE_RUNNING_S,),
                )
                if stuck and int(stuck["n"] or 0):
                    log.warning("marked %s stuck queries as failed", stuck["n"])
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("query recovery sweep failed")
