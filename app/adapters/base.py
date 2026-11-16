"""Catalog adapter interface (architecture §9). Core is adapter-agnostic."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Literal, Protocol


class FetchImageError(Exception):
    """Image fetch failed after retries -> hygiene finding fetch_failed (AC6.3)."""


@dataclass
class ImageRef:
    source_url: str
    role: Literal["primary", "extra"] = "primary"
    ref: str | None = None  # adapter-internal handle (e.g. mock filename)


@dataclass
class NormalizedProduct:
    tenant_id: str
    sku: str
    title: str
    description: str | None
    price: Decimal | None
    currency: str
    stock_status: Literal["in_stock", "low_stock", "out_of_stock"]
    category: str | None
    attributes: dict = field(default_factory=dict)
    images: list[ImageRef] = field(default_factory=list)
    source_updated_at: datetime | None = None


class CatalogAdapter(Protocol):
    adapter_type: str
    delta_is_full: bool  # True when get_delta() returns a full snapshot (diffed downstream)

    def fetch_catalog(self) -> Iterable[NormalizedProduct]: ...
    def get_delta(self, since: datetime | None) -> Iterable[NormalizedProduct]: ...
    def fetch_image(self, image_ref: str) -> bytes: ...
