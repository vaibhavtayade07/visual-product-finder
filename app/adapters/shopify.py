"""Shopify catalog adapter — Admin GraphQL API (architecture §9).

One row per PRODUCT, not per variant (deliberate MVP tradeoff): the index answers
"find me a lamp like this", and variants normally share imagery/tags. The
representative variant (cheapest available, else the first) supplies SKU/price and its
selected options fill known attributes; inventory is aggregated across available
variants. Indexing every variant separately would multiply near-identical images and
split the business boost across sibling SKUs. Revisit only if a store sells variants
that are materially different products.

Full fetch: active products, 250 per page via `pageInfo`. Incremental fetch: the same
query plus an `updated_at:>=...` filter (small overlap buffer for clock skew); the core
pipeline still diffs by image hash/source timestamp, so overlap is idempotent.
Product images are public Shopify CDN URLs -> downloads deliberately send no auth header.
"""
from __future__ import annotations

import logging
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html import unescape
from typing import Callable, Iterable, Iterator
from urllib.parse import urlsplit

import httpx

from .base import FetchImageError, ImageRef, NormalizedProduct
from .csv_adapter import KNOWN_ATTR_KEYS

log = logging.getLogger(__name__)

# Stable Admin GraphQL version verified against shopify.dev (2026-07 docs). Override
# with SHOPIFY_API_VERSION when the store needs a newer pin.
DEFAULT_API_VERSION = "2026-07"
PAGE_SIZE = 250
DELTA_OVERLAP_S = 300
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # matches the ingest image cap

PRODUCTS_QUERY = """
query Products($first: Int!, $after: String, $query: String) {
  shop { currencyCode }
  products(first: $first, after: $after, query: $query) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        handle
        title
        descriptionHtml
        productType
        vendor
        tags
        updatedAt
        featuredImage { url }
        images(first: 10) { edges { node { url } } }
        variants(first: 250) {
          edges {
            node {
              id
              sku
              price
              compareAtPrice
              inventoryQuantity
              availableForSale
              selectedOptions { name value }
            }
          }
        }
      }
    }
  }
}
"""

_OPTION_KEY_ALIASES = {
    "size": "size_class",
    "sizeclass": "size_class",
    "colour": "color",
    "dimension": "dimensions",
}


class ShopifyConfigError(ValueError):
    """Missing/invalid Shopify credentials — surfaced as a clean admin 400."""


class ShopifyAPIError(RuntimeError):
    """Shopify returned an HTTP/GraphQL error; the import fails loudly."""


@dataclass
class _Variant:
    node: dict
    position: int


