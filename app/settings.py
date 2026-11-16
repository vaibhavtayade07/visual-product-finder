"""Environment-driven settings. Values in .env (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", PROJECT_ROOT / "data"))


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _float(name: str, default: float) -> float | None:
    raw = os.environ.get(name)
    return float(raw) if raw and raw.strip() else None


def load_dotenv(path: Path) -> None:
    """Tiny .env loader (no dependency); does not override real env vars."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    app_mode: str = field(default_factory=lambda: os.environ.get("APP_MODE", "mock").lower())
    database_url: str = field(
        default_factory=lambda: os.environ.get(
            "DATABASE_URL", "postgresql://finder:finder@localhost:5433/finder"
        )
    )
    test_database_url: str = field(
        default_factory=lambda: os.environ.get(
            "TEST_DATABASE_URL", "postgresql://finder:finder@localhost:5433/finder_test"
        )
    )
    public_base_url: str = field(
        default_factory=lambda: os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    )
    default_tenant_slug: str = field(
        default_factory=lambda: os.environ.get("DEFAULT_TENANT_SLUG", "demo")
    )
    admin_token: str = field(default_factory=lambda: os.environ.get("ADMIN_TOKEN", "dev-admin-token"))
    log_level: str = field(default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO").upper())
    run_migrations_on_start: bool = field(default_factory=lambda: _bool("RUN_MIGRATIONS_ON_START", True))
    workers_enabled: bool = field(default_factory=lambda: _bool("WORKERS_ENABLED", True))
    scheduler_enabled: bool = field(default_factory=lambda: _bool("SCHEDULER_ENABLED", True))

    storage_backend: str = field(default_factory=lambda: os.environ.get("STORAGE_BACKEND", "local").lower())
    r2_account_id: str = field(default_factory=lambda: os.environ.get("R2_ACCOUNT_ID", ""))
    r2_access_key_id: str = field(default_factory=lambda: os.environ.get("R2_ACCESS_KEY_ID", ""))
    r2_secret_access_key: str = field(default_factory=lambda: os.environ.get("R2_SECRET_ACCESS_KEY", ""))
    r2_bucket: str = field(default_factory=lambda: os.environ.get("R2_BUCKET", ""))
    r2_public_base_url: str = field(default_factory=lambda: os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/"))

    embedding_provider: str = field(
        default_factory=lambda: os.environ.get("EMBEDDING_PROVIDER", "auto").lower()
    )
    vision_provider: str = field(default_factory=lambda: os.environ.get("VISION_PROVIDER", "auto").lower())
    rerank_mode: str = field(default_factory=lambda: os.environ.get("RERANK_MODE", "vision").lower())
    embedding_model: str = field(
        default_factory=lambda: os.environ.get("EMBEDDING_MODEL", "multimodalembedding@001")
    )
    local_clip_model: str = field(
        default_factory=lambda: os.environ.get("LOCAL_CLIP_MODEL", "Qdrant/clip-ViT-B-32-vision")
    )
    local_clip_threads: int = field(default_factory=lambda: _int("LOCAL_CLIP_THREADS", 4))
    local_clip_cache_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("LOCAL_CLIP_CACHE_DIR", DATA_DIR / "models"))
    )
    gcp_project_id: str = field(default_factory=lambda: os.environ.get("GCP_PROJECT_ID", ""))
    gcp_sa_json: str = field(default_factory=lambda: os.environ.get("GCP_SA_JSON", ""))
    vertex_location: str = field(default_factory=lambda: os.environ.get("VERTEX_LOCATION", "us-central1"))
    gemini_api_key: str = field(default_factory=lambda: os.environ.get("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.environ.get("GEMINI_MODEL", "gemini-3-flash"))
    openai_api_key: str = field(default_factory=lambda: os.environ.get("OPENAI_API_KEY", ""))
    openai_model: str = field(default_factory=lambda: os.environ.get("OPENAI_MODEL", "gpt-5-mini"))
    aws_region: str = field(default_factory=lambda: os.environ.get("AWS_REGION", "us-east-1"))

    shopify_shop_domain: str = field(
        default_factory=lambda: os.environ.get("SHOPIFY_SHOP_DOMAIN", "")
    )
    shopify_admin_token: str = field(
        default_factory=lambda: os.environ.get("SHOPIFY_ADMIN_TOKEN", "")
    )
    shopify_api_version: str = field(
        default_factory=lambda: os.environ.get("SHOPIFY_API_VERSION", "2026-07")
    )

    query_cap_monthly_env: int | None = field(
        default_factory=lambda: _int("QUERY_CAP_MONTHLY", 0) or None
    )
    retention_days_env: int | None = field(default_factory=lambda: _int("RETENTION_DAYS", 0) or None)

    mock_llm_delay_s: float = field(default_factory=lambda: _float("MOCK_LLM_DELAY_S", 0.0) or 0.0)
    mock_query_category: str = field(default_factory=lambda: os.environ.get("MOCK_QUERY_CATEGORY", ""))
    mock_query_confidence: float | None = field(
        default_factory=lambda: _float("MOCK_QUERY_CONFIDENCE", None)
    )
    cors_origins: list[str] = field(
        default_factory=lambda: [
            o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()
        ]
    )
    data_dir: Path = DATA_DIR

    @property
    def is_mock(self) -> bool:
        return self.app_mode == "mock"


@lru_cache
def get_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
