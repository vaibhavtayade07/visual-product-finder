"""Popular items: clicks leaderboard (30d) with catalog-order fallback (architecture §4.1).

Anonymous, cached 5 min in-process.
"""
from __future__ import annotations

import time

from ..context import POPULAR_CACHE_TTL, AppContext
from .util import card_from_row


async def get_popular(ctx: AppContext, tenant_id: str, cfg: dict, category: str | None = None,
                      limit: int = 6) -> list[dict]:
    cache_key = f"{tenant_id}:{category or '-'}:{limit}"
    cached = ctx.popular_cache.get(cache_key)
    if cached and time.time() - cached[0] < POPULAR_CACHE_TTL:
        return cached[1]

    cards: list[dict] = []
    seen: set[str] = set()

    leaders = await ctx.db.fetch_all(
        """SELECT c.sku, COUNT(*) AS clicks
           FROM clicks c JOIN products p ON p.tenant_id = c.tenant_id AND p.sku = c.sku
           WHERE c.tenant_id = %s AND c.created_at > now() - interval '30 days'
             AND p.is_active AND p.stock_status <> 'discontinued'
           GROUP BY c.sku ORDER BY clicks DESC, c.sku LIMIT %s""",
        (tenant_id, int(limit)),
    )
    if leaders:
        rows = await ctx.db.fetch_all(
            """SELECT p.sku, p.title, p.price, p.currency, p.stock_status,
                      (SELECT pi.source_url FROM product_images pi
                       WHERE pi.product_id = p.id AND pi.quality_status IN ('ok','too_dark')
                       ORDER BY pi.is_primary DESC, pi.created_at LIMIT 1) AS image_url
               FROM products p WHERE p.tenant_id = %s AND p.sku = ANY(%s)
                 AND p.is_active AND p.stock_status <> 'discontinued'""",
            (tenant_id, [r["sku"] for r in leaders]),
        )
        by_sku = {r["sku"]: r for r in rows}
        for leader in leaders:
            row = by_sku.get(leader["sku"])
            if row and row.get("image_url"):
                cards.append(card_from_row(row, cfg))
                seen.add(row["sku"])

    if len(cards) < limit:
        fillers = await ctx.db.fetch_all(
            """SELECT p.sku, p.title, p.price, p.currency, p.stock_status,
                      (SELECT pi.source_url FROM product_images pi
                       WHERE pi.product_id = p.id AND pi.embedding_id IS NOT NULL
                       ORDER BY pi.is_primary DESC, pi.created_at LIMIT 1) AS image_url
               FROM products p
               WHERE p.tenant_id = %s AND p.is_active AND p.stock_status <> 'discontinued'
                 AND (%s::text IS NULL OR p.category = %s)
                 AND EXISTS (SELECT 1 FROM product_images pi2
                             WHERE pi2.product_id = p.id AND pi2.embedding_id IS NOT NULL)
               ORDER BY p.created_at, p.sku LIMIT %s""",
            (tenant_id, category, category, int(limit * 2)),
        )
        for row in fillers:
            if row["sku"] in seen:
                continue
            cards.append(card_from_row(row, cfg))
            seen.add(row["sku"])
            if len(cards) >= limit:
                break

    cards = cards[:limit]
    for i, card in enumerate(cards, start=1):
        card["position"] = i
    ctx.popular_cache[cache_key] = (time.time(), cards)
    return cards
