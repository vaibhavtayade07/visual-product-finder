"""Seed the mock catalog directly into the database (no running server needed).

Usage (after `docker compose up -d db`):
    uv run python scripts/seed_mock.py --count 300
    uv run python scripts/seed_mock.py --count 80 --reset

Runs migrations, creates the tenant if missing, optional --reset wipes the tenant's
catalog/query rows, then runs the real ingest pipeline (`handle_ingest`) exactly like
the worker pool would — same code path as admin imports.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.context import AppContext, build_storage, load_defaults  # noqa: E402
from app.db import Database  # noqa: E402
from app.migrations import run_migrations  # noqa: E402
from app.providers import build_embedding_provider, build_vision_provider  # noqa: E402
from app.runtime import ensure_selector_loop_on_windows  # noqa: E402
from app.services.ingest import handle_ingest  # noqa: E402
from app.settings import get_settings  # noqa: E402

RESET_TABLES = [
    "clicks", "leads", "consents", "provider_calls", "hygiene_findings", "jobs",
    "search_queries", "catalog_imports", "product_tags", "product_images",
    "image_embeddings", "products", "usage_counters",
]


async def seed(count: int, reset: bool, tenant_slug: str) -> dict:
    settings = get_settings()
    defaults = load_defaults()

    await asyncio.to_thread(run_migrations, settings.database_url)

    db = Database(settings.database_url)
    await db.open()
    try:
        ctx = AppContext(
            settings=settings,
            db=db,
            storage=build_storage(settings),
            embed=build_embedding_provider(settings, defaults),
            vision=None,
            query_queue=asyncio.Queue(),
            defaults=defaults,
        )
        ctx.vision = build_vision_provider(settings, defaults)

        await db.execute(
            """INSERT INTO tenants (slug, name, status, config) VALUES (%s, %s, 'active', '{}'::jsonb)
               ON CONFLICT (slug) DO NOTHING""",
            (tenant_slug, "Demo Lighting Store"),
        )
        tenant = await db.fetch_one("SELECT id::text AS id FROM tenants WHERE slug=%s", (tenant_slug,))
        tenant_id = tenant["id"]

        if reset:
            for table in RESET_TABLES:
                await db.execute(f"DELETE FROM {table} WHERE tenant_id=%s", (tenant_id,))
            print(f"reset: wiped {len(RESET_TABLES)} tables for tenant {tenant_slug!r}")

        import_row = await db.execute_returning(
            """INSERT INTO catalog_imports (tenant_id, kind, filename, status, total_rows,
                                            imported, skipped, failed, validation_report, stats,
                                            created_by, started_at)
               VALUES (%s, 'mock', %s, 'validating', %s, 0, 0, 0, '[]', '{}', 'seed-script', now())
               RETURNING id::text AS id""",
            (tenant_id, f"mock catalog x{count} (script)", count),
        )
        result = await handle_ingest(ctx, {"payload": {
            "tenant_id": tenant_id, "import_id": import_row["id"], "adapter": "mock", "count": count,
        }})
        counts = await db.fetch_one(
            "SELECT imported, skipped, failed FROM catalog_imports WHERE id=%s", (import_row["id"],))
        return {"tenant": tenant_slug, "import_id": import_row["id"], **(counts or {}), **result}
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the mock catalog into the database.")
    parser.add_argument("--count", type=int, default=300, help="number of mock SKUs (default 300)")
    parser.add_argument("--reset", action="store_true", help="wipe the tenant's catalog/query rows first")
    parser.add_argument("--tenant", default=None, help="tenant slug (default: DEFAULT_TENANT_SLUG)")
    args = parser.parse_args()

    ensure_selector_loop_on_windows()
    settings = get_settings()
    summary = asyncio.run(seed(args.count, args.reset, args.tenant or settings.default_tenant_slug))
    print(json.dumps(summary, indent=2, default=str))
    print(f"seeded {summary.get('imported', 0)} SKUs "
          f"(re-embedded {summary.get('reembedded', 0)}, unchanged {summary.get('skipped_unchanged', 0)}, "
          f"failed {summary.get('failed', 0)}) - coverage {summary.get('coverage_pct')}%")


if __name__ == "__main__":
    main()
