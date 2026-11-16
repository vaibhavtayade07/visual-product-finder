"""Ingestion pipeline (architecture §5a): fetch -> normalize -> gate -> hash -> embed ->
upsert -> enrich -> hygiene. Adapter-agnostic; idempotent by (tenant, image_hash, model)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx
from psycopg.types.json import Jsonb

from ..adapters import build_adapter
from ..context import AppContext, mock_image_dir
from ..errors import DecodeFailed
from ..lexicon import CRITICAL_TAG_TYPES
from . import hygiene as hygiene_service
from .images import load_image, make_thumb, normalize_jpeg, quality_gate
from .util import sha256_hex, storage_put

log = logging.getLogger(__name__)
BATCH = 32
FETCH_CONCURRENCY = 8
TAG_TYPES = {"type", "style", "finish", "color", "material", "size_class"}


# --------------------------------------------------------------------------- #
# job handlers
# --------------------------------------------------------------------------- #
async def handle_ingest(ctx: AppContext, job: dict) -> dict:
    """Fetch a catalog and run the ingest pipeline. `payload.mode`:
    "full" (default) -> adapter.fetch_catalog(); "delta" -> adapter.get_delta(cursor)."""
    payload = job.get("payload") or {}
    tenant_id = str(payload["tenant_id"])
    import_id = payload.get("import_id")
    mode = str(payload.get("mode") or "full").strip().lower()
    cfg = await ctx.config_for(tenant_id)
    adapter_type = payload.get("adapter") or cfg.get("adapters", {}).get("catalog", "csv")
    adapter = build_adapter(
        adapter_type,
        tenant_id=tenant_id,
        public_base_url=ctx.settings.public_base_url,
        count=int(payload.get("count") or 300),
        csv_path=payload.get("path"),
        image_dir=mock_image_dir(ctx.settings),
    )

    try:
        if mode == "delta":
            since = await _delta_since(ctx, tenant_id, payload)
            rows = await asyncio.to_thread(lambda: list(adapter.get_delta(since)))
        else:
            rows = await asyncio.to_thread(lambda: list(adapter.fetch_catalog()))
        validation_errors = list(getattr(adapter, "validation_errors", []))
        row_offset = 2  # adapters number malformed rows with a 1-based +header convention
        if import_id:
            await ctx.db.execute(
                """UPDATE catalog_imports SET status='running', started_at=COALESCE(started_at, now()),
                     total_rows=%s, validation_report=%s WHERE id=%s""",
                (len(rows) + len(validation_errors), Jsonb(validation_errors), import_id),
            )

        result = await _process_rows(ctx, tenant_id, cfg, adapter, rows, import_id,
                                     validation_errors, row_offset)

        if _may_flag_missing(payload, adapter, mode, rows):
            await _flag_missing_discontinued(ctx, tenant_id, {r.sku for r in rows})

        if ctx.vision is not None and result["product_ids"]:
            result["enriched_skus"] = await _enrich_missing_tags(
                ctx, tenant_id, cfg, result["product_ids"], import_id)

        report = await hygiene_service.compute_hygiene(ctx, tenant_id, cfg)
        await hygiene_service.persist_findings(ctx, tenant_id, import_id, report)

        if result["reembedded"] or result["imported"]:
            est = result["reembedded"] * ctx.embed.est_cost_per_call
            await ctx.db.execute(
                """INSERT INTO provider_calls (tenant_id, kind, provider, model, units, est_cost_usd, import_id, created_at)
                   VALUES (%s, 'embedding', %s, %s, %s, %s, %s, now())""",
                (tenant_id, ctx.embed.model.split("-")[0], ctx.embed.model, result["reembedded"], est, import_id),
            )

        stats = {
            "reembedded": result["reembedded"],
            "skipped_unchanged": result["skipped_unchanged"],
            "coverage_pct": report["coverage_pct"],
            "enriched_skus": result.get("enriched_skus", 0),
        }
        if import_id:
            await ctx.db.execute(
                """UPDATE catalog_imports SET status='done', imported=%s, skipped=%s, failed=%s,
                     validation_report=%s, stats=%s, finished_at=now() WHERE id=%s""",
                (result["imported"], result["skipped"], result["failed"],
                 Jsonb(result["errors"]), Jsonb(stats), import_id),
            )
        log.info("ingest done tenant=%s adapter=%s mode=%s imported=%s skipped=%s failed=%s reembedded=%s",
                 tenant_id, adapter_type, mode, result["imported"], result["skipped"], result["failed"],
                 result["reembedded"])
        return stats
    except Exception as exc:
        log.exception("ingest failed tenant=%s import_id=%s", tenant_id, import_id)
        if import_id:
            await ctx.db.execute(
                "UPDATE catalog_imports SET status='failed', finished_at=now() WHERE id=%s",
                (import_id,),
            )
        raise


async def handle_delta_scan(ctx: AppContext, job: dict) -> dict:
    """US-7 incremental scan: delta fetch from the tenant watermark by default.

    Only full-snapshot adapters may discontinue absent SKUs; a partial delta
    (Shopify: delta_is_full=False) must never mass-discontinue, so _may_flag_missing
    downgrades mark_missing_discontinued for them.
    """
    payload = dict(job.get("payload") or {})
    payload.setdefault("mode", "delta")
    payload.setdefault("mark_missing_discontinued", True)
    return await handle_ingest(ctx, {**job, "payload": payload})


async def handle_reindex_sku(ctx: AppContext, job: dict) -> dict:
    """Manual single-SKU force re-fetch/re-embed (E15, admin route)."""
    payload = job.get("payload") or {}
    tenant_id = str(payload["tenant_id"])
    sku = payload["sku"]
    cfg = await ctx.config_for(tenant_id)
    product = await ctx.db.fetch_one(
        "SELECT id::text AS id FROM products WHERE tenant_id=%s AND sku=%s", (tenant_id, sku)
    )
    if not product:
        return {"status": "not_found", "sku": sku}
    image = await ctx.db.fetch_one(
        """SELECT source_url FROM product_images WHERE tenant_id=%s AND product_id=%s
           ORDER BY is_primary DESC, created_at LIMIT 1""",
        (tenant_id, product["id"]),
    )
    if not image:
        await _recompute_hygiene(ctx, tenant_id, cfg, None)
        return {"status": "no_image", "sku": sku}
    source_url = image["source_url"]
    try:
        data = await _fetch_bytes(ctx, source_url)
    except Exception as exc:
        entry = {"ref": source_url, "status": "fetch_failed", "error": str(exc)}
    else:
        entry = await _prepare_entry(ctx, data, source_url)
    counters = {"reembedded": 0, "skipped_unchanged": 0, "failed": 0}
    await _apply_image(ctx, tenant_id, product["id"], entry, counters)
    await ctx.db.execute("UPDATE products SET updated_at=now() WHERE id=%s", (product["id"],))
    await _recompute_hygiene(ctx, tenant_id, cfg, None)
    return {"status": "reindexed" if counters["reembedded"] else "unchanged", "sku": sku,
            "reembedded": counters["reembedded"]}


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #
async def _process_rows(ctx: AppContext, tenant_id: str, cfg: dict, adapter, rows: list,
                        import_id: str | None, validation_errors: list[dict],
                        row_offset: int) -> dict:
    counters = {"imported": 0, "skipped": 0, "failed": 0,
                "reembedded": 0, "skipped_unchanged": 0}
    errors = list(validation_errors)
    product_ids: list[str] = []
    sem = asyncio.Semaphore(FETCH_CONCURRENCY)

    for start in range(0, len(rows), BATCH):
        batch = rows[start:start + BATCH]
        prepared = await asyncio.gather(
            *[_prepare_row(ctx, adapter, row, sem) for row in batch]
        )
        for i, (row, entries) in enumerate(zip(batch, prepared)):
            row_no = start + i + row_offset
            try:
                bucket = await _upsert_row(ctx, tenant_id, row, entries, counters)
                counters[bucket] += 1
                product_ids.append(entries["product_id"])
            except Exception as exc:  # per-row failure lands in the report, never dropped
                counters["failed"] += 1
                errors.append({"row": row_no, "sku": row.sku,
                               "reason": f"{type(exc).__name__}: {exc}"})
                log.exception("ingest row failed sku=%s", row.sku)
        if import_id:
            await ctx.db.execute(
                """UPDATE catalog_imports SET imported=%s, skipped=%s, failed=%s,
                     validation_report=%s WHERE id=%s""",
                (counters["imported"], counters["skipped"], counters["failed"],
                 Jsonb(errors), import_id),
            )
    return {**counters, "errors": errors, "product_ids": product_ids}


async def _prepare_row(ctx: AppContext, adapter, row, sem: asyncio.Semaphore) -> dict:
    entries = []
    for ref in row.images:
        async with sem:
            try:
                data = await asyncio.to_thread(adapter.fetch_image, ref.ref or ref.source_url)
            except Exception as exc:
                entries.append({"ref": ref.source_url, "status": "fetch_failed", "error": str(exc)})
                continue
        entries.append(await _prepare_entry(ctx, data, ref.source_url))
    return {"product_id": None, "images": entries}


async def _prepare_entry(ctx: AppContext, data: bytes, source_url: str) -> dict:
    entry = {"ref": source_url}
    try:
        img = load_image(data)
    except DecodeFailed as exc:
        entry.update(status="decode_failed", error=str(exc))
        return entry
    status, detail = quality_gate(img)
    norm = normalize_jpeg(img)
    entry.update(status=status, detail=detail, jpeg=norm, hash=sha256_hex(norm),
                 width=img.width, height=img.height, bytes=len(norm))
    if status in ("ok", "too_dark"):
        entry["thumb"] = make_thumb(img)
        entry["embedding"] = await ctx.embed.embed_image(norm)  # provider errors abort the job (retryable)
    return entry


async def _upsert_row(ctx: AppContext, tenant_id: str, row, entries: dict, counters: dict) -> str:
    db = ctx.db
    existing = await db.fetch_one(
        "SELECT id::text AS id, source_updated_at FROM products WHERE tenant_id=%s AND sku=%s",
        (tenant_id, row.sku),
    )
    upsert = await db.execute_returning(
        """INSERT INTO products (tenant_id, sku, title, description, price, currency, stock_status,
                                 category, attributes, is_active, source_updated_at,
                                 first_seen_at, last_seen_at, created_at, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s, now(), now(), now(), now())
           ON CONFLICT (tenant_id, sku) DO UPDATE SET
             title=EXCLUDED.title, description=EXCLUDED.description, price=EXCLUDED.price,
             currency=EXCLUDED.currency, stock_status=EXCLUDED.stock_status, category=EXCLUDED.category,
             attributes=EXCLUDED.attributes, is_active=true,
             source_updated_at=EXCLUDED.source_updated_at, last_seen_at=now(), updated_at=now()
           RETURNING id::text AS id, (xmax = 0) AS was_insert""",
        (tenant_id, row.sku, row.title, row.description, row.price, row.currency,
         row.stock_status if row.stock_status != "discontinued" else "out_of_stock",
         row.category, Jsonb(row.attributes or {}), row.source_updated_at),
    )
    product_id = upsert["id"]
    entries["product_id"] = product_id

    # feed tags: refresh (enrichment tags with source='llm' are preserved)
    await db.execute(
        "DELETE FROM product_tags WHERE tenant_id=%s AND product_id=%s AND source='feed'",
        (tenant_id, product_id),
    )
    for key, value in (row.attributes or {}).items():
        if key in TAG_TYPES and value not in (None, ""):
            await db.execute(
                """INSERT INTO product_tags (tenant_id, product_id, tag_type, tag_value, source, confidence, created_at)
                   VALUES (%s, %s, %s, %s, 'feed', 1.0, now()) ON CONFLICT DO NOTHING""",
                (tenant_id, product_id, key, str(value)),
            )

    any_created = False
    any_failed = False
    for entry in entries["images"]:
        created = await _apply_image(ctx, tenant_id, product_id, entry, counters)
        any_created = any_created or created
        any_failed = any_failed or entry.get("status") in ("fetch_failed", "decode_failed", "too_small")

    if entries["images"] and any_failed:
        return "failed"
    if (existing and existing["source_updated_at"] == row.source_updated_at
            and not any_created):
        return "skipped"
    return "imported"


async def _apply_image(ctx: AppContext, tenant_id: str, product_id: str, entry: dict,
                       counters: dict) -> bool:
    """Upsert one product_images row (+ embedding). Returns True when a new embedding was created."""
    db = ctx.db
    status = entry.get("status", "fetch_failed")
    if entry.get("embedding") is not None and status in ("ok", "too_dark"):
        image_hash = entry["hash"]
        emb = await db.fetch_one(
            "SELECT id::text AS id FROM image_embeddings WHERE tenant_id=%s AND image_hash=%s AND model=%s",
            (tenant_id, image_hash, ctx.embed.model),
        )
        created = False
        if emb:
            embedding_id = emb["id"]
        else:
            inserted = await db.execute_returning(
                """INSERT INTO image_embeddings (tenant_id, image_hash, model, dim, embedding, created_at)
                   VALUES (%s, %s, %s, %s, %s::vector, now())
                   ON CONFLICT (tenant_id, image_hash, model) DO NOTHING RETURNING id::text AS id""",
                (tenant_id, image_hash, ctx.embed.model, ctx.embed.dim, _vector_literal(entry["embedding"])),
            )
            if inserted:
                embedding_id, created = inserted["id"], True
            else:  # raced with another writer
                emb2 = await db.fetch_one(
                    "SELECT id::text AS id FROM image_embeddings WHERE tenant_id=%s AND image_hash=%s AND model=%s",
                    (tenant_id, image_hash, ctx.embed.model),
                )
                embedding_id = emb2["id"]
        if created:
            counters["reembedded"] += 1
        else:
            counters["skipped_unchanged"] += 1

        thumb_key = None
        if entry.get("thumb"):
            thumb_key = f"thumb/{tenant_id}/{image_hash}_256.jpg"
            if not await asyncio.to_thread(ctx.storage.exists, thumb_key):
                await storage_put(ctx, thumb_key, entry["thumb"])

        # E15: a changed image hash replaces the old row -> no ghost matches
        await db.execute(
            "DELETE FROM product_images WHERE tenant_id=%s AND product_id=%s AND image_hash IS DISTINCT FROM %s",
            (tenant_id, product_id, image_hash),
        )
        await db.execute(
            """INSERT INTO product_images (tenant_id, product_id, embedding_id, source_url, image_hash,
                                           thumb_key, width, height, bytes, quality_status, is_primary, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, now())
               ON CONFLICT (tenant_id, product_id, image_hash) DO UPDATE SET
                 embedding_id=EXCLUDED.embedding_id, source_url=EXCLUDED.source_url,
                 thumb_key=EXCLUDED.thumb_key, width=EXCLUDED.width, height=EXCLUDED.height,
                 bytes=EXCLUDED.bytes, quality_status=EXCLUDED.quality_status""",
            (tenant_id, product_id, embedding_id, entry["ref"], image_hash, thumb_key,
             entry.get("width"), entry.get("height"), entry.get("bytes"), status),
        )
        return created

    # unusable image: keep a marker row (hygiene fetch_failed/quality_fail), no vector (E5)
    await db.execute("DELETE FROM product_images WHERE tenant_id=%s AND product_id=%s", (tenant_id, product_id))
    await db.execute(
        """INSERT INTO product_images (tenant_id, product_id, embedding_id, source_url, image_hash,
                                       quality_status, is_primary, created_at)
           VALUES (%s, %s, NULL, %s, NULL, %s, true, now())""",
        (tenant_id, product_id, entry.get("ref"), status),
    )
    return False


async def _flag_missing_discontinued(ctx: AppContext, tenant_id: str, skus: set[str]) -> None:
    if not skus:
        return
    await ctx.db.execute(
        """UPDATE products SET stock_status='discontinued', is_active=false, updated_at=now()
           WHERE tenant_id=%s AND is_active AND stock_status <> 'discontinued'
             AND NOT (sku = ANY(%s))""",
        (tenant_id, list(skus)),
    )


def _may_flag_missing(payload: dict, adapter, mode: str, rows: list) -> bool:
    """Absence in a partial view proves nothing: only full snapshots may discontinue."""
    if not payload.get("mark_missing_discontinued") or not rows:
        return False
    if mode == "delta" and not getattr(adapter, "delta_is_full", False):
        return False
    return True


async def _delta_since(ctx: AppContext, tenant_id: str, payload: dict) -> datetime | None:
    """Tenant-scoped watermark for delta fetches (None -> adapter full fetch).

    Watermark = claim time of the last successful catalog job (ingest/delta_scan).
    Claim time, not finish time: any store update that lands while a run is in
    flight is then still covered by the next run (duplicates are deduped
    downstream). The watermark advances implicitly: a job only lands in this
    query once it reaches status='done' after the ingest committed - scheduler
    runs included, no catalog_imports row required. Adapters own their small
    overlap buffer (Shopify: delta_overlap_s).
    """
    override = payload.get("since")
    if override is not None:
        parsed = _coerce_since(override)
        if parsed is not None:
            return parsed
    row = await ctx.db.fetch_one(
        """SELECT COALESCE(locked_at, updated_at) AS watermark FROM jobs
           WHERE tenant_id=%s AND type IN ('ingest','delta_scan') AND status='done'
           ORDER BY COALESCE(locked_at, updated_at) DESC LIMIT 1""",
        (tenant_id,),
    )
    return row["watermark"] if row and row["watermark"] else None


def _coerce_since(raw: object) -> datetime | None:
    if isinstance(raw, datetime):
        parsed = raw
    else:
        try:
            parsed = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def _enrich_missing_tags(ctx: AppContext, tenant_id: str, cfg: dict,
                               product_ids: list[str], import_id: str | None) -> int:
    rows = await ctx.db.fetch_all(
        """SELECT p.id::text AS id, p.sku, p.title, p.description, p.category, p.attributes,
                  (SELECT pi.thumb_key FROM product_images pi
                   WHERE pi.product_id = p.id AND pi.thumb_key IS NOT NULL LIMIT 1) AS thumb_key,
                  ARRAY(SELECT DISTINCT tag_type FROM product_tags pt WHERE pt.product_id = p.id) AS tag_types
           FROM products p WHERE p.tenant_id = %s AND p.id = ANY(%s::uuid[])""",
        (tenant_id, product_ids),
    )
    enriched = 0
    for row in rows:
        missing = [t for t in CRITICAL_TAG_TYPES if t not in (row["tag_types"] or [])]
        if not missing:
            continue
        thumb = None
        if row["thumb_key"]:
            try:
                thumb = await asyncio.to_thread(ctx.storage.get, row["thumb_key"])
            except Exception:
                thumb = None
        try:
            tags = await ctx.vision.enrich(
                {"sku": row["sku"], "title": row["title"], "description": row["description"],
                 "category": row["category"], "attributes": row["attributes"] or {}},
                thumb,
            )
        except Exception:
            log.warning("enrichment failed for sku=%s", row["sku"], exc_info=True)
            continue
        for tag_type, value in (tags or {}).items():
            if tag_type in TAG_TYPES and value:
                await ctx.db.execute(
                    """INSERT INTO product_tags (tenant_id, product_id, tag_type, tag_value, source, confidence, created_at)
                       VALUES (%s, %s, %s, %s, 'llm', 0.7, now()) ON CONFLICT DO NOTHING""",
                    (tenant_id, row["id"], tag_type, str(value)),
                )
        enriched += 1
    if rows:
        await ctx.db.execute(
            """INSERT INTO provider_calls (tenant_id, kind, provider, model, units, est_cost_usd, import_id, created_at)
               VALUES (%s, 'enrichment', %s, %s, %s, %s, %s, now())""",
            (tenant_id, getattr(ctx.vision, "name", "vision"), getattr(ctx.vision, "model", ""),
             enriched, enriched * ctx.vision.est_cost_per_call, import_id),
        )
    return enriched


async def _recompute_hygiene(ctx: AppContext, tenant_id: str, cfg: dict, import_id: str | None) -> None:
    report = await hygiene_service.compute_hygiene(ctx, tenant_id, cfg)
    await hygiene_service.persist_findings(ctx, tenant_id, import_id, report)


async def _fetch_bytes(ctx: AppContext, source_url: str) -> bytes:
    if "/mock-images/" in source_url:
        name = source_url.rsplit("/", 1)[-1]
        return (mock_image_dir(ctx.settings) / name).read_bytes()
    if source_url.startswith(("filesystem:", "file://")):
        from pathlib import Path

        return Path(source_url.split(":", 1)[1]).read_bytes()
    resp = httpx.get(source_url, timeout=10.0, follow_redirects=True)
    resp.raise_for_status()
    return resp.content


def _vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(f"{v:.7f}" for v in embedding) + "]"
