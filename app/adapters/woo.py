"""WooCommerce catalog adapter — interface-only stub (architecture §9).

TODO: implement against the WooCommerce REST API (products?modified_after=...) when the
first store's platform is confirmed (spec Open Q1). Implement fetch_catalog(),
get_delta(since), fetch_image() and register `adapter_type = "woo"`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from .base import NormalizedProduct

_TODO = "WooAdapter is a stub — implement per architecture §9 when the store platform is confirmed."


class WooAdapter:
    adapter_type = "woo"
    delta_is_full = False

    def __init__(self, tenant_id: str, base_url: str = "", consumer_key: str = "", consumer_secret: str = "") -> None:
        self.tenant_id = tenant_id
        self.base_url = base_url
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret

    def fetch_catalog(self) -> Iterable[NormalizedProduct]:
        raise NotImplementedError(_TODO)

    def get_delta(self, since: datetime | None) -> Iterable[NormalizedProduct]:
        raise NotImplementedError(_TODO)

    def fetch_image(self, image_ref: str) -> bytes:
        raise NotImplementedError(_TODO)
