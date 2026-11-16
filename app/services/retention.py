"""Retention sweep + orphan-vector GC (US-9, AC9.1; §5a daily jobs)."""
from __future__ import annotations

import asyncio
import logging

from ..context import AppContext

log = logging.getLogger(__name__)


async def handle_retention_sweep(ctx: AppContext, job: dict) -> dict:
    """Delete query photos past retention (default 30 d); consent rows survive (AC9.3)."""
    payload = job.get("payload") or {}
    tenant_id = str(payload["tenant_id"])
    cfg = await ctx.config_for(tenant_id)
    days = int(payload.get("older_than_days") or cfg.get("retention_days", 30))

    rows = await ctx.db.fetch_all(
        """SELECT id::text AS id, image_key FROM search_queries
           WHERE tenant_id=%s AND image_key IS NOT NULL
             AND created_at < now() - make_interval(days => %s)
           ORDER BY created_at LIMIT 5000""",
        (tenant_id, days),
    )
    deleted = 0
    for row in rows:
        try:
            await asyncio.to_thread(ctx.storage.delete, row["image_key"])
        except Exception:
            log.warning("retention: failed to delete %s (continuing)", row["image_key"], exc_info=True)
        await ctx.db.execute(
            "UPDATE search_queries SET image_key=NULL, photo_deleted_at=now() WHERE id=%s",
            (row["id"],),
        )
        deleted += 1
    log.info("retention sweep tenant=%s retention_days=%s deleted=%s", tenant_id, days, deleted)
    return {"deleted": deleted, "retention_days": days}


async def handle_orphan_gc(ctx: AppContext, job: dict) -> dict:
    """Delete embeddings no longer referenced by any product_images row (§5a)."""
    payload = job.get("payload") or {}
    tenant_id = str(payload["tenant_id"])
    older_than_hours = int(payload.get("older_than_hours", 24))
    result = await ctx.db.execute_returning(
        """WITH gone AS (
             DELETE FROM image_embeddings e
             WHERE e.tenant_id = %s
               AND e.created_at < now() - make_interval(hours => %s)
               AND NOT EXISTS (SELECT 1 FROM product_images pi WHERE pi.embedding_id = e.id)
             RETURNING 1)
           SELECT COUNT(*) AS deleted FROM gone""",
        (tenant_id, older_than_hours),
    )
    deleted = int(result["deleted"]) if result else 0
    log.info("orphan gc tenant=%s older_than_h=%s deleted=%s", tenant_id, older_than_hours, deleted)
    return {"deleted": deleted}
