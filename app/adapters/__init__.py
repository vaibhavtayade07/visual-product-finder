"""Adapter registry (architecture §9): `adapters.get(name)` -> adapter instance."""
from __future__ import annotations

from pathlib import Path

from .base import CatalogAdapter, FetchImageError, ImageRef, NormalizedProduct
from .csv_adapter import CsvAdapter
from .mock_adapter import MockCatalogAdapter

ADAPTER_TYPES = ("csv", "mock", "shopify", "woo")


def build_adapter(adapter_type: str, *, tenant_id: str, public_base_url: str,
                  count: int = 300, csv_path: str | Path | None = None,
                  image_dir: Path | None = None,
                  product_url_template: str | None = None) -> CatalogAdapter:
    if adapter_type == "mock":
        if image_dir is None:
            raise ValueError("mock adapter requires image_dir")
        return MockCatalogAdapter(tenant_id=tenant_id, image_dir=image_dir,
                                  public_base_url=public_base_url, count=count,
                                  product_url_template=product_url_template)
    if adapter_type == "csv":
        if not csv_path:
            raise ValueError("csv adapter requires csv_path")
        return CsvAdapter(tenant_id=tenant_id, path=csv_path,
                          product_url_template=product_url_template)
    if adapter_type == "shopify":
        from .shopify import ShopifyAdapter  # noqa: PLC0415

        return ShopifyAdapter(tenant_id=tenant_id)
    if adapter_type == "woo":
        from .woo import WooAdapter  # noqa: PLC0415

        return WooAdapter(tenant_id=tenant_id)
    raise ValueError(f"unknown adapter type: {adapter_type!r}")


__all__ = [
    "ADAPTER_TYPES", "CatalogAdapter", "CsvAdapter", "FetchImageError", "ImageRef",
    "MockCatalogAdapter", "NormalizedProduct", "build_adapter",
]
