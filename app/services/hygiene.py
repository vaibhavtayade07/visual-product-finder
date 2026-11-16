"""Hygiene report: findings + coverage (US-8, AC8.1-8.4).

Computed live on every call (coverage is assertable against actual embed counts,
AC8.4) and persisted to `hygiene_findings` on every indexing run (AC8.3).
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..context import AppContext
from ..lexicon import CRITICAL_TAG_TYPES
from .util import product_url

FINDING_TYPES = ["no_image", "quality_fail", "missing_attributes", "discontinued", "fetch_failed"]


async def compute_hygiene(ctx: AppContext, tenant_id: str, cfg: dict) -> dict:
    db = ctx.db

    counts = await db.fetch_one(
        """SELECT
             COUNT(*) FILTER (WHERE is_active AND stock_status <> 'discontinued') AS active,
             COUNT(*) FILTER (WHERE is_active AND stock_status <> 'discontinued'
                              AND EXISTS (SELECT 1 FROM product_images pi
                                          WHERE pi.product_id = p.id AND pi.embedding_id IS NOT NULL)) AS indexed
           FROM products p WHERE tenant_id = %s""",
        (tenant_id,),
    )
    active = int(counts["active"] or 0)
    indexed = int(counts["indexed"] or 0)
    coverage = round(100.0 * indexed / active, 1) if active else 100.0

    findings: dict[str, list[dict]] = {t: [] for t in FINDING_TYPES}

    no_image = await db.fetch_all(
        """SELECT p.sku FROM products p
           WHERE p.tenant_id = %s AND p.is_active AND p.stock_status <> 'discontinued'
             AND NOT EXISTS (SELECT 1 FROM product_images pi WHERE pi.product_id = p.id)
           ORDER BY p.sku""",
        (tenant_id,),
    )
    for row in no_image:
        findings["no_image"].append({"sku": row["sku"], "product_url": product_url(cfg, row["sku"]),
                                     "detail": {"reason": "no images in catalog"}})

    quality = await db.fetch_all(
        """SELECT p.sku, pi.quality_status
           FROM product_images pi JOIN products p ON p.id = pi.product_id
           WHERE pi.tenant_id = %s AND pi.quality_status IN ('too_small', 'too_dark', 'decode_failed')
           ORDER BY p.sku""",
        (tenant_id,),
    )
    for row in quality:
        findings["quality_fail"].append({"sku": row["sku"], "product_url": product_url(cfg, row["sku"]),
                                         "detail": {"quality_status": row["quality_status"]}})

    fetch_failed = await db.fetch_all(
        """SELECT p.sku, pi.source_url
           FROM product_images pi JOIN products p ON p.id = pi.product_id
           WHERE pi.tenant_id = %s AND pi.quality_status = 'fetch_failed'
           ORDER BY p.sku""",
        (tenant_id,),
    )
    for row in fetch_failed:
        findings["fetch_failed"].append({"sku": row["sku"], "product_url": product_url(cfg, row["sku"]),
                                         "detail": {"source_url": row["source_url"]}})

    missing = await db.fetch_all(
        """SELECT p.sku,
                  ARRAY(SELECT t FROM unnest(%s::text[]) t
                        WHERE NOT EXISTS (SELECT 1 FROM product_tags pt
                                          WHERE pt.product_id = p.id AND pt.tag_type = t)) AS missing
           FROM products p
           WHERE p.tenant_id = %s AND p.is_active AND p.stock_status <> 'discontinued'
           ORDER BY p.sku""",
        (CRITICAL_TAG_TYPES, tenant_id),
    )
    for row in missing:
        if row["missing"]:
            findings["missing_attributes"].append({
                "sku": row["sku"], "product_url": product_url(cfg, row["sku"]),
                "detail": {"missing": list(row["missing"])},
            })

    discontinued = await db.fetch_all(
        """SELECT sku FROM products
           WHERE tenant_id = %s AND (stock_status = 'discontinued' OR NOT is_active)
           ORDER BY sku""",
        (tenant_id,),
    )
    for row in discontinued:
        findings["discontinued"].append({"sku": row["sku"], "product_url": product_url(cfg, row["sku"]),
                                         "detail": {"reason": "absent from latest full feed"}})

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "coverage_pct": coverage,
        "indexed": indexed,
        "active": active,
        "findings": findings,
    }


async def persist_findings(ctx: AppContext, tenant_id: str, import_id: str | None, report: dict) -> None:
    """Regenerated per import run (AC8.3): replace this tenant's finding rows."""
    async with ctx.db.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM hygiene_findings WHERE tenant_id = %s", (tenant_id,))
            for finding_type, items in report["findings"].items():
                for item in items:
                    await cur.execute(
                        """INSERT INTO hygiene_findings (tenant_id, import_id, sku, finding_type, detail, created_at)
                           VALUES (%s, %s, %s, %s, %s, now())""",
                        (tenant_id, import_id, item["sku"], finding_type, _jsonb(item.get("detail") or {})),
                    )
        await conn.commit()


def hygiene_to_csv(report: dict) -> str:
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["finding_type", "sku", "product_url", "detail"])
    for finding_type, items in report["findings"].items():
        for item in items:
            writer.writerow([finding_type, item["sku"], item.get("product_url", ""),
                             _detail_str(item.get("detail"))])
    writer.writerow([])
    writer.writerow(["coverage_pct", report["coverage_pct"], "indexed", report["indexed"], "active", report["active"]])
    return buf.getvalue()


def _detail_str(detail: dict | None) -> str:
    if not detail:
        return ""
    return "; ".join(f"{k}={v}" for k, v in detail.items())


def _jsonb(value: dict):
    from psycopg.types.json import Jsonb

    return Jsonb(value)
