"""Sanity-check the local CLIP provider (real model, real images).

Not part of pytest: the first run downloads the ONNX model (~340 MB) and later runs
exercise CPU inference. Use it to verify the demo machine before seeding.

    uv run python scripts/check_local_clip.py
    uv run python scripts/check_local_clip.py path/a.png path/b.png

Checks: output dims, model cache, first-call (load) vs warm latency, and that zero
padding to the schema's 1408 dims preserves cosine exactly.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.providers.local_clip import LOCAL_CLIP_DIM, LocalClipEmbeddingProvider  # noqa: E402
from app.settings import get_settings  # noqa: E402

DEFAULT_IMAGES = ["data/mock_images/lmp-1001.png", "data/mock_images/lmp-1002.png"]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _dir_size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) / (1024 * 1024)


async def main(images: list[Path]) -> int:
    settings = get_settings()
    provider = LocalClipEmbeddingProvider(
        model=settings.local_clip_model,
        cache_dir=settings.local_clip_cache_dir,
        threads=settings.local_clip_threads,
    )
    print(f"model={provider.model} dim={provider.dim} pad_to={provider.pad_to}")
    print(f"cache_dir={provider.cache_dir} (current size {_dir_size_mb(provider.cache_dir):.1f} MB)")

    vectors: list[list[float]] = []
    for i, path in enumerate(images):
        data = path.read_bytes()
        t0 = time.perf_counter()
        vector = await provider.embed_image(data)
        elapsed = time.perf_counter() - t0
        label = "first (includes model load)" if i == 0 else "warm-ish"
        print(f"{path.name}: {label} {elapsed * 1000:.0f} ms, len={len(vector)}")
        assert len(vector) == provider.pad_to
        assert vector[LOCAL_CLIP_DIM:] == [0.0] * (provider.pad_to - LOCAL_CLIP_DIM)
        vectors.append(vector)

    warm = []
    for _ in range(3):
        t0 = time.perf_counter()
        await provider.embed_image(images[0].read_bytes())
        warm.append((time.perf_counter() - t0) * 1000)
    print(f"warm single-image ms: min={min(warm):.0f} all={[round(v) for v in warm]}")
    print(f"cache size after load: {_dir_size_mb(provider.cache_dir):.1f} MB")

    if len(vectors) > 1:
        for i in range(1, len(vectors)):
            padded = _cosine(vectors[0], vectors[i])
            raw = _cosine(vectors[0][:LOCAL_CLIP_DIM], vectors[i][:LOCAL_CLIP_DIM])
            delta = abs(padded - raw)
            print(f"cos({images[0].name}, {images[i].name}): padded={padded:.6f} raw={raw:.6f} "
                  f"delta={delta:.2e}")
            assert delta < 1e-12, "zero-padding must preserve cosine exactly"

    # Model output must already be unit norm (fastembed normalizes CLIP vectors).
    for vector in vectors:
        norm = math.sqrt(sum(x * x for x in vector))
        print(f"padded norm={norm:.6f}")
        assert abs(norm - 1.0) < 1e-3
    print("OK")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check local CLIP embeddings end to end.")
    parser.add_argument("images", nargs="*", help="image paths (default: two mock catalog images)")
    args = parser.parse_args()
    paths = [Path(p) for p in (args.images or DEFAULT_IMAGES)]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        print(f"missing images: {missing} (run scripts/seed_mock.py first)", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(paths)))
