"""Shared runtime context: DB, storage, providers, caches, queues (built in lifespan)."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .db import Database
from .providers.base import EmbeddingProvider, VisionProvider
from .settings import PROJECT_ROOT, Settings
from .storage import LocalStorage, R2Storage, StorageProvider

CONFIG_CACHE_TTL = 60.0
POPULAR_CACHE_TTL = 300.0


def load_defaults() -> dict:
    with (PROJECT_ROOT / "config" / "defaults.yaml").open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class Tenant:
    id: str
    slug: str
    name: str
    config: dict


@dataclass
class AppContext:
    settings: Settings
    db: Database
    storage: StorageProvider
    embed: EmbeddingProvider
    vision: VisionProvider | None
    query_queue: asyncio.Queue
    defaults: dict = field(default_factory=load_defaults)
    _config_cache: dict[str, tuple[float, dict]] = field(default_factory=dict)
    _tenant_cache: dict[str, Tenant] = field(default_factory=dict)
    rate_windows: dict[str, list[float]] = field(default_factory=dict)
    popular_cache: dict[str, tuple[float, dict]] = field(default_factory=dict)
    degraded_providers: list[str] = field(default_factory=list)
    worker_pool: Any = None
    scheduler: Any = None
    started_at: float = field(default_factory=time.time)

    # ---- tenants & config ----------------------------------------------------
    def effective_config(self, tenant_config: dict) -> dict:
        cfg = deep_merge(self.defaults, tenant_config or {})
        if self.settings.query_cap_monthly_env:
            cfg["query_cap_monthly"] = self.settings.query_cap_monthly_env
        if self.settings.retention_days_env:
            cfg["retention_days"] = self.settings.retention_days_env
        return cfg

    async def get_tenant_by_slug(self, slug: str) -> Tenant | None:
        cached = self._tenant_cache.get(slug)
        if cached:
            return cached
        row = await self.db.fetch_one(
            "SELECT id, slug, name, config FROM tenants WHERE slug = %s", (slug,)
        )
        if not row:
            return None
        tenant = Tenant(id=str(row["id"]), slug=row["slug"], name=row["name"], config=row["config"] or {})
        self._tenant_cache[slug] = tenant
        return tenant

    async def default_tenant(self) -> Tenant:
        tenant = await self.get_tenant_by_slug(self.settings.default_tenant_slug)
        if not tenant:
            raise RuntimeError(
                f"default tenant {self.settings.default_tenant_slug!r} missing — did startup seeding run?"
            )
        return tenant

    async def config_for(self, tenant_id: str) -> dict:
        now = time.time()
        cached = self._config_cache.get(tenant_id)
        if cached and now - cached[0] < CONFIG_CACHE_TTL:
            return cached[1]
        row = await self.db.fetch_one("SELECT config FROM tenants WHERE id = %s", (tenant_id,))
        cfg = self.effective_config(row["config"] if row else {})
        self._config_cache[tenant_id] = (now, cfg)
        return cfg

    def invalidate_config(self, tenant_id: str) -> None:
        self._config_cache.pop(tenant_id, None)
        self._tenant_cache.clear()

    # ---- rate limiting (in-process token bucket, per IP) ----------------------
    def rate_allowed(self, key: str, limit_per_min: int) -> bool:
        now = time.time()
        window = [t for t in self.rate_windows.get(key, []) if now - t < 60.0]
        if len(window) >= limit_per_min:
            self.rate_windows[key] = window
            return False
        window.append(now)
        self.rate_windows[key] = window
        return True


def build_storage(settings: Settings) -> StorageProvider:
    if settings.storage_backend == "r2":
        return R2Storage(
            account_id=settings.r2_account_id,
            access_key_id=settings.r2_access_key_id,
            secret_access_key=settings.r2_secret_access_key,
            bucket=settings.r2_bucket,
            public_base_url=settings.r2_public_base_url,
        )
    return LocalStorage(settings.data_dir / "storage", settings.public_base_url)


def mock_image_dir(settings: Settings) -> Path:
    return settings.data_dir / "mock_images"
