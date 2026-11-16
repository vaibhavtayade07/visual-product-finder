"""US-7 correctness: delta jobs must fetch increments (get_delta) and only full
snapshots may discontinue SKUs that are absent from a fetched view."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
from psycopg.types.json import Jsonb

from app.adapters.base import NormalizedProduct
from app.services import ingest
from app.workers import scheduler as scheduler_module
from app.workers.scheduler import Scheduler
from conftest import wait_job_done


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def enqueue(sql, job_type: str, tenant_id: str, payload: dict) -> str:
    row = sql.execute(
        """INSERT INTO jobs (type, tenant_id, payload, status, attempts, max_attempts,
                             run_after, created_at, updated_at)
           VALUES (%s, %s, %s, 'queued', 0, 3, now(), now(), now()) RETURNING id::text AS id""",
        (job_type, tenant_id, Jsonb({**payload, "tenant_id": tenant_id})),
    ).fetchone()
    return row["id"]


def tenant_id_of(sql, slug: str) -> str:
    return sql.execute("SELECT id::text AS id FROM tenants WHERE slug=%s", (slug,)).fetchone()["id"]


def seed_product(sql, tenant_id: str, sku: str) -> None:
    sql.execute(
        """INSERT INTO products (tenant_id, sku, title, price, currency, stock_status, category,
                                 attributes, is_active)
           VALUES (%s, %s, %s, 10, 'USD', 'in_stock', 'lamp', '{}', true)""",
        (tenant_id, sku, f"Item {sku}"),
    )


def product_state(sql, tenant_id: str, sku: str) -> dict:
    return sql.execute(
        "SELECT is_active, stock_status FROM products WHERE tenant_id=%s AND sku=%s",
        (tenant_id, sku),
    ).fetchone()


def make_row(tenant_id: str, sku: str) -> NormalizedProduct:
    return NormalizedProduct(
        tenant_id=tenant_id, sku=sku, title=f"Item {sku}", description=None,
        price=Decimal("19.99"), currency="USD", stock_status="in_stock", category="lamp",
        attributes={"type": "table_lamp"},
        source_updated_at=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
    )


class RecordingAdapter:
    """Offline stand-in that records which fetch path the handler chose."""

    adapter_type = "recording"

    def __init__(self, rows: list, *, delta_is_full: bool = False) -> None:
        self.rows = rows
        self.delta_is_full = delta_is_full
        self.delta_calls: list = []
        self.catalog_calls = 0
        self.validation_errors: list = []

    def fetch_catalog(self):
        self.catalog_calls += 1
        return list(self.rows)

    def get_delta(self, since):
        self.delta_calls.append(since)
        return list(self.rows)

    def fetch_image(self, image_ref):  # pragma: no cover - fixtures carry no images
        raise AssertionError("these fixtures must not fetch images")


# --------------------------------------------------------------------------- #
# fetch-path + discontinue rules
# --------------------------------------------------------------------------- #
def test_delta_job_calls_get_delta_not_fetch_catalog(client, sql, make_tenant, monkeypatch):
    tid = tenant_id_of(sql, make_tenant())
    adapter = RecordingAdapter([make_row(tid, "SKU-DELTA")])
    monkeypatch.setattr(ingest, "build_adapter", lambda *a, **k: adapter)

    job = wait_job_done(sql, enqueue(sql, "delta_scan", tid, {}))
    assert job["status"] == "done", job["last_error"]
    assert len(adapter.delta_calls) == 1, "delta job must fetch via get_delta"
    assert adapter.catalog_calls == 0, "delta job must not call fetch_catalog"
    assert product_state(sql, tid, "SKU-DELTA")["is_active"] is True


def test_delta_mode_never_discontinues_absent_skus_for_incremental_adapter(
        client, sql, make_tenant, monkeypatch):
    tid = tenant_id_of(sql, make_tenant())
    seed_product(sql, tid, "SKU-KEEP")
    seed_product(sql, tid, "SKU-ABSENT")
    adapter = RecordingAdapter([make_row(tid, "SKU-KEEP")])  # delta holds only one SKU
    monkeypatch.setattr(ingest, "build_adapter", lambda *a, **k: adapter)

    job = wait_job_done(sql, enqueue(sql, "delta_scan", tid, {"mark_missing_discontinued": True}))
    assert job["status"] == "done", job["last_error"]
    absent = product_state(sql, tid, "SKU-ABSENT")
    assert absent["is_active"] is True, "a partial delta must not discontinue absent SKUs"
    assert absent["stock_status"] != "discontinued"
    assert product_state(sql, tid, "SKU-KEEP")["is_active"] is True


def test_full_mode_still_discontinues_missing_skus(client, sql, make_tenant, monkeypatch):
    tid = tenant_id_of(sql, make_tenant())
    seed_product(sql, tid, "SKU-KEEP")
    seed_product(sql, tid, "SKU-ABSENT")
    adapter = RecordingAdapter([make_row(tid, "SKU-KEEP")])
    monkeypatch.setattr(ingest, "build_adapter", lambda *a, **k: adapter)

    job = wait_job_done(sql, enqueue(sql, "ingest", tid, {
        "mode": "full", "mark_missing_discontinued": True,
    }))
    assert job["status"] == "done", job["last_error"]
    assert adapter.catalog_calls == 1 and adapter.delta_calls == []
    absent = product_state(sql, tid, "SKU-ABSENT")
    assert absent["is_active"] is False and absent["stock_status"] == "discontinued"


def test_delta_mode_discontinues_when_adapter_is_full_snapshot(
        client, sql, make_tenant, monkeypatch):
    tid = tenant_id_of(sql, make_tenant())
    seed_product(sql, tid, "SKU-KEEP")
    seed_product(sql, tid, "SKU-ABSENT")
    adapter = RecordingAdapter([make_row(tid, "SKU-KEEP")], delta_is_full=True)
    monkeypatch.setattr(ingest, "build_adapter", lambda *a, **k: adapter)

    job = wait_job_done(sql, enqueue(sql, "delta_scan", tid, {}))
    assert job["status"] == "done", job["last_error"]
    assert len(adapter.delta_calls) == 1
    absent = product_state(sql, tid, "SKU-ABSENT")
    assert absent["is_active"] is False and absent["stock_status"] == "discontinued"


# --------------------------------------------------------------------------- #
# cursor
# --------------------------------------------------------------------------- #
def test_delta_cursor_uses_last_successful_job_and_advances(client, sql, make_tenant, monkeypatch):
    tid = tenant_id_of(sql, make_tenant())
    adapter = RecordingAdapter([make_row(tid, "SKU-1")])
    monkeypatch.setattr(ingest, "build_adapter", lambda *a, **k: adapter)

    first_id = enqueue(sql, "delta_scan", tid, {})
    first = wait_job_done(sql, first_id)
    assert first["status"] == "done", first["last_error"]
    assert adapter.delta_calls == [None], "no prior success -> None -> adapter full fetch"

    watermark = sql.execute("SELECT locked_at FROM jobs WHERE id=%s", (first_id,)).fetchone()["locked_at"]
    assert watermark is not None

    second = wait_job_done(sql, enqueue(sql, "delta_scan", tid, {}))
    assert second["status"] == "done", second["last_error"]
    assert adapter.delta_calls[1] == watermark, \
        "next delta must start from the last successful job's watermark"


# --------------------------------------------------------------------------- #
# scheduler modes
# --------------------------------------------------------------------------- #
def test_scheduler_enqueues_full_for_nightly_and_delta_for_hourly(monkeypatch):
    calls: list[dict] = []

    async def fake_enqueue(ctx, job_type, tenant_id, payload=None, *, match_payload=None):
        calls.append({"type": job_type, "tenant_id": tenant_id, "payload": payload,
                      "match_payload": match_payload})
        return f"job-{len(calls)}"

    async def fake_lock(ctx, key, fn):
        await fn()

    monkeypatch.setattr(scheduler_module, "enqueue_job_deduped", fake_enqueue)
    monkeypatch.setattr(scheduler_module, "_with_advisory_lock", fake_lock)

    cfg = {"adapters": {"catalog": "shopify", "csv_path": "feed.csv", "mock_count": 42}}

    class FakeCtx:
        async def default_tenant(self):
            return SimpleNamespace(id="tenant-1")

        async def config_for(self, tenant_id):
            return cfg

    sched = Scheduler(FakeCtx())
    asyncio.run(sched._fire("nightly_batch"))
    asyncio.run(sched._fire("hourly_delta"))

    assert [c["type"] for c in calls] == ["delta_scan", "delta_scan"]
    assert calls[0]["payload"]["mode"] == "full"
    assert calls[0]["payload"]["mark_missing_discontinued"] is True
    assert calls[1]["payload"]["mode"] == "delta"
    assert calls[1]["payload"]["mark_missing_discontinued"] is True
    # mode-scoped dedupe: a running hourly delta must never swallow the nightly full
    assert calls[0]["match_payload"] == {"mode": "full"}
    assert calls[1]["match_payload"] == {"mode": "delta"}


# --------------------------------------------------------------------------- #
# Shopify through the real job handler (MockTransport, offline)
# --------------------------------------------------------------------------- #
def shopify_delta_page(sku: str = "SHOP-DELTA-1") -> dict:
    return {
        "shop": {"currencyCode": "USD"},
        "products": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "edges": [{
                "node": {
                    "id": "gid://shopify/Product/9001",
                    "title": "Delta lamp",
                    "descriptionHtml": "<p>Nice</p>",
                    "productType": "lamp",
                    "tags": [],
                    "updatedAt": "2026-10-01T12:00:00Z",
                    "featuredImage": None,
                    "images": {"edges": []},
                    "variants": {"edges": [{"node": {
                        "id": "gid://shopify/ProductVariant/1",
                        "sku": sku,
                        "title": "Default",
                        "price": "19.99",
                        "compareAtPrice": None,
                        "inventoryQuantity": 3,
                        "availableForSale": True,
                        "selectedOptions": [],
                        "updatedAt": "2026-10-01T12:00:00Z",
                    }}]},
                },
            }],
        },
    }


def test_shopify_delta_job_uses_updated_at_cursor_and_spares_absent_skus(
        client, sql, make_tenant, monkeypatch):
    tid = tenant_id_of(sql, make_tenant())
    seed_product(sql, tid, "SKU-ABSENT")
    monkeypatch.setenv("SHOPIFY_SHOP_DOMAIN", "delta-store.myshopify.com")
    monkeypatch.setenv("SHOPIFY_ADMIN_TOKEN", "shpat_test")
    monkeypatch.setenv("SHOPIFY_API_VERSION", "2026-07")

    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"data": shopify_delta_page()})

    from app.adapters.shopify import ShopifyAdapter

    original_init = ShopifyAdapter.__init__

    def fake_init(self, tenant_id, **kwargs):
        original_init(self, tenant_id, transport=httpx.MockTransport(handler), retries=1)

    monkeypatch.setattr(ShopifyAdapter, "__init__", fake_init)

    job = wait_job_done(sql, enqueue(sql, "delta_scan", tid, {
        "adapter": "shopify",
        "since": "2026-10-01T00:00:00Z",
    }))
    assert job["status"] == "done", job["last_error"]
    assert len(sent) == 1
    assert sent[0]["variables"]["query"] == "status:active AND updated_at:>='2026-09-30T23:55:00Z'", \
        "delta cursor minus the 5-minute overlap must drive the GraphQL filter"

    imported = product_state(sql, tid, "SHOP-DELTA-1")
    assert imported is not None and imported["stock_status"] == "low_stock", \
        "delta rows still go through the normal mapping/upsert path"

    absent = product_state(sql, tid, "SKU-ABSENT")
    assert absent["is_active"] is True and absent["stock_status"] != "discontinued", \
        "Shopify delta (delta_is_full=False) must never mass-discontinue"
