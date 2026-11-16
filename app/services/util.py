"""Small shared service helpers."""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from ..context import AppContext
from ..storage import StorageProvider


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def product_url(cfg: dict, sku: str) -> str:
    storefront = cfg.get("storefront", {})
    template = storefront.get("product_url_template", "{base}/products/{sku}")
    base = str(storefront.get("base_url", "")).rstrip("/")
    return template.replace("{base}", base).replace("{sku}", sku)


async def storage_put(ctx: AppContext, key: str, data: bytes, content_type: str = "image/jpeg") -> str:
    return await asyncio.to_thread(ctx.storage.put, key, data, content_type)


async def storage_get(ctx: AppContext, key: str) -> bytes:
    return await asyncio.to_thread(ctx.storage.get, key)


async def storage_delete(ctx: AppContext, key: str) -> bool:
    return await asyncio.to_thread(ctx.storage.delete, key)


async def fetch_product_tags(ctx: AppContext, tenant_id: str, product_ids: list[str]) -> dict[str, dict[str, str]]:
    """product_id -> {tag_type: value}; feed values win over llm values."""
    if not product_ids:
        return {}
    rows = await ctx.db.fetch_all(
        """SELECT product_id::text AS pid, tag_type, tag_value, source
           FROM product_tags WHERE tenant_id = %s AND product_id = ANY(%s::uuid[])
           ORDER BY (source = 'feed') DESC""",
        (tenant_id, product_ids),
    )
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        out.setdefault(row["pid"], {})
        out[row["pid"]].setdefault(row["tag_type"], row["tag_value"])
    return out


def card_from_row(row: dict[str, Any], cfg: dict) -> dict[str, Any]:
    return {
        "sku": row["sku"],
        "title": row.get("title"),
        "price": float(row["price"]) if row.get("price") is not None else None,
        "currency": row.get("currency") or "USD",
        "stock_status": row.get("stock_status"),
        "image_url": row.get("image_url"),
        "product_url": product_url(cfg, row["sku"]),
        "score": None,
        "visual_sim": None,
        "attribute_match": None,
        "boost": None,
        "similarity_label": "same_category",
        "reason": None,
        "position": None,
    }
