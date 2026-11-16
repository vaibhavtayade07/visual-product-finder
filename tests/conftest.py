"""Shared test fixtures.

Tests run against a real Postgres (docker compose up -d db) with the real lifespan:
migrations run on startup, the worker pool consumes the Postgres job queue and the
asyncio query queue in-process, mock providers are used end-to-end. Scheduler is
disabled — retention/orphan jobs are triggered explicitly by inserting job rows.

`finder_test` is created automatically on first run (the docker role is superuser).
"""
from __future__ import annotations

import io
import os
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://finder:finder@localhost:5433/finder_test"
)

# Environment MUST be pinned before any `app.*` import: settings read env at import time,
# and the dev .env (if present) must never win over the test database.
os.environ["TEST_DATABASE_URL"] = TEST_DB_URL
os.environ["DATABASE_URL"] = TEST_DB_URL
os.environ["APP_MODE"] = "mock"
# Never let a developer's .env (e.g. EMBEDDING_PROVIDER=local_clip) trigger a model
# download inside pytest: the suite always runs the keyless mock embedder.
os.environ["EMBEDDING_PROVIDER"] = "mock"
os.environ["WORKERS_ENABLED"] = "true"
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["RUN_MIGRATIONS_ON_START"] = "true"
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["DATA_DIR"] = str(PROJECT_ROOT / "data-test")
os.environ.setdefault("ADMIN_TOKEN", "dev-admin-token")
# Never let a developer's real Shopify credentials leak into the offline test suite.
# Empty (not popped) also prevents load_dotenv() from filling them from a local .env.
os.environ["SHOPIFY_SHOP_DOMAIN"] = ""
os.environ["SHOPIFY_ADMIN_TOKEN"] = ""
os.environ["SHOPIFY_API_VERSION"] = ""

from app.runtime import ensure_selector_loop_on_windows  # noqa: E402

ensure_selector_loop_on_windows()  # must run before any loop exists (TestClient portal)

import psycopg  # noqa: E402
import pytest  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

ADMIN_TOKEN = os.environ["ADMIN_TOKEN"]
DEMO_SLUG = os.environ.get("DEFAULT_TENANT_SLUG", "demo")

TRANSIENT_TABLES = ["clicks", "leads", "provider_calls", "jobs", "search_queries",
                    "consents", "usage_counters"]
CATALOG_TABLES = ["clicks", "leads", "consents", "provider_calls", "hygiene_findings",
                  "jobs", "search_queries", "product_tags", "product_images",
                  "image_embeddings", "products", "usage_counters"]


# --------------------------------------------------------------------------- #
# helpers (importable from test modules: `from conftest import ...`)
# --------------------------------------------------------------------------- #
def new_session(prefix: str = "s") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def wait_import(client, headers, import_id: str, tenant: str | None = None,
                timeout: float = 300) -> dict:
    url = f"/api/v1/admin/imports/{import_id}"
    if tenant:
        url += f"?tenant={tenant}"
    deadline = time.time() + timeout
    while True:
        resp = client.get(url, headers=headers)
        assert resp.status_code == 200, resp.text
        status = resp.json()
        if status["status"] in ("done", "failed"):
            return status
        if time.time() > deadline:
            pytest.fail(f"import {import_id} did not finish in {timeout}s: {status}")
        time.sleep(0.5)


def wait_query(client, query_id: str, timeout: float = 60) -> dict:
    deadline = time.time() + timeout
    while True:
        resp = client.get(f"/api/v1/search/{query_id}")
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        if payload["status"] in ("done", "failed"):
            return payload
        if time.time() > deadline:
            pytest.fail(f"query {query_id} did not finish in {timeout}s "
                        f"(status={payload['status']}, degraded={payload['degraded']})")
        time.sleep(0.25)


def wait_job_done(sql, job_id: str, timeout: float = 60) -> dict:
    deadline = time.time() + timeout
    while True:
        row = sql.execute("SELECT status, result, last_error FROM jobs WHERE id=%s", (job_id,)).fetchone()
        if row and row["status"] in ("done", "failed"):
            return row
        if time.time() > deadline:
            pytest.fail(f"job {job_id} did not finish in {timeout}s")
        time.sleep(0.25)


