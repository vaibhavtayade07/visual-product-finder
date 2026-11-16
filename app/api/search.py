"""Public search API (architecture §4.1)."""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse

from ..context import AppContext, Tenant
from ..errors import BadRequest, ConsentRequired, NotFound, QueryCapReached, RateLimited
from ..services import query_pipeline
from ..services.images import validate_query_upload
from ..services.popular import get_popular
from ..services.util import sha256_hex, storage_delete, storage_put
from .deps import client_ip, get_ctx, resolve_tenant

router = APIRouter(prefix="/api/v1", tags=["search"])

log = logging.getLogger(__name__)

DEDUPE_WINDOW_MIN = 10
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?[0-9][0-9\s\-()]{6,17}$")


@router.post("/search", status_code=202)
async def create_search(
    request: Request,
    image: UploadFile = File(...),
    session_id: str = Form(...),
    consent_version: str = Form(""),  # missing/empty -> CONSENT_REQUIRED below, not a 422 blob
    tenant: str | None = Form(None),
    ctx: AppContext = Depends(get_ctx),
) -> JSONResponse:
    t: Tenant = await resolve_tenant(request, tenant)
    cfg = await ctx.config_for(t.id)

    limit = int(cfg.get("rate_limit", {}).get("ip_per_min", 10))
    if not ctx.rate_allowed(f"{t.id}:{client_ip(request)}", limit):
        raise RateLimited("Too many searches from this connection. Please wait a minute.")
    if not consent_version.strip():
        raise ConsentRequired("Consent is required before uploading a photo.")

    data = await image.read(10 * 1024 * 1024 + 1)
    normalized, _, _ = validate_query_upload(data)  # raises 413/415/400 (AC1.1, E12)
    image_hash = sha256_hex(normalized)

    # same-photo dedupe (E13): return the in-flight/recent query for this session
    existing = await ctx.db.fetch_one(
        """SELECT id::text AS id, status FROM search_queries
           WHERE tenant_id=%s AND image_hash=%s AND session_id=%s AND status <> 'failed'
             AND created_at > now() - make_interval(mins => %s)
           ORDER BY created_at DESC LIMIT 1""",
        (t.id, image_hash, session_id, DEDUPE_WINDOW_MIN),
    )
    if existing:
        return JSONResponse(status_code=202, content={
            "query_id": existing["id"], "status": existing["status"], "poll_after_ms": 1500,
            "deduplicated": True,
        })

    # cost guard (fail-closed atomic counter; block -> 429)
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    counter = await ctx.db.execute_returning(
        """INSERT INTO usage_counters (tenant_id, period, queries) VALUES (%s, %s, 1)
           ON CONFLICT (tenant_id, period) DO UPDATE SET queries = usage_counters.queries + 1
           RETURNING queries""",
        (t.id, period),
    )
    cap = int(cfg.get("query_cap_monthly", 10000))
    guard = str(cfg.get("cost_guard", "block"))
    if counter and int(counter["queries"]) > cap:
        if guard == "block":
            raise QueryCapReached(
                "This month's search limit was reached. Please try again next month.",
                details={"cap": cap},
            )
        log.warning(
            "cost guard %s: tenant=%s queries=%s cap=%s", guard, t.id, counter["queries"], cap)

    query_id = str(uuid.uuid4())
    image_key = f"query/{query_id}.jpg"
    await storage_put(ctx, image_key, normalized)

    retention_days = int(cfg.get("retention_days", 30))
    await ctx.db.execute(
        """INSERT INTO search_queries (id, tenant_id, session_id, image_key, image_hash,
                                       consent_version, status, degraded, keep_for_eval,
                                       expires_at, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, 'pending', false, false,
                   now() + make_interval(days => %s), now())""",
        (query_id, t.id, session_id, image_key, image_hash, consent_version, retention_days),
    )
    await ctx.db.execute(
        """INSERT INTO consents (tenant_id, session_id, consent_version, consented_at)
           SELECT %s, %s, %s, now()
           WHERE NOT EXISTS (SELECT 1 FROM consents
                             WHERE tenant_id=%s AND session_id=%s AND consent_version=%s)""",
        (t.id, session_id, consent_version, t.id, session_id, consent_version),
    )
    ctx.query_queue.put_nowait(query_id)
    return JSONResponse(status_code=202, content={
        "query_id": query_id, "status": "pending", "poll_after_ms": 1500,
    })


@router.get("/search/{query_id}")
async def get_search(query_id: str, ctx: AppContext = Depends(get_ctx)) -> dict:
    row = await ctx.db.fetch_one("SELECT * FROM search_queries WHERE id=%s", (query_id,))
    if not row:
        raise NotFound("unknown query_id")
    return query_pipeline.public_payload(row)