class ShopifyAdapter:
    adapter_type = "shopify"
    delta_is_full = False

    def __init__(
        self,
        tenant_id: str,
        shop_domain: str | None = None,
        access_token: str | None = None,
        api_version: str | None = None,
        *,
        low_stock_threshold: int = 5,
        timeout_s: float = 20.0,
        max_image_bytes: int = MAX_IMAGE_BYTES,
        retries: int = 3,
        page_size: int = PAGE_SIZE,
        delta_overlap_s: int = DELTA_OVERLAP_S,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.shop_domain = _normalize_shop_domain(
            shop_domain if shop_domain is not None else os.environ.get("SHOPIFY_SHOP_DOMAIN", "")
        )
        self.access_token = (
            access_token if access_token is not None else os.environ.get("SHOPIFY_ADMIN_TOKEN", "")
        ).strip()
        configured_version = (
            api_version if api_version is not None else os.environ.get("SHOPIFY_API_VERSION", "")
        )
        self.api_version = (configured_version or "").strip() or DEFAULT_API_VERSION
        self.low_stock_threshold = int(low_stock_threshold)
        self.timeout_s = float(timeout_s)
        self.max_image_bytes = int(max_image_bytes)
        self.retries = max(1, int(retries))
        self.page_size = max(1, int(page_size))
        self.delta_overlap_s = max(0, int(delta_overlap_s))
        self._transport = transport
        self._http = client
        self._sleep = sleep

        missing = []
        if not self.shop_domain:
            missing.append("SHOPIFY_SHOP_DOMAIN")
        if not self.access_token:
            missing.append("SHOPIFY_ADMIN_TOKEN")
        if missing:
            raise ShopifyConfigError(
                "Shopify adapter is not configured: set "
                + " and ".join(missing)
                + ". SHOPIFY_SHOP_DOMAIN looks like your-store.myshopify.com; "
                "SHOPIFY_ADMIN_TOKEN is an Admin API access token with "
                "read_products + read_inventory scopes."
            )

        # populated by fetch_catalog()/get_delta(), mirroring CsvAdapter
        self.validation_errors: list[dict] = []
        self.total_rows: int = 0

    # ---- CatalogAdapter ------------------------------------------------------
    def fetch_catalog(self) -> Iterable[NormalizedProduct]:
        return self._fetch("status:active")

    def get_delta(self, since: datetime | None) -> Iterable[NormalizedProduct]:
        if since is None:
            return self.fetch_catalog()
        if since.tzinfo is None:  # naive input is treated as UTC
            since = since.replace(tzinfo=timezone.utc)
        since = since.astimezone(timezone.utc) - timedelta(seconds=self.delta_overlap_s)
        stamp = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        return self._fetch(f"status:active AND updated_at:>='{stamp}'")

    def fetch_image(self, image_ref: str) -> bytes:
        """Download a public Shopify CDN image (no auth header), with retries + size cap."""
        url = (image_ref or "").strip()
        if not url:
            raise FetchImageError("image fetch failed: empty image reference")
        if urlsplit(url).scheme not in ("http", "https"):
            raise FetchImageError(f"image fetch failed: unsupported image URL: {url}")
        resp = self._request(
            "GET",
            url,
            error_cls=FetchImageError,
            what="Shopify image download",
        )
        declared = resp.headers.get("Content-Length")
        if declared:
            try:
                declared_bytes = int(declared)
            except ValueError:
                declared_bytes = 0
            if declared_bytes > self.max_image_bytes:
                raise FetchImageError(
                    f"image fetch failed: {url} exceeds the "
                    f"{self.max_image_bytes}-byte cap (Content-Length={declared_bytes})"
                )
        data = resp.content
        if len(data) > self.max_image_bytes:
            raise FetchImageError(
                f"image fetch failed: {url} exceeds the {self.max_image_bytes}-byte cap "
                f"({len(data)} bytes)"
            )
        if not data:
            raise FetchImageError(f"image fetch failed: {url} returned an empty body")
        return data

    # ---- HTTP ---------------------------------------------------------------
    @property
    def graphql_url(self) -> str:
        return f"https://{self.shop_domain}/admin/api/{self.api_version}/graphql.json"

    @contextmanager
    def _client_ctx(self) -> Iterator[httpx.Client]:
        if self._http is not None:
            yield self._http
            return
        kwargs: dict = {"timeout": self.timeout_s, "follow_redirects": True}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        with httpx.Client(**kwargs) as client:
            yield client

    def _request(self, method: str, url: str, *, error_cls: type[Exception],
                 what: str, **kwargs) -> httpx.Response:
        last_error: Exception | str | None = None
        for attempt in range(self.retries):
            try:
                with self._client_ctx() as client:
                    resp = client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt + 1 < self.retries:
                    self._backoff(attempt)
                    continue
                raise error_cls(f"{what} request failed after {self.retries} attempts: {exc}") from exc

            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}: {_body_snippet(resp)}"
                if attempt + 1 < self.retries:
                    self._sleep_for(resp, attempt)
                    continue
                raise error_cls(f"{what} failed with HTTP {resp.status_code}: {_body_snippet(resp)}")
            if resp.status_code >= 400:
                raise error_cls(
                    f"{what} failed with HTTP {resp.status_code}: {_body_snippet(resp)}"
                )
            return resp
        raise error_cls(f"{what} failed: {last_error}")

    def _backoff(self, attempt: int) -> None:
        delay = 0.5 * (2 ** attempt)
        (self._sleep or time.sleep)(delay)

    def _sleep_for(self, resp: httpx.Response, attempt: int) -> None:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                (self._sleep or time.sleep)(min(float(retry_after), 5.0))
                return
            except ValueError:
                pass
        self._backoff(attempt)

    def _graphql(self, variables: dict) -> dict:
        resp = self._request(
            "POST",
            self.graphql_url,
            error_cls=ShopifyAPIError,
            what="Shopify Admin API",
            json={"query": PRODUCTS_QUERY, "variables": variables},
            headers={
                "X-Shopify-Access-Token": self.access_token,
                "Accept": "application/json",
            },
        )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ShopifyAPIError(
                f"Shopify Admin API returned non-JSON (HTTP {resp.status_code}): "
                f"{_body_snippet(resp)}"
            ) from exc
        if not isinstance(payload, dict):
            raise ShopifyAPIError("Shopify Admin API returned an unexpected JSON payload")
        errors = payload.get("errors")
        if errors:
            raise ShopifyAPIError(f"Shopify GraphQL error: {_format_errors(errors)}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ShopifyAPIError("Shopify GraphQL response is missing `data`")
        user_errors = list(_walk_user_errors(data))
        if user_errors:
            raise ShopifyAPIError(
                "Shopify GraphQL userErrors: "
                + "; ".join(f"{path}: {_format_errors(items)}" for path, items in user_errors)
            )
        return data

    # ---- fetch + map ---------------------------------------------------------
    def _fetch(self, query_filter: str) -> list[NormalizedProduct]:
        rows: list[NormalizedProduct] = []
        self.validation_errors = []
        shop_currency: str | None = None
        after: str | None = None
        seen_cursors: set[str] = set()

        while True:
            variables: dict = {"first": self.page_size, "query": query_filter}
            if after is not None:
                variables["after"] = after
            data = self._graphql(variables)

            shop = data.get("shop")
            if isinstance(shop, dict) and shop.get("currencyCode"):
                shop_currency = str(shop["currencyCode"]).upper()[:3]

            products = data.get("products")
            if not isinstance(products, dict):
                raise ShopifyAPIError("Shopify GraphQL response is missing `data.products`")
            nodes = _connection_nodes(products)
            for node in nodes:
                ordinal = len(rows) + len(self.validation_errors) + 1
                mapped = self._map_product(node, shop_currency, ordinal)
                if mapped is not None:
                    rows.append(mapped)

            page_info = products.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                raise ShopifyAPIError(
                    "Shopify GraphQL pagination error: hasNextPage is true but endCursor is empty"
                )
            if cursor in seen_cursors:
                raise ShopifyAPIError(
                    f"Shopify GraphQL pagination error: repeated endCursor {cursor!r}"
                )
            seen_cursors.add(cursor)
            after = str(cursor)

        self.total_rows = len(rows) + len(self.validation_errors)
        return rows

    def _map_product(self, node: dict, shop_currency: str | None,
                     ordinal: int) -> NormalizedProduct | None:
        variants = [
            _Variant(v, i)
            for i, v in enumerate(_connection_nodes(node.get("variants")))
        ]
        product_id = _product_id(node.get("id"))
        sku_fallback = f"shopify-{product_id}"
        if not variants:
            self.validation_errors.append({
                "row": ordinal,
                "sku": sku_fallback,
                "reason": "Shopify product has no variants",
            })
            log.warning("shopify product %s has no variants; skipped", product_id)
            return None

        representative = _representative_variant(variants)
        rep_node = representative.node
        amount, variant_currency = _money(rep_node.get("price"))
        currency = _clean_currency(variant_currency) or _clean_currency(shop_currency) or "USD"
        sku = str(rep_node.get("sku") or "").strip()
        if not sku:  # representative blank: keep a real variant SKU before the fallback
            sku = next(
                (str(v.node.get("sku") or "").strip() for v in variants
                 if str(v.node.get("sku") or "").strip()),
                "",
            )
        sku = sku or sku_fallback

        title = str(node.get("title") or node.get("handle") or f"Shopify product {product_id}").strip()
        category = str(node.get("productType") or "").strip() or None

        attributes = _attributes_from_tags(node.get("tags"), rep_node)

        return NormalizedProduct(
            tenant_id=self.tenant_id,
            sku=sku,
            title=title,
            description=_plain_text(node.get("descriptionHtml") or node.get("description")),
            price=amount,
            currency=currency,
            stock_status=_stock_status(variants, self.low_stock_threshold),
            category=category,
            attributes=attributes,
            images=_map_images(node),
            source_updated_at=_parse_iso(node.get("updatedAt")),
        )


# --------------------------------------------------------------------------- #
# mapping helpers
# --------------------------------------------------------------------------- #
def _normalize_shop_domain(raw: str | None) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    if "://" in value:
        value = urlsplit(value).netloc or value
    return value.split("/", 1)[0].strip().lower()


def _product_id(raw: object) -> str:
    value = str(raw or "").strip().rstrip("/")
    if not value:
        return "unknown"
    return value.rsplit("/", 1)[-1] or "unknown"


def _connection_nodes(connection: object) -> list[dict]:
    """Accept GraphQL `edges { node }` and (fixture-friendly) `nodes`/list shapes."""
    if isinstance(connection, list):
        return [item for item in connection if isinstance(item, dict)]
    if not isinstance(connection, dict):
        return []
    edges = connection.get("edges")
    if isinstance(edges, list):
        return [
            edge["node"] for edge in edges
            if isinstance(edge, dict) and isinstance(edge.get("node"), dict)
        ]
    nodes = connection.get("nodes")
    if isinstance(nodes, list):
        return [item for item in nodes if isinstance(item, dict)]
    return []


def _money(value: object) -> tuple[Decimal | None, str | None]:
    """Shopify returns `Money` as a string; tolerate MoneyV2 `{amount, currencyCode}`."""
    currency: str | None = None
    if isinstance(value, dict):
        amount = value.get("amount")
        currency = value.get("currencyCode") or value.get("currency_code")
    else:
        amount = value
    if amount is None or amount == "":
        return None, currency
    try:
        return Decimal(str(amount)), currency
    except (InvalidOperation, ValueError):
        return None, currency


def _representative_variant(variants: list[_Variant]) -> _Variant:
    available = [v for v in variants if _available(v.node)]
    if not available:
        return variants[0]  # spec: fall back to the first variant, not the cheapest
    return min(available, key=lambda v: _variant_sort_key(v))


def _variant_sort_key(variant: _Variant) -> tuple:
    amount, _ = _money(variant.node.get("price"))
    # valid prices first (ascending), then original position -> deterministic
    return (amount is None, amount if amount is not None else Decimal(0), variant.position)


def _available(node: dict) -> bool:
    return bool(node.get("availableForSale", node.get("available", False)))


def _stock_status(variants: list[_Variant], low_stock_threshold: int) -> str:
    available = [v.node for v in variants if _available(v.node)]
    if not available:
        return "out_of_stock"
    quantities = [_int_or_none(v.get("inventoryQuantity")) for v in available]
    tracked = [q for q in quantities if q is not None]
    if not tracked:  # untracked inventory -> availability is the only signal
        return "in_stock"
    return "low_stock" if sum(tracked) <= low_stock_threshold else "in_stock"


def _int_or_none(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _attributes_from_tags(tags: object, variant: dict) -> dict:
    attrs: dict = {}
    if isinstance(tags, str):
        tag_list = tags.split(",")
    elif isinstance(tags, list):
        tag_list = tags
    else:
        tag_list = []
    for raw in tag_list:
        if not isinstance(raw, str):
            continue
        tag = raw.strip()
        separator = ":" if ":" in tag else ("=" if "=" in tag else "")
        if not separator:
            continue
        key, _, value = tag.partition(separator)
        key = _canonical_attr_key(key)
        value = value.strip()
        if key in KNOWN_ATTR_KEYS and value:
            attrs.setdefault(key, value)

    # Variant option summary: only names that map to known attributes (lean by design).
    for option in variant.get("selectedOptions") or []:
        if not isinstance(option, dict):
            continue
        key = _canonical_attr_key(option.get("name"))
        value = str(option.get("value") or "").strip()
        if key in KNOWN_ATTR_KEYS and value:
            attrs.setdefault(key, value)
    return attrs


def _canonical_attr_key(raw: object) -> str:
    key = re.sub(r"[\s-]+", "_", str(raw or "").strip().lower())
    return _OPTION_KEY_ALIASES.get(key, key)


def _map_images(node: dict) -> list[ImageRef]:
    urls: list[str] = []
    featured = node.get("featuredImage")
    if isinstance(featured, dict):
        url = str(featured.get("url") or "").strip()
        if url:
            urls.append(url)
    for image in _connection_nodes(node.get("images")):
        url = str(image.get("url") or "").strip()
        if url and url not in urls:
            urls.append(url)
    return [
        ImageRef(source_url=url, ref=url, role="primary" if index == 0 else "extra")
        for index, url in enumerate(urls)
    ]


_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_TAG_RE = re.compile(r"(?is)</?(?:p|div|br|li|ul|ol|h[1-6]|tr|td)[^>]*>")


def _plain_text(raw: object) -> str | None:
    if not raw:
        return None
    text = _BLOCK_TAG_RE.sub(" ", str(raw))
    text = _TAG_RE.sub(" ", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _parse_iso(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))  # 3.11+ parses trailing "Z" directly
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _clean_currency(raw: object) -> str | None:
    value = str(raw or "").strip().upper()[:3]
    return value or None


def _format_errors(errors: object) -> str:
    if not isinstance(errors, list):
        return str(errors)
    messages = []
    for error in errors:
        if isinstance(error, dict):
            message = str(error.get("message") or error)
            path = error.get("path")
            if path:
                message += f" (path: {_format_path(path)})"
            messages.append(message)
        else:
            messages.append(str(error))
    return "; ".join(messages) or "unknown error"


def _format_path(path: object) -> str:
    if isinstance(path, (list, tuple)):
        return ".".join(str(part) for part in path)
    return str(path)


def _walk_user_errors(value: object, path: str = "data") -> Iterator[tuple[str, object]]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key == "userErrors" and child:
                yield child_path, child
            elif isinstance(child, (dict, list)):
                yield from _walk_user_errors(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_user_errors(child, f"{path}[{index}]")


def _body_snippet(resp: httpx.Response) -> str:
    try:
        text = resp.text
    except Exception:  # noqa: BLE001 - error path must never mask the original failure
        return "<unreadable body>"
    return text[:300] + ("…" if len(text) > 300 else "")