def search(client, image_bytes: bytes, *, tenant: str | None = None, session_id: str | None = None,
           consent: str = "test-v1", filename: str = "photo.jpg", content_type: str = "image/jpeg"):
    data = {"session_id": session_id or new_session(), "consent_version": consent}
    if tenant:
        data["tenant"] = tenant
    return client.post("/api/v1/search",
                       files={"image": (filename, image_bytes, content_type)}, data=data)


def error_code(resp) -> str:
    try:
        return resp.json()["error"]["code"]
    except Exception:  # noqa: BLE001
        return f"<no-envelope:{resp.status_code}:{resp.text[:120]}>"


def make_jpeg(size: tuple[int, int] = (320, 320), color: tuple[int, int, int] = (190, 170, 150),
              seed: int = 0) -> bytes:
    img = Image.new("RGB", size, (240, 238, 232))
    draw = ImageDraw.Draw(img)
    offset = (seed * 37) % 80
    draw.ellipse([40 + offset, 60, 240 + offset, 260], fill=color)
    draw.line([(160 + offset, 0), (160 + offset, 60)], fill=(90, 90, 96), width=4)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def make_png(size: tuple[int, int] = (320, 320), color: tuple[int, int, int] = (120, 140, 190)) -> bytes:
    img = Image.new("RGB", size, (245, 243, 240))
    ImageDraw.Draw(img).ellipse([60, 60, 260, 260], fill=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def too_small_jpeg() -> bytes:
    return make_jpeg(size=(150, 150))


def corrupt_jpeg() -> bytes:
    return b"\xff\xd8\xff\xe0" + os.urandom(4096)


def heic_magic_bytes() -> bytes:
    # ftyp box advertising HEIC; body is garbage -> sniff says heic, decode must fail cleanly
    return b"\x00\x00\x00\x20ftypheic\x00\x00\x00\x00heicmif1" + os.urandom(2048)


def oversized_jpeg() -> bytes:
    return b"\xff\xd8\xff" + b"\x00" * (10 * 1024 * 1024 + 64)


def mock_image_bytes(settings, index: int) -> bytes:
    """The exact image bytes the mock catalog adapter serves for SKU index."""
    from app.adapters.mock_adapter import MockCatalogAdapter, sku_for
    from app.context import mock_image_dir

    adapter = MockCatalogAdapter(tenant_id="bytes-helper", image_dir=mock_image_dir(settings),
                                 public_base_url="http://unused", count=max(index + 2, 2))
    adapter.ensure_images()
    return adapter.fetch_image(f"{sku_for(index).lower()}.png")


def mock_product_attributes(settings, index: int) -> dict:
    from app.adapters.mock_adapter import MockCatalogAdapter
    from app.context import mock_image_dir

    adapter = MockCatalogAdapter(tenant_id="attr-helper", image_dir=mock_image_dir(settings),
                                 public_base_url="http://unused", count=max(index + 2, 2))
    return adapter._product(index)[0].attributes  # noqa: SLF001 - deterministic fixture reuse


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def ensure_test_db():
    try:
        conn = psycopg.connect(TEST_DB_URL, connect_timeout=5)
        conn.close()
        return TEST_DB_URL
    except psycopg.OperationalError:
        pass
    parts = urlsplit(TEST_DB_URL)
    dbname = parts.path.lstrip("/")
    admin_url = urlunsplit((parts.scheme, parts.netloc, "/postgres", parts.query, parts.fragment))
    admin = psycopg.connect(admin_url, autocommit=True, connect_timeout=5)
    try:
        exists = admin.execute("SELECT 1 FROM pg_database WHERE datname=%s", (dbname,)).fetchone()
        if not exists:
            admin.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        admin.close()
    return TEST_DB_URL


@pytest.fixture(scope="session")
def migrated(ensure_test_db):
    """Schema must exist before any DB-touching test, not only after the app boots."""
    from app.migrations import run_migrations

    run_migrations(TEST_DB_URL)
    # clean slate: leftovers from previous runs must never leak into assertions
    with psycopg.connect(TEST_DB_URL, autocommit=True) as conn:
        conn.execute("TRUNCATE TABLE tenants CASCADE")  # cascades all tenant-linked tables
        conn.execute("TRUNCATE TABLE jobs")
    return TEST_DB_URL


@pytest.fixture(scope="session")
def sql(migrated):
    conn = psycopg.connect(TEST_DB_URL, autocommit=True, row_factory=dict_row)
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def settings():
    from app.settings import get_settings, reset_settings_cache

    reset_settings_cache()
    return get_settings()


@pytest.fixture(scope="session")
def client(settings):
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def ctx(client):
    return client.app.state.ctx


@pytest.fixture(scope="session")
def admin_headers():
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture(scope="session")
def demo_tenant_id(client, sql, ctx):
    """Widen the demo tenant's rate limit BEFORE anything caches its config."""
    row = sql.execute("SELECT id::text AS id FROM tenants WHERE slug=%s", (DEMO_SLUG,)).fetchone()
    assert row, "demo tenant must exist after startup seeding"
    sql.execute("UPDATE tenants SET config = config || %s::jsonb WHERE id=%s",
                (Jsonb({"rate_limit": {"ip_per_min": 100000}}), row["id"]))
    ctx.invalidate_config(row["id"])
    return row["id"]


@pytest.fixture(scope="session")
def seeded(client, sql, admin_headers, demo_tenant_id, ctx):
    """Deterministic demo catalog: wipe + mock import of 80 SKUs (fresh every run)."""
    for table in CATALOG_TABLES:
        sql.execute(f"DELETE FROM {table} WHERE tenant_id=%s", (demo_tenant_id,))
    ctx.popular_cache.clear()
    resp = client.post("/api/v1/admin/imports", json={"adapter": "mock", "count": 80},
                       headers=admin_headers)
    assert resp.status_code == 202, resp.text
    status = wait_import(client, admin_headers, resp.json()["import_id"])
    assert status["status"] == "done", status
    # 80 rows: 4 without image (indices 0,23,46,69), 1 with 128px image (index 61) -> failed marker
    assert status["counts"] == {"total": 80, "imported": 79, "skipped": 0, "failed": 1}, status
    # 75 usable images; 3 planned byte-identical pairs reuse embeddings -> 72 new ones
    assert status["reembedded"] == 72 and status["skipped_unchanged"] == 3, status
    assert 0 < status["coverage_pct"] < 100, status
    return status


@pytest.fixture(autouse=True)
def clean_transient(sql):
    """Keep tests independent: transient (non-catalog) tables are wiped after each test."""
    yield
    for table in TRANSIENT_TABLES:
        sql.execute(f"DELETE FROM {table}")


@pytest.fixture
def make_tenant(sql, ctx):
    """Create a fresh tenant (unique slug per call) with optional config override."""
    created: list[str] = []

    def _make(config: dict | None = None, slug: str | None = None) -> str:
        slug = slug or f"t-{uuid.uuid4().hex[:8]}"
        row = sql.execute(
            """INSERT INTO tenants (slug, name, status, config) VALUES (%s, %s, 'active', %s)
               ON CONFLICT (slug) DO UPDATE SET config = EXCLUDED.config
               RETURNING id::text AS id""",
            (slug, slug, Jsonb(config or {})),
        ).fetchone()
        ctx.invalidate_config(row["id"])
        created.append(slug)
        return slug

    yield _make


@pytest.fixture
def set_tenant_config(sql, ctx):
    def _set(slug: str, config: dict) -> str:
        row = sql.execute("SELECT id::text AS id FROM tenants WHERE slug=%s", (slug,)).fetchone()
        assert row, f"unknown tenant {slug!r}"
        sql.execute("UPDATE tenants SET config = config || %s::jsonb WHERE id=%s",
                    (Jsonb(config), row["id"]))
        ctx.invalidate_config(row["id"])
        return row["id"]

    return _set


@pytest.fixture
def vision_hooks(ctx):
    """Mutate the mock vision provider's test hooks; restored afterwards (E6/E10)."""
    vision = ctx.vision
    snapshot = (vision.delay_s, vision.force_category, vision.force_confidence)
    yield vision
    vision.delay_s, vision.force_category, vision.force_confidence = snapshot