async def _refine_impl(request: Request, query_id: str, ctx: AppContext) -> dict:
    body = await request.json()
    filters = body.get("filters") or {}
    clean = {
        "finish": [str(v) for v in (filters.get("finish") or [])],
        "color": [str(v) for v in (filters.get("color") or [])],
        "size_class": [str(v) for v in (filters.get("size_class") or [])],
        "price_min": filters.get("price_min"),
        "price_max": filters.get("price_max"),
        "in_stock_only": bool(filters.get("in_stock_only")),
    }
    return await query_pipeline.refine(ctx, query_id, clean, body.get("top_n"))


@router.post("/search/{query_id}/refine")
async def refine_search(query_id: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> dict:
    return await _refine_impl(request, query_id, ctx)


@router.get("/popular")
async def popular(request: Request, category: str | None = Query(None), limit: int = Query(6, ge=1, le=12),
                  tenant: str | None = Query(None), ctx: AppContext = Depends(get_ctx)) -> dict:
    t = await resolve_tenant(request, tenant)
    cfg = await ctx.config_for(t.id)
    items = await get_popular(ctx, t.id, cfg, category=category, limit=limit)
    return {"items": items}


@router.post("/leads", status_code=201)
async def create_lead(request: Request, ctx: AppContext = Depends(get_ctx)) -> dict:
    body = await request.json()
    session_id = str(body.get("session_id") or "").strip()
    if not session_id:
        raise BadRequest("session_id is required.", details={"field": "session_id"})
    email = str(body.get("email") or "").strip()
    phone = str(body.get("phone") or "").strip()
    name = str(body.get("name") or "").strip() or None
    note = str(body.get("note") or "").strip() or None
    errors: dict[str, str] = {}
    if email and not EMAIL_RE.match(email):
        errors["email"] = "Enter a valid email address."
    if phone and not PHONE_RE.match(phone):
        errors["phone"] = "Enter a valid phone number."
    if not email and not phone:
        errors["contact"] = "Add an email or phone number so we can reach you."
    if errors:
        raise BadRequest("Please check the contact details.", details={"fields": errors})
    tenant_row = await ctx.db.fetch_one(
        "SELECT id::text AS id FROM tenants WHERE slug=%s", (ctx.settings.default_tenant_slug,))
    query_id = body.get("search_query_id")
    if query_id:
        exists = await ctx.db.fetch_one("SELECT 1 AS ok FROM search_queries WHERE id=%s", (query_id,))
        if not exists:
            query_id = None
    lead = await ctx.db.execute_returning(
        """INSERT INTO leads (tenant_id, search_query_id, session_id, name, email, phone, note, status, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, 'new', now()) RETURNING id::text AS id""",
        (tenant_row["id"], query_id, session_id, name, email or None, phone or None, note),
    )
    return {"lead_id": lead["id"], "message": "We'll contact you if we find a match"}


@router.post("/events/click", status_code=202)
async def log_click(request: Request, ctx: AppContext = Depends(get_ctx)) -> dict:
    body = await request.json()
    sku = str(body.get("sku") or "").strip()
    session_id = str(body.get("session_id") or "").strip()
    if not sku or not session_id:
        raise BadRequest("session_id and sku are required.")
    try:
        position = int(body.get("position") or 0)
    except (TypeError, ValueError):
        raise BadRequest("position must be an integer.") from None
    tenant_row = await ctx.db.fetch_one(
        "SELECT id::text AS id FROM tenants WHERE slug=%s", (ctx.settings.default_tenant_slug,))
    query_id = body.get("query_id")
    if query_id:
        exists = await ctx.db.fetch_one("SELECT 1 AS ok FROM search_queries WHERE id=%s", (query_id,))
        if not exists:
            query_id = None
    await ctx.db.execute(
        """INSERT INTO clicks (tenant_id, search_query_id, session_id, sku, position, created_at)
           VALUES (%s, %s, %s, %s, %s, now())""",
        (tenant_row["id"], query_id, session_id, sku, position),
    )
    return {}


@router.delete("/search/{query_id}", status_code=202)
async def delete_query_photo(query_id: str, request: Request, session_id: str = Query(...),
                             ctx: AppContext = Depends(get_ctx)) -> dict:
    """AC9.4: delete-my-photo. Row + consent survive; only the photo object goes (AC9.3)."""
    row = await ctx.db.fetch_one(
        "SELECT id::text AS id, session_id, image_key FROM search_queries WHERE id=%s", (query_id,))
    if not row or row["session_id"] != session_id:
        raise NotFound("unknown query_id for this session")
    if row["image_key"]:
        try:
            await storage_delete(ctx, row["image_key"])
        except Exception:
            pass
    await ctx.db.execute(
        "UPDATE search_queries SET image_key=NULL, photo_deleted_at=now() WHERE id=%s", (query_id,))
    return {"deleted": True}
