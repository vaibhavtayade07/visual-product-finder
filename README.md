# Visual Product Finder

**"Show us what you want" — photo in, ranked product matches out.**

<p>
<img alt="Python" src="https://img.shields.io/badge/Python-3776AB?logo=python&logoColor=white">
<img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white">
<img alt="pgvector" src="https://img.shields.io/badge/Postgres-pgvector-336791?logo=postgresql&logoColor=white">
<img alt="Docker" src="https://img.shields.io/badge/Docker-compose-2496ED?logo=docker&logoColor=white">
<img alt="License" src="https://img.shields.io/badge/License-MIT-green">
</p>

Visual product search MVP for a lighting store: shoppers upload a photo, the pipeline
embeds it, retrieves candidates from Postgres + pgvector, optionally reranks with a
vision LLM, and renders ranked match cards with reasons. Runs fully offline in **mock
mode** (no API keys required).

## Quickstart (mock mode)

Prereqs: Docker Desktop running, [`uv`](https://docs.astral.sh/uv/) installed.

```powershell
# 1. Database (host port 5433 — 5432 is left to other local stacks)
docker compose up -d db

# 2. Python deps
uv sync --extra dev

# 3. Migrations (also applied automatically at app startup)
uv run alembic upgrade head

# 4. Seed the mock lighting catalog (~300 SKUs with generated fixture images)
uv run python scripts/seed_mock.py --count 300 --reset

# 5. Run the server (works on Windows: pins a psycopg-compatible event loop;
#    plain `uvicorn` uses Windows' Proactor loop, which psycopg's async rejects)
uv run python scripts/serve.py --port 8000

# 6. Open the widget and the admin page
#    http://127.0.0.1:8000/find    photo search (consent, upload, results, refine)
#    http://127.0.0.1:8000/admin   catalog imports, hygiene, reindex, tenant config

# 7. Tests (creates/uses the finder_test database; mock mode, no keys)
uv run pytest

# 8. Eval harness (correctness gate; mock labels ship for smoke-testing)
uv run python eval/run_eval.py --labels eval/labels.mock.json
```

## Configuration

- `config/defaults.yaml` — scoring thresholds/weights, top-K, degrade timeout, retention,
  cost cap, rate limits. Per-tenant overrides via `PUT /api/v1/admin/config` (merged
  under defaults).
- `.env` (see `.env.example`) — `APP_MODE=mock|real`, `DATABASE_URL`, `DATA_DIR`,
  `ADMIN_TOKEN` (default `dev-admin-token`), provider keys for `APP_MODE=real`.
- Real providers are key-gated: Vertex / Gemini / OpenAI activate when their keys are
  present; the schema is pinned to 1408-dim embeddings (Titan MM G1's 1024 dims are
  rejected by design — see `docs/ARCHITECTURE.md`).

## Shopify catalog import (optional)

Mock and CSV remain the default; the Shopify adapter only runs when an import or a
scheduled sync explicitly selects it (tenant config `adapters.catalog = "shopify"`).
To index a live store:

1. In Shopify admin, create a custom app with Admin API scopes `read_products` and
   `read_inventory`, install it, and copy the Admin API access token.
2. Set `SHOPIFY_SHOP_DOMAIN` (e.g. `your-store.myshopify.com`),
   `SHOPIFY_ADMIN_TOKEN`, and optionally `SHOPIFY_API_VERSION` (default `2026-07`) in
   `.env`.
3. Trigger a full import either from `/admin` ("Import from Shopify") or with
   `POST /api/v1/admin/imports` + bearer admin token and body `{"adapter": "shopify"}`.

The adapter reads active products through the Admin GraphQL API (250 per page), indexes
one row per product using the cheapest in-stock variant for SKU/price, and aggregates
inventory across available variants. Incremental runs use
`products(query: "status:active AND updated_at:>=...")` with a 5-minute overlap. Product
images are public Shopify CDN URLs, so image downloads send no auth header. Missing
credentials return a 400 that names the env vars to set; no live store is needed for the
offline test suite.

Scheduled syncs: `nightly_batch` runs a full reconcile (products missing from the
snapshot are discontinued), while `hourly_delta` fetches only rows updated since the
tenant's last successful sync. A partial delta never discontinues products missing from
it — that only happens on a full snapshot (CSV/mock `delta_is_full` adapters and the
nightly run).

## Layout

- `app/api/` public search, admin, health routes
- `app/services/` ingest, query pipeline, retention/hygiene, popular, images
- `app/providers/` embedding + vision providers (mock and cloud)
- `app/adapters/` catalog adapters (mock generator, CSV, Shopify Admin GraphQL)
- `app/workers/` Postgres-backed job queue, worker pool, scheduler
- `eval/` top-1/top-3 correctness harness (`labels.mock.json` for offline smoke runs)
- `docs/` spec and architecture
