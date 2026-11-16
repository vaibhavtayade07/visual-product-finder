"""Query pipeline (architecture §5b/§5c): embed -> pgvector top-K -> rerank -> score ->
band -> persist payload. Degrade path per E10 (LLM timeout -> embedding-only, in-place
upgrade while the 40 s wall allows). Refine re-filters cached candidates (AC4.4)."""
from __future__ import annotations

import asyncio
import logging
import time

from psycopg.types.json import Jsonb

from ..context import AppContext
from ..errors import NotFound, NotReady
from ..providers.base import ContentBlockedError, ProviderError, RerankCandidate
from ..scoring import clamp_reason, finalize, score_candidate
from .popular import get_popular
from .util import fetch_product_tags, product_url, storage_get

log = logging.getLogger(__name__)

WALL_CLOCK_S = 40.0  # hard wall (§5b): never an error page
FINAL_STATUSES = ("done", "failed")

TOP_K_SQL = """
SELECT e.id::text AS embedding_id, e.image_hash, (1 - (e.embedding <=> %(vec)s::vector)) AS visual_sim,
       p.id::text AS product_id, p.sku, p.title, p.price, p.currency, p.stock_status,
       p.category, p.attributes,
       pi.source_url, pi.thumb_key
FROM image_embeddings e
JOIN product_images pi ON pi.embedding_id = e.id AND pi.tenant_id = e.tenant_id
JOIN products p ON p.id = pi.product_id AND p.tenant_id = e.tenant_id
WHERE e.tenant_id = %(tenant)s AND e.model = %(model)s
  AND p.is_active AND p.stock_status <> 'discontinued'
  AND pi.quality_status IN ('ok', 'too_dark')
ORDER BY e.embedding <=> %(vec)s::vector
LIMIT %(k)s
"""


def _vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(f"{v:.7f}" for v in embedding) + "]"


