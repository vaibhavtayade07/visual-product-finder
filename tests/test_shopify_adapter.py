"""Offline Shopify adapter tests: Admin GraphQL fixtures over httpx.MockTransport."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from app.adapters import build_adapter
from app.adapters.base import FetchImageError
from app.adapters.shopify import (
    ShopifyAdapter,
    ShopifyAPIError,
    ShopifyConfigError,
)

SHOP = "test.myshopify.com"
TOKEN = "shpat_test"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def variant(
    sku="SKU-1",
    price="10.00",
    qty=10,
    available=True,
    options=None,
    variant_id="1",
):
    return {
        "node": {
            "id": f"gid://shopify/ProductVariant/{variant_id}",
            "sku": sku,
            "price": price,
            "compareAtPrice": None,
            "inventoryQuantity": qty,
            "availableForSale": available,
            "selectedOptions": options or [],
        }
    }


def product(
    pid=101,
    title="Aurora Pendant",
    description="<p>A <strong>lamp</strong> &amp; shade</p>",
    product_type="Pendant",
    tags=None,
    variants=None,
    featured="https://cdn.shopify.com/a.jpg",
    images=None,
    updated_at="2026-10-01T12:00:00Z",
):
    if images is None:
        images = [featured, "https://cdn.shopify.com/b.jpg", ""]
    return {
        "node": {
            "id": f"gid://shopify/Product/{pid}",
            "handle": f"lamp-{pid}",
            "title": title,
            "descriptionHtml": description,
            "productType": product_type,
            "vendor": "Lumen",
            "tags": tags if tags is not None else ["type:pendant", "material:Brass"],
            "updatedAt": updated_at,
            "featuredImage": {"url": featured} if featured else None,
            "images": {"edges": [{"node": {"url": url}} for url in images]},
            "variants": {"edges": variants if variants is not None else [variant()]},
        }
    }


def page(nodes, *, has_next=False, cursor=None, currency="USD", user_errors=None):
    products = {
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        "edges": nodes,
    }
    if user_errors is not None:
        products["userErrors"] = user_errors
    return {"data": {"shop": {"currencyCode": currency}, "products": products}}


def make_adapter(handler, **kwargs):
    kwargs.setdefault("shop_domain", SHOP)
    kwargs.setdefault("access_token", TOKEN)
    kwargs.setdefault("sleep", lambda _seconds: None)
    return ShopifyAdapter("tenant-1", transport=httpx.MockTransport(handler), **kwargs)


def single_page(handler_response):
    def handler(request):
        return httpx.Response(200, json=handler_response)

    return handler


# --------------------------------------------------------------------------- #
# GraphQL plumbing / pagination
# --------------------------------------------------------------------------- #
def test_fetch_catalog_paginates_and_sends_admin_auth():
    requests = []

    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        if body["variables"].get("after") is None:
            return httpx.Response(200, json=page(
                [product(1), product(2)], has_next=True, cursor="cursor-1"))
        return httpx.Response(200, json=page([product(3)]))

    adapter = make_adapter(handler)
    rows = list(adapter.fetch_catalog())

    assert [row.sku for row in rows] == ["SKU-1", "SKU-1", "SKU-1"]
    assert len(requests) == 2
    assert str(requests[0].url) == "https://test.myshopify.com/admin/api/2026-07/graphql.json"
    assert requests[0].headers["x-shopify-access-token"] == TOKEN

    first = json.loads(requests[0].content)
    second = json.loads(requests[1].content)
    assert first["variables"]["first"] == 250
    assert first["variables"]["query"] == "status:active"
    assert "after" not in first["variables"]
    assert second["variables"]["after"] == "cursor-1"
    assert adapter.total_rows == 3 and adapter.validation_errors == []


def test_mapping_representative_variant_currency_tags_options_images():
    variants = [
        variant("SKU-B", "99.00", qty=10, variant_id="2"),
        variant(
            "SKU-A",
            {"amount": "12.50", "currencyCode": "EUR"},
            qty=2,
            options=[
                {"name": "Color", "value": "Black"},
                {"name": "Size", "value": "Large"},
                {"name": "Voltage", "value": "120V"},
            ],
            variant_id="3",
        ),
        variant("SKU-C", "1.00", qty=0, available=False, variant_id="4"),
    ]
    node = product(
        101,
        tags=["Material:Brass", "finish=Matte Black", "style:Modern", "loose"],
        variants=variants,
    )
    adapter = make_adapter(single_page(page([node])))
    row = list(adapter.fetch_catalog())[0]

    assert row.sku == "SKU-A", "cheapest in-stock variant is representative"
    assert row.price == Decimal("12.50")
    assert row.currency == "EUR"
    assert row.stock_status == "in_stock", "available inventory 2 + 10 = 12 > threshold"
    assert row.category == "Pendant"
    assert row.attributes["material"] == "Brass"
    assert row.attributes["finish"] == "Matte Black"
    assert row.attributes["style"] == "Modern"
    assert row.attributes["color"] == "Black"
    assert row.attributes["size_class"] == "Large"
    assert "voltage" not in row.attributes, "unknown options stay out of lean attributes"
    assert row.description == "A lamp & shade"
    assert row.source_updated_at == datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

    assert [image.role for image in row.images] == ["primary", "extra"]
    assert [image.source_url for image in row.images] == [
        "https://cdn.shopify.com/a.jpg",
        "https://cdn.shopify.com/b.jpg",
    ]
    assert all(image.ref == image.source_url for image in row.images)


def test_currency_falls_back_to_shop_currency_and_blank_sku_uses_product_id():
    node = product(
        202,
        variants=[variant(sku="", price="19.99", qty=2)],
        featured=None,
        images=["https://cdn.shopify.com/c.jpg"],
    )
    adapter = make_adapter(single_page(page([node], currency="cad")))
    row = list(adapter.fetch_catalog())[0]

    assert row.sku == "shopify-202"
    assert row.currency == "CAD"
    assert row.price == Decimal("19.99")
    assert row.images[0].role == "primary", "first image becomes primary when no featuredImage"


def test_out_of_stock_uses_first_variant_when_none_available():
    node = product(303, variants=[
        variant("FIRST", "50.00", qty=0, available=False),
        variant("CHEAP", "10.00", qty=5, available=False),
    ])
    row = list(make_adapter(single_page(page([node]))).fetch_catalog())[0]

    assert row.stock_status == "out_of_stock"
    assert row.sku == "FIRST", "no in-stock variant -> first variant is representative"
    assert row.price == Decimal("50.00")


def test_stock_status_low_stock_threshold_and_untracked_inventory():
    nodes = [
        product(401, variants=[variant(qty=3)]),
        product(402, variants=[variant(qty=None)]),
        product(403, variants=[variant(qty=6)]),
        product(404, variants=[variant(qty=2), variant(qty=2)]),
    ]
    rows = list(make_adapter(single_page(page(nodes))).fetch_catalog())
    assert [row.stock_status for row in rows] == [
        "low_stock", "in_stock", "in_stock", "low_stock",
    ]

    looser = list(make_adapter(
        single_page(page(nodes)), low_stock_threshold=3).fetch_catalog())
    assert [row.stock_status for row in looser] == [
        "low_stock", "in_stock", "in_stock", "in_stock",
    ]


def test_product_without_variants_lands_in_validation_errors():
    node = product(505)
    node["node"]["variants"] = {"edges": []}
    adapter = make_adapter(single_page(page([node])))
    rows = list(adapter.fetch_catalog())

    assert rows == []
    assert adapter.total_rows == 1
    assert adapter.validation_errors[0]["reason"] == "Shopify product has no variants"
    assert adapter.validation_errors[0]["sku"] == "shopify-505"


# --------------------------------------------------------------------------- #
# delta
# --------------------------------------------------------------------------- #
def test_delta_query_includes_updated_at_filter_and_overlap():
    queries = []

    def handler(request):
        queries.append(json.loads(request.content)["variables"]["query"])
        return httpx.Response(200, json=page([]))

    since = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    adapter = make_adapter(handler, delta_overlap_s=0)
    list(adapter.get_delta(since))

    assert adapter.delta_is_full is False
    assert queries[0] == "status:active AND updated_at:>='2026-10-01T12:00:00Z'"

    list(make_adapter(handler).get_delta(since))
    assert queries[1] == "status:active AND updated_at:>='2026-10-01T11:55:00Z'"

    list(adapter.get_delta(None))
    assert queries[2] == "status:active", "since=None is a full snapshot"


def test_delta_accepts_naive_utc_datetime():
    queries = []

    def handler(request):
        queries.append(json.loads(request.content)["variables"]["query"])
        return httpx.Response(200, json=page([]))

    list(make_adapter(handler, delta_overlap_s=0).get_delta(datetime(2026, 10, 1, 12, 0)))
    assert "2026-10-01T12:00:00Z" in queries[0]


# --------------------------------------------------------------------------- #
# errors / config
# --------------------------------------------------------------------------- #
def test_graphql_errors_and_user_errors_surface_clearly():
    def graphql_error(request):
        return httpx.Response(200, json={
            "errors": [{"message": "Access denied", "path": ["products"]}],
        })

    with pytest.raises(ShopifyAPIError) as exc:
        list(make_adapter(graphql_error).fetch_catalog())
    assert "Access denied" in str(exc.value) and "products" in str(exc.value)

    def user_error(request):
        return httpx.Response(200, json=page([], user_errors=[
            {"field": ["query"], "message": "Invalid filter"},
        ]))

    with pytest.raises(ShopifyAPIError) as exc:
        list(make_adapter(user_error).fetch_catalog())
    assert "userErrors" in str(exc.value) and "Invalid filter" in str(exc.value)


def test_http_errors_surface_status_and_body():
    def handler(request):
        return httpx.Response(401, json={"errors": [{"message": "Invalid token"}]})

    with pytest.raises(ShopifyAPIError) as exc:
        list(make_adapter(handler).fetch_catalog())
    assert "401" in str(exc.value) and "Invalid token" in str(exc.value)


def test_missing_credentials_raise_actionable_config_error(monkeypatch):
    monkeypatch.delenv("SHOPIFY_SHOP_DOMAIN", raising=False)
    monkeypatch.delenv("SHOPIFY_ADMIN_TOKEN", raising=False)

    with pytest.raises(ShopifyConfigError) as exc:
        ShopifyAdapter("t")
    message = str(exc.value)
    assert "SHOPIFY_SHOP_DOMAIN" in message
    assert "SHOPIFY_ADMIN_TOKEN" in message
    assert "read_products" in message and "read_inventory" in message

    with pytest.raises(ShopifyConfigError, match="SHOPIFY_ADMIN_TOKEN"):
        ShopifyAdapter("t", shop_domain=SHOP, access_token="")


def test_build_adapter_registry_wires_shopify(monkeypatch):
    monkeypatch.setenv("SHOPIFY_SHOP_DOMAIN", SHOP)
    monkeypatch.setenv("SHOPIFY_ADMIN_TOKEN", TOKEN)
    adapter = build_adapter("shopify", tenant_id="t", public_base_url="http://unused")
    assert isinstance(adapter, ShopifyAdapter)
    assert adapter.adapter_type == "shopify" and adapter.delta_is_full is False
    assert adapter.graphql_url.endswith("/admin/api/2026-07/graphql.json")


# --------------------------------------------------------------------------- #
# images
# --------------------------------------------------------------------------- #
def test_image_fetch_retries_then_succeeds_without_auth_header():
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, content=b"\xff\xd8\xff-image-bytes",
                              headers={"content-type": "image/jpeg"})

    data = make_adapter(handler).fetch_image("https://cdn.shopify.com/lamp.jpg")
    assert data == b"\xff\xd8\xff-image-bytes"
    assert len(requests) == 3
    assert all("x-shopify-access-token" not in request.headers for request in requests)


def test_image_fetch_persistent_failure_raises_fetch_image_error():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500, text="nope")

    with pytest.raises(FetchImageError, match="500"):
        make_adapter(handler).fetch_image("https://cdn.shopify.com/lamp.jpg")
    assert len(calls) == 3


def test_image_fetch_enforces_size_cap_and_url_scheme():
    def handler(request):
        return httpx.Response(200, content=b"12345")

    adapter = make_adapter(handler, max_image_bytes=4)
    with pytest.raises(FetchImageError, match="cap"):
        adapter.fetch_image("https://cdn.shopify.com/big.jpg")
    with pytest.raises(FetchImageError, match="unsupported image URL"):
        adapter.fetch_image("filesystem:C:/tmp/lamp.jpg")
