"""Eval harness (AC7.1): runs labeled query images through embed -> retrieval -> scoring
and records top-1/top-3 hit rates into `eval_runs`.

Labels file (JSON list, >= 50 entries recommended):

    [
      {"image": "path/to/photo.jpg", "expected_skus": ["LMP-1004", "LMP-1015"], "notes": "floor lamp"},
      ...
    ]

This is the correctness gate only — no tuning loop in the MVP (architecture §8).

Usage:
    uv run python eval/run_eval.py --labels eval/labels.json
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
from app.providers import build_embedding_provider, build_vision_provider  # noqa: E402
from app.runtime import ensure_selector_loop_on_windows  # noqa: E402
from app.services.images import load_image, normalize_jpeg  # noqa: E402
from app.services.query_pipeline import TOP_K_SQL, _vector_literal  # noqa: E402
from app.settings import get_settings  # noqa: E402

DEFAULT_LABELS = Path(__file__).resolve().parent / "labels.json"


def load_labels(path: Path) -> list[dict]:
    if not path.exists():
        print(f"labels file not found: {path}\nCreate it (see docstring) — eval tunes nothing, it gates.")
        raise SystemExit(1)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        print(f"labels file must be a non-empty JSON list, got: {type(data).__name__}")
        raise SystemExit(1)
    return data


async def run(labels_path: Path, tenant_slug: str, top_k: int) -> int:
    labels = load_labels(labels_path)
    settings = get_settings()
    defaults = load_defaults()
    db = Database(settings.database_url)
    await db.open()
    try:
        ctx = AppContext(settings=settings, db=db, storage=build_storage(settings),
                         embed=build_embedding_provider(settings, defaults), vision=None,
                         query_queue=asyncio.Queue(), defaults=defaults)
        ctx.vision = build_vision_provider(settings, defaults)
        tenant = await db.fetch_one("SELECT id::text AS id FROM tenants WHERE slug=%s", (tenant_slug,))
        if not tenant:
            print(f"unknown tenant: {tenant_slug!r}")
            return 1
        tenant_id = tenant["id"]

        hits1 = hits3 = 0
        per_query: list[dict] = []
        for entry in labels:
            image_path = Path(entry["image"])
            if not image_path.exists():
                per_query.append({"image": str(image_path), "status": "missing_file"})
                continue
            img = load_image(image_path.read_bytes())
            embedding = await ctx.embed.embed_image(normalize_jpeg(img))
            rows = await db.fetch_all(TOP_K_SQL, {
                "vec": _vector_literal(embedding), "tenant": tenant_id,
                "model": ctx.embed.model, "k": top_k,
            })
            ranked = []
            for row in rows:  # product-level dedupe, same as the query pipeline
                if row["sku"] not in ranked:
                    ranked.append(row["sku"])
            expected = set(entry.get("expected_skus") or [])
            rank = next((i for i, sku in enumerate(ranked, start=1) if sku in expected), None)
            hit1 = rank == 1
            hit3 = rank is not None and rank <= 3
            hits1 += hit1
            hits3 += hit3
            per_query.append({"image": image_path.name, "rank": rank, "top1": ranked[0] if ranked else None})

        n = len(labels)
        metrics = {
            "n": n,
            "hit_at_1": hits1,
            "hit_at_3": hits3,
            "hit_at_1_pct": round(100.0 * hits1 / n, 1) if n else 0.0,
            "hit_at_3_pct": round(100.0 * hits3 / n, 1) if n else 0.0,
            "top_k": top_k,
            "model": ctx.embed.model,
            "per_query": per_query,
        }
        await db.execute(
            """INSERT INTO eval_runs (tenant_id, config_snapshot, metrics, created_at)
               VALUES (%s, %s, %s, now())""",
            (tenant_id, _jsonb(defaults), _jsonb({k: v for k, v in metrics.items() if k != "per_query"})),
        )
        print(json.dumps({k: v for k, v in metrics.items() if k != "per_query"}, indent=2))
        misses = [q for q in per_query if not (q.get("rank") and q["rank"] <= 3)]
        if misses:
            print(f"\n{len(misses)} miss(es) outside top-3 (first 10):")
            for miss in misses[:10]:
                print(f"  {miss}")
        return 0
    finally:
        await db.close()


def _jsonb(value: dict):
    from psycopg.types.json import Jsonb

    return Jsonb(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Eval harness: top-1/top-3 hit rates.")
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    parser.add_argument("--tenant", default=None)
    parser.add_argument("--top-k", type=int, default=50)
    args = parser.parse_args()
    tenant = args.tenant or get_settings().default_tenant_slug
    ensure_selector_loop_on_windows()
    raise SystemExit(asyncio.run(run(args.labels, tenant, args.top_k)))


if __name__ == "__main__":
    main()
