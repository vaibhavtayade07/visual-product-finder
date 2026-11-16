"""In-process scheduler (architecture §7): nightly batch, hourly delta, daily retention
sweep + orphan GC. Advisory-locked enqueue so restarts/duplicates can never double-run."""
from __future__ import annotations

import asyncio
import logging
import time

from ..context import AppContext
from .queue import enqueue_job_deduped

log = logging.getLogger(__name__)


async def _with_advisory_lock(ctx: AppContext, key: str, fn) -> None:
    lock_id = abs(hash(key)) % (2**31)
    async with ctx.db.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT pg_try_advisory_lock(%s) AS got", (lock_id,))
            row = await cur.fetchone()
            if not row or not row["got"]:
                return
            try:
                await fn()
            finally:
                await cur.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))


class Scheduler:
    """Interval-driven; each schedule first fires one interval after boot."""

    def __init__(self, ctx: AppContext, *, delta_every_s: float = 3600.0,
                 nightly_every_s: float = 86400.0, retention_every_s: float = 86400.0,
                 orphan_every_s: float = 86400.0) -> None:
        self.ctx = ctx
        self.intervals = {
            "nightly_batch": nightly_every_s,
            "hourly_delta": delta_every_s,
            "retention_sweep": retention_every_s,
            "orphan_gc": orphan_every_s,
        }
        now = time.monotonic()
        self.next_run = {key: now + interval for key, interval in self.intervals.items()}
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="scheduler")
        log.info("scheduler started: %s", {k: int(v) for k, v in self.intervals.items()})

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            now = time.monotonic()
            for key, due in list(self.next_run.items()):
                if now >= due:
                    self.next_run[key] = now + self.intervals[key]
                    try:
                        await self._fire(key)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception("scheduler job %s failed to enqueue", key)
            await asyncio.sleep(15)

    async def _fire(self, key: str) -> None:
        tenant = await self.ctx.default_tenant()
        cfg = await self.ctx.config_for(tenant.id)

        async def enqueue() -> None:
            if key in ("nightly_batch", "hourly_delta"):
                common = {
                    "tenant_id": tenant.id,
                    "adapter": cfg.get("adapters", {}).get("catalog", "csv"),
                    "path": cfg.get("adapters", {}).get("csv_path"),
                    "count": int(cfg.get("adapters", {}).get("mock_count", 300)),
                    # Honored only for full snapshots; delta mode of a true incremental
                    # adapter (delta_is_full=False) never discontinues absent SKUs.
                    "mark_missing_discontinued": True,
                }
                mode = "full" if key == "nightly_batch" else "delta"
                job_id = await enqueue_job_deduped(self.ctx, "delta_scan", tenant.id, {
                    **common,
                    "mode": mode,
                }, match_payload={"mode": mode})
            elif key == "retention_sweep":
                job_id = await enqueue_job_deduped(self.ctx, "retention_sweep", tenant.id,
                                                   {"tenant_id": tenant.id})
            elif key == "orphan_gc":
                job_id = await enqueue_job_deduped(self.ctx, "orphan_gc", tenant.id,
                                                   {"tenant_id": tenant.id})
            else:
                return
            if job_id:
                log.info("scheduler enqueued %s -> job %s", key, job_id)

        await _with_advisory_lock(self.ctx, f"scheduler:{tenant.id}:{key}", enqueue)
