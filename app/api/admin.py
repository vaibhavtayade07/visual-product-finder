"""Admin API (architecture §4.2). Bearer ADMIN_TOKEN required on all routes."""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from ..context import AppContext, Tenant, deep_merge
from ..errors import BadRequest, NotFound
from ..services import hygiene as hygiene_service
from ..workers.queue import enqueue_job
from .deps import get_ctx, require_admin, resolve_tenant

router = APIRouter(prefix="/api/v1/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.post("/imports", status_code=202)
async def create_import(request: Request, ctx: AppContext = Depends(get_ctx)) -> JSONResponse:
    t: Tenant = await resolve_tenant(request)
    content_type = (request.headers.get("content-type") or "").lower()

    if "multipart/form-data" in content_type:
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise BadRequest("multipart form must carry a `file` part (CSV).")
        raw = await upload.read()
        if not raw:
            raise BadRequest("uploaded CSV is empty.")
        import_id = str(uuid.uuid4())
        upload_dir = ctx.settings.data_dir / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        csv_path = upload_dir / f"{import_id}.csv"
        csv_path.write_bytes(raw)

        # validate BEFORE indexing starts (AC6.4): malformed rows carry row numbers
        from ..adapters.csv_adapter import CsvAdapter  # noqa: PLC0415

        try:
            adapter = CsvAdapter(t.id, csv_path)
            rows, errors = adapter._parse()  # noqa: SLF001 - pre-index validation by design
        except ValueError as exc:
            raise BadRequest(str(exc), details={"reason": "missing_columns"}) from exc

        await ctx.db.execute(
            """INSERT INTO catalog_imports (id, tenant_id, kind, filename, status, total_rows,
                                            imported, skipped, failed, validation_report, stats,
                                            created_by, started_at)
               VALUES (%s, %s, 'full', %s, 'validating', %s, 0, 0, 0, %s, '{}', 'admin', now())""",
            (import_id, t.id, upload.filename or csv_path.name, len(rows) + len(errors),
             _jsonb(errors)),
        )
        await _remember_csv_path(ctx, t, str(csv_path))
        job_id = await enqueue_job(ctx, "ingest", t.id, {
            "tenant_id": t.id, "import_id": import_id, "adapter": "csv", "path": str(csv_path),
        })
        return JSONResponse(status_code=202, content={
            "import_id": import_id, "job_id": job_id, "status": "validating",
            "validation_errors": errors,
        })

    body = await request.json()
    adapter_type = str(body.get("adapter") or "").lower()
    if adapter_type == "mock":
        count = int(body.get("count") or 300)
        if not (1 <= count <= 5000):
            raise BadRequest("count must be between 1 and 5000.")
        import_id = str(uuid.uuid4())
        await ctx.db.execute(
            """INSERT INTO catalog_imports (id, tenant_id, kind, filename, status, total_rows,
                                            imported, skipped, failed, validation_report, stats,
                                            created_by, started_at)
               VALUES (%s, %s, 'mock', %s, 'validating', %s, 0, 0, 0, '[]', '{}', 'admin', now())""",
            (import_id, t.id, f"mock catalog x{count}", count),
        )
        job_id = await enqueue_job(ctx, "ingest", t.id, {
            "tenant_id": t.id, "import_id": import_id, "adapter": "mock", "count": count,
        })
    elif adapter_type == "shopify":
        # Validate credentials before enqueueing: a missing env var must be an
        # actionable 400, not a failed background job with a stack trace.
        from ..adapters import build_adapter  # noqa: PLC0415

        try:
            adapter = build_adapter(
                "shopify", tenant_id=t.id, public_base_url=ctx.settings.public_base_url
            )
        except ValueError as exc:
            raise BadRequest(str(exc), details={"reason": "shopify_not_configured"}) from exc
        import_id = str(uuid.uuid4())
        await ctx.db.execute(
            """INSERT INTO catalog_imports (id, tenant_id, kind, filename, status, total_rows,
                                            imported, skipped, failed, validation_report, stats,
                                            created_by, started_at)
               VALUES (%s, %s, 'full', %s, 'validating', 0, 0, 0, 0, '[]', '{}', 'admin', now())""",
            (import_id, t.id, f"shopify:{adapter.shop_domain}"),
        )
        job_id = await enqueue_job(ctx, "ingest", t.id, {
            "tenant_id": t.id, "import_id": import_id, "adapter": "shopify",
        })
    else:
        raise BadRequest(
            "JSON import body must be {\"adapter\": \"mock\", \"count\": N}, "
            "{\"adapter\": \"shopify\"} or a CSV upload."
        )
    return JSONResponse(status_code=202, content={
        "import_id": import_id, "job_id": job_id, "status": "validating", "validation_errors": [],
    })


@router.get("/imports/{import_id}")
async def import_status(import_id: str, ctx: AppContext = Depends(get_ctx)) -> dict:
    row = await ctx.db.fetch_one("SELECT * FROM catalog_imports WHERE id=%s", (import_id,))
    if not row:
        raise NotFound("unknown import_id")
    total = int(row["total_rows"] or 0)
    done = int(row["imported"] or 0) + int(row["skipped"] or 0) + int(row["failed"] or 0)
    stats = row["stats"] or {}
    return {
        "import_id": str(row["id"]),
        "status": row["status"],
        "kind": row["kind"],
        "progress_pct": round(100.0 * done / total, 1) if total else (100.0 if row["status"] == "done" else 0.0),
        "counts": {"total": total, "imported": int(row["imported"] or 0),
                   "skipped": int(row["skipped"] or 0), "failed": int(row["failed"] or 0)},
        "reembedded": int(stats.get("reembedded", 0)),
        "skipped_unchanged": int(stats.get("skipped_unchanged", 0)),
        "coverage_pct": stats.get("coverage_pct"),
        "validation_errors": row["validation_report"] or [],
        "started_at": row["started_at"].isoformat() if row["started_at"] else None,
        "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
    }


@router.get("/hygiene")
async def hygiene(request: Request, format: str = "json", ctx: AppContext = Depends(get_ctx)):
    t: Tenant = await resolve_tenant(request)
    cfg = await ctx.config_for(t.id)
    report = await hygiene_service.compute_hygiene(ctx, t.id, cfg)
    if format == "csv":
        csv_text = hygiene_service.hygiene_to_csv(report)
        return Response(content=csv_text, media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=hygiene.csv"})
    report["last_import"] = await _last_import(ctx, t.id)
    return report


@router.post("/skus/{sku}/reindex", status_code=202)
async def reindex_sku(sku: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> dict:
    t: Tenant = await resolve_tenant(request)
    product = await ctx.db.fetch_one(
        "SELECT id::text AS id FROM products WHERE tenant_id=%s AND sku=%s", (t.id, sku))
    if not product:
        raise NotFound(f"unknown sku: {sku}")
    job_id = await enqueue_job(ctx, "reindex_sku", t.id, {"tenant_id": t.id, "sku": sku})
    return {"job_id": job_id}


@router.get("/config")
async def get_config(request: Request, ctx: AppContext = Depends(get_ctx)) -> dict:
    t: Tenant = await resolve_tenant(request)
    return {"config": await ctx.config_for(t.id), "overrides": t.config}


@router.put("/config")
async def put_config(request: Request, ctx: AppContext = Depends(get_ctx)) -> dict:
    t: Tenant = await resolve_tenant(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise BadRequest("config body must be a JSON object.")
    if "config" in body and isinstance(body["config"], dict):
        body = body["config"]
    merged = deep_merge(t.config or {}, body)
    await ctx.db.execute("UPDATE tenants SET config=%s WHERE id=%s", (_jsonb(merged), t.id))
    ctx.invalidate_config(t.id)
    fresh = await ctx.get_tenant_by_slug(t.slug)
    return {"config": await ctx.config_for(t.id), "overrides": (fresh.config if fresh else merged)}


async def _remember_csv_path(ctx: AppContext, tenant: Tenant, path: str) -> None:
    merged = deep_merge(tenant.config or {}, {"adapters": {"catalog": "csv", "csv_path": path}})
    await ctx.db.execute("UPDATE tenants SET config=%s WHERE id=%s", (_jsonb(merged), tenant.id))
    ctx.invalidate_config(tenant.id)


async def _last_import(ctx: AppContext, tenant_id: str) -> dict | None:
    row = await ctx.db.fetch_one(
        """SELECT id::text AS id, status, finished_at FROM catalog_imports
           WHERE tenant_id=%s ORDER BY COALESCE(finished_at, started_at) DESC NULLS LAST LIMIT 1""",
        (tenant_id,),
    )
    if not row:
        return None
    return {"import_id": row["id"], "status": row["status"],
            "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None}


def _jsonb(value):
    from psycopg.types.json import Jsonb  # noqa: PLC0415

    return Jsonb(value)
