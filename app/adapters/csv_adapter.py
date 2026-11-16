"""CSV catalog adapter (MVP, architecture §9).

Required columns: sku,title,price,stock,image_url. Optional: description,currency,attributes.
Malformed rows are collected into validation_errors with row numbers (AC6.4) — never
silently dropped. `attributes` accepts a JSON object or `key=value;key=value` pairs.
"""
from __future__ import annotations

import csv
import json
import logging
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable

import httpx

from .base import FetchImageError, ImageRef, NormalizedProduct

log = logging.getLogger(__name__)

REQUIRED_COLUMNS = ["sku", "title", "price", "stock", "image_url"]
STOCK_ALIASES = {
    "in_stock": "in_stock", "in stock": "in_stock", "instock": "in_stock", "1": "in_stock",
    "yes": "in_stock", "true": "in_stock", "available": "in_stock",
    "low_stock": "low_stock", "low stock": "low_stock", "low": "low_stock", "lowstock": "low_stock",
    "out_of_stock": "out_of_stock", "out of stock": "out_of_stock", "outofstock": "out_of_stock",
    "0": "out_of_stock", "no": "out_of_stock", "false": "out_of_stock", "unavailable": "out_of_stock",
}
KNOWN_ATTR_KEYS = {"type", "style", "finish", "color", "material", "size_class", "dimensions"}


class CsvAdapter:
    adapter_type = "csv"
    delta_is_full = True

    def __init__(self, tenant_id: str, path: str | Path,
                 product_url_template: str | None = None, fetch_timeout_s: float = 10.0) -> None:
        self.tenant_id = tenant_id
        self.path = Path(path)
        self.product_url_template = product_url_template
        self.fetch_timeout_s = fetch_timeout_s
        # populated by fetch_catalog()/get_delta()
        self.validation_errors: list[dict] = []
        self.total_rows: int = 0

    # ---- parsing -------------------------------------------------------------
    def _parse(self) -> tuple[list[NormalizedProduct], list[dict]]:
        rows: list[NormalizedProduct] = []
        errors: list[dict] = []
        with self.path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            header = [h.strip().lower() for h in (reader.fieldnames or [])]
            missing = [c for c in REQUIRED_COLUMNS if c not in header]
            if missing:
                raise ValueError(f"CSV missing required columns: {', '.join(missing)}")
            for line_no, raw in enumerate(reader, start=2):  # row 1 = header
                sku_hint = raw.get("sku").strip() if isinstance(raw.get("sku"), str) else None
                try:
                    row = {(k or "").strip().lower(): (v or "").strip()
                           for k, v in raw.items() if isinstance(v, str)}
                    extras = raw.get(None)  # DictReader puts overflow cells under key None
                    if extras and any(str(x or "").strip() for x in extras):
                        raise ValueError(f"row has more columns than the header: {extras!r}")
                    rows.append(self._parse_row(row))
                except ValueError as exc:
                    errors.append({"row": line_no, "sku": sku_hint or None, "reason": str(exc)})
        return rows, errors

    def _parse_row(self, row: dict) -> NormalizedProduct:
        sku = row.get("sku", "")
        if not sku:
            raise ValueError("sku is empty")
        title = row.get("title", "")
        if not title:
            raise ValueError("title is empty")
        price_raw = row.get("price", "")
        try:
            price = Decimal(price_raw) if price_raw else None
        except InvalidOperation as exc:
            raise ValueError(f"price not numeric: {price_raw!r}") from exc
        if price is not None and price < 0:
            raise ValueError(f"price is negative: {price_raw!r}")
        stock_raw = row.get("stock", "").lower()
        stock = STOCK_ALIASES.get(stock_raw)
        if stock is None:
            if stock_raw == "":
                stock = "out_of_stock"
            else:
                raise ValueError(f"unknown stock value: {row.get('stock')!r}")
        attributes = self._parse_attributes(row.get("attributes", ""))
        image_url = row.get("image_url", "")
        images = [ImageRef(source_url=image_url, ref=image_url)] if image_url else []
        category = row.get("category") or attributes.get("category") or None
        return NormalizedProduct(
            tenant_id=self.tenant_id,
            sku=sku,
            title=title,
            description=row.get("description") or None,
            price=price,
            currency=(row.get("currency") or "USD").upper()[:3],
            stock_status=stock,
            category=category,
            attributes=attributes,
            images=images,
            source_updated_at=_parse_ts(row.get("source_updated_at")),
        )

    @staticmethod
    def _parse_attributes(raw: str) -> dict:
        if not raw:
            return {}
        if raw.lstrip().startswith("{"):
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"attributes JSON invalid: {exc}") from exc
            if not isinstance(data, dict):
                raise ValueError("attributes JSON must be an object")
            return {str(k): v for k, v in data.items()}
        attrs: dict = {}
        for pair in raw.split(";"):
            pair = pair.strip()
            if not pair:
                continue
            if "=" not in pair:
                raise ValueError(f"attributes entry not key=value: {pair!r}")
            key, _, value = pair.partition("=")
            attrs[key.strip().lower()] = value.strip()
        return attrs

    # ---- CatalogAdapter ------------------------------------------------------
    def fetch_catalog(self) -> Iterable[NormalizedProduct]:
        rows, errors = self._parse()
        self.validation_errors = errors
        self.total_rows = len(rows) + len(errors)
        return rows

    def get_delta(self, since: datetime | None) -> Iterable[NormalizedProduct]:
        # CSV = full snapshot; the core diffs downstream by image hash / source_updated_at.
        return self.fetch_catalog()

    def fetch_image(self, image_ref: str) -> bytes:
        if image_ref.startswith(("filesystem:", "file://")):
            ref = image_ref.split(":", 1)[1]
            return Path(ref).read_bytes()
        last_error: Exception | None = None
        for attempt in range(3):  # 10s timeout, 2 retries (§5a)
            try:
                resp = httpx.get(image_ref, timeout=self.fetch_timeout_s, follow_redirects=True)
                resp.raise_for_status()
                return resp.content
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
        raise FetchImageError(f"image fetch failed: {image_ref}: {last_error}")


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.replace("Z", "+0000"), fmt)
        except ValueError:
            continue
    return None
