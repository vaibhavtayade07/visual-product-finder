"""Health endpoint (unauthenticated; architecture §4.2)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..context import AppContext
from ..workers.queue import queue_depth
from .deps import get_ctx

router = APIRouter(tags=["health"])

VERSION = "0.1.0"


@router.get("/health")
async def health(ctx: AppContext = Depends(get_ctx)) -> dict:
    db_ok = await ctx.db.ping()
    depth = await queue_depth(ctx) if db_ok else None
    last_import = None
    if db_ok:
        try:
            row = await ctx.db.fetch_one(
                "SELECT MAX(finished_at) AS ts FROM catalog_imports WHERE status='done'")
            last_import = row["ts"].isoformat() if row and row["ts"] else None
        except Exception:
            last_import = None
    return {
        "status": "ok" if db_ok else "degraded",
        "db": "ok" if db_ok else "down",
        "version": VERSION,
        "queue_depth": depth,
        "last_import_at": last_import,
        "degraded_providers": list(ctx.degraded_providers),
    }