async def handle_query(ctx: AppContext, query_id: str) -> None:
    started = time.monotonic()
    row = await ctx.db.fetch_one("SELECT * FROM search_queries WHERE id = %s", (query_id,))
    if not row or row["status"] in FINAL_STATUSES:
        return
    tenant_id = str(row["tenant_id"])
    cfg = await ctx.config_for(tenant_id)
    timings = {"embedding_ms": 0, "retrieval_ms": 0, "rerank_ms": 0}

    async def fail(code: str, message: str) -> None:
        await ctx.db.execute(
            """UPDATE search_queries SET status='failed', error_code=%s, latency_ms=%s,
                 result_payload=%s WHERE id=%s""",
            (code, int((time.monotonic() - started) * 1000),
             Jsonb({"query_id": query_id, "status": "failed",
                    "error": {"code": code, "message": message, "details": {}}}),
             query_id),
        )

    # 1) embed (retries per §6.1: 8 s timeout x2, 1 s backoff)
    await ctx.db.execute("UPDATE search_queries SET status='embedding' WHERE id=%s", (query_id,))
    t0 = time.monotonic()
    image_bytes = await storage_get(ctx, row["image_key"])
    embedding: list[float] | None = None
    embed_timeout = float(cfg.get("embed", {}).get("timeout_s", 8))
    embed_retries = int(cfg.get("embed", {}).get("retries", 2))
    for attempt in range(embed_retries):
        try:
            embedding = await asyncio.wait_for(ctx.embed.embed_image(image_bytes), timeout=embed_timeout)
            break
        except ContentBlockedError:
            await fail("PHOTO_UNPROCESSABLE", "We couldn't process this photo. Try another one.")
            return
        except (ProviderError, asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
            log.warning("query embed attempt %s failed: %s", attempt + 1, exc)
            if attempt < embed_retries - 1:
                await asyncio.sleep(1.0)
    timings["embedding_ms"] = int((time.monotonic() - t0) * 1000)
    if embedding is None:
        await fail("PROVIDER_UNAVAILABLE", "Matching is temporarily unavailable. Please retry.")
        return
    await ctx.db.execute(
        """INSERT INTO provider_calls (tenant_id, kind, provider, model, units, est_cost_usd, search_query_id, created_at)
           VALUES (%s, 'embedding', %s, %s, 1, %s, %s, now())""",
        (tenant_id, ctx.embed.model.split("-")[0], ctx.embed.model, ctx.embed.est_cost_per_call, query_id),
    )

    # 2) retrieve top-K (tenant-scoped, HNSW, quality-gated)
    await ctx.db.execute("UPDATE search_queries SET status='retrieving' WHERE id=%s", (query_id,))
    t0 = time.monotonic()
    top_k = int(cfg.get("top_k_retrieve", 50))
    rows = await ctx.db.fetch_all(TOP_K_SQL, {
        "vec": _vector_literal(embedding), "tenant": tenant_id, "model": ctx.embed.model, "k": top_k,
    })
    timings["retrieval_ms"] = int((time.monotonic() - t0) * 1000)

    candidates = await _build_candidates(ctx, tenant_id, rows)

    if not candidates:
        await _persist_low(ctx, query_id, tenant_id, cfg, timings, started, query_attributes=None,
                           candidates=[], degraded=False)
        return

    # 3) rerank (vision | degrade | text_only)
    await ctx.db.execute("UPDATE search_queries SET status='reranking' WHERE id=%s", (query_id,))
    t0 = time.monotonic()
    query_attrs: dict | None = None
    degraded = ctx.vision is None
    if ctx.vision is not None:
        llm_timeout = float(cfg.get("degrade", {}).get("llm_timeout_s", 20))
        rerank_k = int(cfg.get("rerank_k", 10))
        llm_candidates = [c for c in candidates[:rerank_k]]
        try:
            query_attrs = await _run_llm(ctx, tenant_id, query_id, image_bytes, llm_candidates, llm_timeout,
                                         cfg, timings)
        except (asyncio.TimeoutError, ProviderError, ContentBlockedError) as exc:
            degraded = True
            log.warning("rerank degraded for query=%s: %s: %s", query_id, type(exc).__name__, exc)
    timings["rerank_ms"] = int((time.monotonic() - t0) * 1000)

    cards, band, top_score = _assemble(ctx, cfg, candidates, query_attrs, degraded)
    filters = await _filter_options(ctx, tenant_id, candidates)

    if band == "low":
        # §5c: no-match fallback (also covers the category-confidence gate, E6)
        await _persist_low(ctx, query_id, tenant_id, cfg, timings, started, query_attributes=query_attrs,
                           candidates=candidates, degraded=degraded, top_score=top_score)
        return

    fallback = None
    payload = _payload(query_id, "done", band, degraded, timings, started, query_attrs,
                       cards, fallback, filters)
    await _persist(ctx, query_id, "done", band, top_score, query_attrs, candidates,
                   payload, degraded, timings, started)

    # E10: if we degraded, one upgrade attempt while the 40 s wall allows
    if degraded and ctx.vision is not None:
        elapsed = time.monotonic() - started
        remaining = WALL_CLOCK_S - elapsed - 1.0
        if remaining > 5.0:
            try:
                t1 = time.monotonic()
                query_attrs2 = await _run_llm(ctx, tenant_id, query_id, image_bytes,
                                              candidates[: int(cfg.get("rerank_k", 10))],
                                              min(remaining, float(cfg.get("degrade", {}).get("llm_timeout_s", 20))),
                                              cfg, timings)
                timings["rerank_ms"] += int((time.monotonic() - t1) * 1000)
                cards2, band2, top_score2 = _assemble(ctx, cfg, candidates, query_attrs2, False)
                if band2 != "low":
                    payload2 = _payload(query_id, "done", band2, False, timings, started, query_attrs2,
                                        cards2, None, filters)
                    await _persist(ctx, query_id, "done", band2, top_score2, query_attrs2, candidates,
                                   payload2, False, timings, started)
                    log.info("query=%s upgraded in place after degrade", query_id)
            except Exception as exc:  # noqa: BLE001 - degraded payload stays final
                log.info("query=%s upgrade attempt failed (staying degraded): %s", query_id, exc)


async def _run_llm(ctx: AppContext, tenant_id: str, query_id: str, image_bytes: bytes,
                   candidates: list[dict], timeout_s: float, cfg: dict, timings: dict) -> dict:
    rerank_inputs = await _load_thumbs(ctx, candidates)
    result = await asyncio.wait_for(
        ctx.vision.rerank(image_bytes, rerank_inputs), timeout=timeout_s)
    await ctx.db.execute(
        """INSERT INTO provider_calls (tenant_id, kind, provider, model, units, est_cost_usd, search_query_id, created_at)
           VALUES (%s, 'llm_rerank', %s, %s, 1, %s, %s, now())""",
        (tenant_id, getattr(ctx.vision, "name", "vision"), getattr(ctx.vision, "model", ""),
         result.est_cost_usd, query_id),
    )
    for cand in candidates:
        match = result.matches.get(cand["sku"]) or {}
        cand["attribute_match"] = float(match.get("attribute_match", 0.0))
        cand["reason"] = clamp_reason(str(match.get("reason", "")), int(cfg.get("reason_max_chars", 120)))
    return dict(result.query_attributes or {})


async def _load_thumbs(ctx: AppContext, candidates: list[dict]) -> list[RerankCandidate]:
    sem = asyncio.Semaphore(8)

    async def load(key: str | None) -> bytes | None:
        if not key:
            return None
        async with sem:
            try:
                return await storage_get(ctx, key)
            except Exception:
                return None

    thumbs = await asyncio.gather(*[load(c.get("thumb_key")) for c in candidates])
    return [
        RerankCandidate(sku=c["sku"], title=c["title"], thumbs=t, attributes=c["attributes"],
                        price=c["price"], stock_status=c["stock_status"])
        for c, t in zip(candidates, thumbs)
    ]


async def _build_candidates(ctx: AppContext, tenant_id: str, rows: list[dict]) -> list[dict]:
    """Dedupe by product and by image hash (E11: one card per image, best SKU wins)."""
    seen_products: set[str] = set()
    seen_hashes: set[str] = set()
    picked: list[dict] = []
    for r in rows:
        if r["product_id"] in seen_products or r["image_hash"] in seen_hashes:
            continue
        seen_products.add(r["product_id"])
        seen_hashes.add(r["image_hash"])
        picked.append(r)
    tag_map = await fetch_product_tags(ctx, tenant_id, [r["product_id"] for r in picked])
    candidates = []
    for r in picked:
        merged = {**(r["attributes"] or {}), **(tag_map.get(r["product_id"]) or {})}
        candidates.append({
            "embedding_id": r["embedding_id"],
            "product_id": r["product_id"],
            "sku": r["sku"],
            "title": r["title"],
            "price": float(r["price"]) if r["price"] is not None else None,
            "currency": r["currency"] or "USD",
            "stock_status": r["stock_status"],
            "category": r["category"],
            "attributes": merged,
            "image_url": r["source_url"],
            "thumb_key": r["thumb_key"],
            "image_hash": r["image_hash"],
            "visual_sim": round(float(r["visual_sim"]), 6),
            "attribute_match": None,
            "reason": "",
        })
    return candidates


def _assemble(ctx: AppContext, cfg: dict, candidates: list[dict], query_attrs: dict | None,
              degraded: bool) -> tuple[list[dict], str, float]:
    for cand in candidates:
        scored = dict(cand)
        score_candidate(scored, cfg, degraded=degraded)
        cand["boost"] = scored["boost"]
        cand["score"] = scored["score"]
    top_score = max(c["score"] for c in candidates) if candidates else 0.0
    confidence = None
    if query_attrs and query_attrs.get("confidence") is not None:
        try:
            confidence = float(query_attrs["confidence"])
        except (TypeError, ValueError):
            confidence = None

    cards = [_card(cfg, c) for c in candidates]
    cards, band = finalize(cards, top_score, confidence, cfg, degraded=degraded)
    return cards, band, top_score


def _card(cfg: dict, cand: dict) -> dict:
    return {
        "sku": cand["sku"],
        "title": cand["title"],
        "price": cand["price"],
        "currency": cand["currency"],
        "stock_status": cand["stock_status"],
        "image_url": cand["image_url"],
        "product_url": product_url(cfg, cand["sku"]),
        "score": cand["score"],
        "visual_sim": cand["visual_sim"],
        "attribute_match": cand["attribute_match"],
        "boost": cand["boost"],
        "similarity_label": None,  # assigned by scoring.assign_labels via finalize
        "reason": cand.get("reason") or None,
        "position": None,
    }


async def _filter_options(ctx: AppContext, tenant_id: str, candidates: list[dict]) -> dict:
    options: dict[str, list[str]] = {"finish": [], "color": [], "size_class": []}
    for cand in candidates:
        for key in options:
            value = cand["attributes"].get(key)
            if value and value not in options[key]:
                options[key].append(value)
    prices = [c["price"] for c in candidates if c["price"] is not None]
    for key in options:
        options[key].sort()
    return {
        "finish": options["finish"],
        "color": options["color"],
        "size_class": options["size_class"],
        "price_band": {"min": min(prices), "max": max(prices)} if prices else None,
    }


async def _persist_low(ctx: AppContext, query_id: str, tenant_id: str, cfg: dict, timings: dict,
                       started: float, query_attributes: dict | None, candidates: list[dict],
                       degraded: bool, top_score: float | None = None) -> None:
    gate = float(cfg.get("category_confidence_gate", 0.50))
    category = None
    if query_attributes and query_attributes.get("category"):
        try:
            if float(query_attributes.get("confidence", 0)) >= gate:
                category = query_attributes["category"]
        except (TypeError, ValueError):
            category = None
    popular = await get_popular(ctx, tenant_id, cfg, category=category, limit=6)
    fallback = {"message": "We couldn't find a close match", "popular": popular}
    payload = _payload(query_id, "done", "low", degraded, timings, started, query_attributes,
                       [], fallback, None)
    await _persist(ctx, query_id, "done", "low", top_score, query_attributes, candidates,
                   payload, degraded, timings, started)


def _payload(query_id: str, status: str, band: str, degraded: bool, timings: dict, started: float,
             query_attrs: dict | None, results: list[dict], fallback: dict | None,
             filters: dict | None) -> dict:
    return {
        "query_id": query_id,
        "status": status,
        "band": band,
        "degraded": degraded,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "query_attributes": query_attrs,
        "results": results,
        "fallback": fallback,
        "filters": filters,
        "poll_after_ms": None,
    }


async def _persist(ctx: AppContext, query_id: str, status: str, band: str, top_score: float | None,
                   query_attrs: dict | None, candidates: list[dict], payload: dict,
                   degraded: bool, timings: dict, started: float) -> None:
    stored_candidates = [
        {k: c.get(k) for k in ("embedding_id", "product_id", "sku", "visual_sim", "image_hash",
                               "attribute_match", "reason", "image_url", "title", "price", "currency",
                               "stock_status")}
        for c in candidates
    ]
    await ctx.db.execute(
        """UPDATE search_queries SET status=%s, band=%s, top_score=%s, query_attributes=%s,
             candidates=%s, result_payload=%s, degraded=%s, latency_ms=%s, embedding_ms=%s,
             retrieval_ms=%s, rerank_ms=%s, error_code=NULL WHERE id=%s""",
        (status, band, top_score, Jsonb(query_attrs) if query_attrs is not None else None,
         Jsonb(stored_candidates), Jsonb(payload), degraded, payload["latency_ms"],
         timings["embedding_ms"], timings["retrieval_ms"], timings["rerank_ms"], query_id),
    )


# --------------------------------------------------------------------------- #
# refine (AC4.4: cached candidates, no re-embedding)
# --------------------------------------------------------------------------- #
async def refine(ctx: AppContext, query_id: str, filters: dict, top_n: int | None) -> dict:
    row = await ctx.db.fetch_one("SELECT * FROM search_queries WHERE id = %s", (query_id,))
    if not row:
        raise NotFound("unknown query_id")
    if not row["candidates"]:
        raise NotReady("results are not ready yet — candidates not cached")
    tenant_id = str(row["tenant_id"])
    cfg = await ctx.config_for(tenant_id)
    degraded = bool(row["degraded"])

    candidates = list(row["candidates"])
    product_ids = [c["product_id"] for c in candidates]
    products = await ctx.db.fetch_all(
        """SELECT id::text AS id, sku, title, price, currency, stock_status, is_active
           FROM products WHERE tenant_id=%s AND id = ANY(%s::uuid[])""",
        (tenant_id, product_ids),
    )
    by_id = {p["id"]: p for p in products}
    tag_map = await fetch_product_tags(ctx, tenant_id, product_ids)

    live: list[dict] = []
    for cand in candidates:
        product = by_id.get(cand["product_id"])
        if not product or not product["is_active"] or product["stock_status"] == "discontinued":
            continue
        merged_attrs = {**(tag_map.get(cand["product_id"]) or {})}
        item = {**cand,
                "title": product["title"], "price": float(product["price"]) if product["price"] is not None else None,
                "currency": product["currency"] or "USD", "stock_status": product["stock_status"],
                "attributes": merged_attrs,
                "attribute_match": cand.get("attribute_match") if not degraded else None}
        live.append(item)

    filtered = [c for c in live if _passes_filters(c, filters)]

    for cand in filtered:
        score_candidate(cand, cfg, degraded=degraded)
    top_score = max((c["score"] for c in filtered), default=0.0)
    confidence = None
    if row["query_attributes"] and row["query_attributes"].get("confidence") is not None:
        confidence = float(row["query_attributes"]["confidence"])
    n = int(top_n or cfg.get("top_n_cards", 10))
    cfg_n = {**cfg, "top_n_cards": n}
    cards = [_card(cfg, c) for c in filtered]
    cards, band = finalize(cards, top_score, confidence, cfg_n, degraded=degraded)

    if not cards:
        fallback = {"message": "No results with these filters", "popular": (
            await get_popular(ctx, tenant_id, cfg, limit=6))}
        return {"query_id": query_id, "status": row["status"], "band": "low", "degraded": degraded,
                "results": [], "fallback": fallback, "filters_applied": filters,
                "poll_after_ms": None}
    return {"query_id": query_id, "status": row["status"], "band": band, "degraded": degraded,
            "results": cards, "fallback": None, "filters_applied": filters, "poll_after_ms": None}


def _passes_filters(cand: dict, filters: dict) -> bool:
    attrs = cand.get("attributes") or {}
    for key in ("finish", "color", "size_class"):
        wanted = filters.get(key) or []
        if wanted and attrs.get(key) not in wanted:
            return False
    price = cand.get("price")
    if filters.get("price_min") is not None and (price is None or price < float(filters["price_min"])):
        return False
    if filters.get("price_max") is not None and (price is None or price > float(filters["price_max"])):
        return False
    if filters.get("in_stock_only") and cand.get("stock_status") not in ("in_stock", "low_stock"):
        return False
    return True


def public_payload(row: dict) -> dict:
    """GET /search/{id} body: stored payload for final states, envelope otherwise."""
    if row["status"] in FINAL_STATUSES and row["result_payload"]:
        payload = dict(row["result_payload"])
        payload.pop("poll_after_ms", None)
        payload["poll_after_ms"] = None
        return payload
    return {
        "query_id": str(row["id"]),
        "status": row["status"],
        "band": row["band"],
        "degraded": bool(row["degraded"]),
        "latency_ms": row["latency_ms"],
        "query_attributes": row["query_attributes"],
        "results": [],
        "fallback": None,
        "filters": None,
        "poll_after_ms": 1500,
    }
