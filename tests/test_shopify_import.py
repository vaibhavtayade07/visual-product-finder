"""Fixture-driven Shopify import through the real admin -> queue -> ingest path.

No store and no network: ShopifyAdapter's httpx transport is replaced with a
MockTransport that serves GraphQL fixture pages and public-CDN image bytes.
"""
from __future__ import annotations

import json
from decimal import Decimal

import httpx

from conftest import make_jpeg, wait_import


def _variant(sku: str, price: str, qty: int | None, available: bool, options=None):
    return {
        "node": {
            "id": f"gid://shopify/ProductVariant/{sku or 'blank'}",
            "sku": sku,
            "price": price,
            "compareAtPrice": None,
            "inventoryQuantity": qty,
            "availableForSale": available,
            "selectedOptions": options or [],
        }
    }


def _product(pid: str, title: str, sku: str, price: str, qty: int | None,
             available: bool, image: str, description: str, tags: list[str],
             options=None):
    return {
        "node": {
            "id": f"gid://shopify/Product/{pid}",
            "handle": f"product-{pid}",
            "title": title,
            "descriptionHtml": description,
            "productType": "Pendant",
            "vendor": "Lumen",
            "tags": tags,
            "updatedAt": "2026-10-01T12:00:00Z",
            "featuredImage": {"url": image},
            "images": {"edges": [{"node": {"url": image}}]},
            "variants": {"edges": [_variant(sku, price, qty, available, options)]},
        }
    }


def _payload():
    return {
        "data": {
            "shop": {"currencyCode": "USD"},
            "products": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "edges": [
                    _product(
                        "9001", "Aurora Pendant", "AUR-1", "89.90", 9, True,
                        "https://cdn.shopify.com/aurora.jpg",
                        "<p>Brass <strong>pendant</strong> &amp; shade</p>",
                        ["material:Brass", "finish:Brushed"],
                        options=[{"name": "Color", "value": "Brass"}],
                    ),
                    _product(
                        "9002", "Nova Table Lamp", "NOVA-2", "49.00", 0, False,
                        "https://cdn.shopify.com/nova.jpg",
                        "<p>Compact table lamp.</p>",
                        ["style:Modern"],
                    ),
                ],
            },
        }
    }


def test_shopify_import_end_to_end_offline(client, admin_headers, sql, make_tenant, monkeypatch):
    slug = make_tenant()
    aurora_image = make_jpeg(size=(512, 512), seed=31)
    nova_image = make_jpeg(size=(512, 512), seed=32)
    graphql_requests: list[dict] = []
    image_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "fixture.myshopify.com":
            body = json.loads(request.content)
            graphql_requests.append(body)
            assert request.headers["x-shopify-access-token"] == "shpat_fixture"
            return httpx.Response(200, json=_payload())
        if request.url.host == "cdn.shopify.com":
            image_requests.append(request)
            data = aurora_image if "aurora" in request.url.path else nova_image
            return httpx.Response(200, content=data, headers={"content-type": "image/jpeg"})
        return httpx.Response(404, text="unexpected host")

    monkeypatch.setenv("SHOPIFY_SHOP_DOMAIN", "fixture.myshopify.com")
    monkeypatch.setenv("SHOPIFY_ADMIN_TOKEN", "shpat_fixture")
    monkeypatch.setenv("SHOPIFY_API_VERSION", "2026-07")

    from app.adapters.shopify import ShopifyAdapter  # noqa: PLC0415

    original_init = ShopifyAdapter.__init__

    def init_with_fixture_transport(self, *args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(handler))
        kwargs.setdefault("sleep", lambda _seconds: None)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(ShopifyAdapter, "__init__", init_with_fixture_transport)

    resp = client.post(
        f"/api/v1/admin/imports?tenant={slug}",
        json={"adapter": "shopify"},
        headers=admin_headers,
    )
    assert resp.status_code == 202, resp.text

    status = wait_import(client, admin_headers, resp.json()["import_id"], tenant=slug)
    assert status["status"] == "done", status
    assert status["counts"] == {"total": 2, "imported": 2, "skipped": 0, "failed": 0}, status
    assert status["reembedded"] == 2
    assert status["validation_errors"] == []

    assert len(graphql_requests) == 1
    assert graphql_requests[0]["variables"]["query"] == "status:active"
    assert all("x-shopify-access-token" not in request.headers for request in image_requests)

    products = sql.execute(
        """SELECT sku, title, price, currency, stock_status, category, attributes, description
           FROM products WHERE tenant_id=(SELECT id FROM tenants WHERE slug=%s) ORDER BY sku""",
        (slug,),
    ).fetchall()
    assert [row["sku"] for row in products] == ["AUR-1", "NOVA-2"]

    aurora, nova = products
    assert aurora["title"] == "Aurora Pendant"
    assert aurora["price"] == Decimal("89.90")
    assert aurora["currency"] == "USD"
    assert aurora["stock_status"] == "in_stock"
    assert aurora["category"] == "Pendant"
    assert aurora["attributes"]["material"] == "Brass"
    assert aurora["attributes"]["color"] == "Brass"
    assert "<p>" not in aurora["description"] and "Brass pendant" in aurora["description"]

    assert nova["stock_status"] == "out_of_stock"
    assert nova["attributes"]["style"] == "Modern"

    images = sql.execute(
        """SELECT p.sku, pi.source_url, pi.quality_status FROM product_images pi
           JOIN products p ON p.id = pi.product_id
           WHERE p.tenant_id=(SELECT id FROM tenants WHERE slug=%s) ORDER BY p.sku""",
        (slug,),
    ).fetchall()
    assert [(row["sku"], row["source_url"]) for row in images] == [
        ("AUR-1", "https://cdn.shopify.com/aurora.jpg"),
        ("NOVA-2", "https://cdn.shopify.com/nova.jpg"),
    ]
    assert all(row["quality_status"] == "ok" for row in images)

    job = sql.execute(
        """SELECT status, last_error FROM jobs
           WHERE tenant_id=(SELECT id FROM tenants WHERE slug=%s) ORDER BY created_at DESC LIMIT 1""",
        (slug,),
    ).fetchone()
    assert job["status"] == "done" and not job["last_error"]
