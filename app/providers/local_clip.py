"""Keyless local CLIP image embeddings (fastembed / ONNX; no torch, no cloud).

Model: ``Qdrant/clip-ViT-B-32-vision`` (CLIP ViT-B/32 image tower, 512-d). The first
call downloads the ONNX model (~340 MB) into ``LOCAL_CLIP_CACHE_DIR``; afterwards this
runs CPU-only and fully offline. Warm single-image inference is tens of milliseconds.

Schema dimension shim
---------------------
``image_embeddings.embedding`` is ``vector(1408)`` with an HNSW cosine index, so this
provider zero-pads its 512-d vector to 1408 for **both** ingest and query. Cosine
similarity is preserved exactly: the appended dimensions contribute 0 to both the dot
product and the norms. No migration is needed and retrieval/scoring is unchanged.
``model`` is written per row, and the query pipeline filters on it, so local CLIP
vectors never mix with mock (1408-d) or Vertex vectors.

This shim bridges dimension only, not semantics: switching the catalog to Vertex (or
any other embedding space) still requires re-embedding every image.

``embed_text`` is intentionally not wired: the search pipeline is image->image and no
caller uses text embeddings. fastembed ships a matching CLIP text tower
(``Qdrant/clip-ViT-B-32-text``) if text queries are added later.
"""
from __future__ import annotations

import asyncio
import io
import threading
from pathlib import Path

from PIL import Image

from .base import ProviderError

LOCAL_CLIP_MODEL = "Qdrant/clip-ViT-B-32-vision"
LOCAL_CLIP_DIM = 512
SCHEMA_DIM = 1408  # image_embeddings.embedding vector(1408) — passed by the factory


def zero_pad(vector: list[float], size: int) -> list[float]:
    """Append zeros up to ``size``. Exact for cosine (extra dims add 0 to dot and norm)."""
    if len(vector) > size:
        raise ValueError(f"embedding has {len(vector)} dims, cannot pad down to {size}")
    return list(vector) + [0.0] * (size - len(vector))


class LocalClipEmbeddingProvider:
    """CLIP ViT-B/32 image embeddings via fastembed, lazily loaded on first use."""

    est_cost_per_call = 0.0

    def __init__(
        self,
        model: str = LOCAL_CLIP_MODEL,
        cache_dir: str | Path | None = None,
        threads: int | None = None,
        pad_to: int = SCHEMA_DIM,
    ) -> None:
        self.model = model
        self.dim = LOCAL_CLIP_DIM        # actual model dim (schema column is pad_to)
        self.pad_to = pad_to
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.threads = threads
        self._model = None
        self._load_lock = threading.Lock()

    def _get_model(self):
        """Load (and on first ever call, download) the ONNX model exactly once."""
        if self._model is None:
            with self._load_lock:
                if self._model is None:
                    from fastembed import ImageEmbedding  # noqa: PLC0415 - keeps import cost lazy

                    kwargs: dict = {"model_name": self.model}
                    if self.cache_dir is not None:
                        kwargs["cache_dir"] = str(self.cache_dir)
                    if self.threads:
                        kwargs["threads"] = self.threads
                    self._model = ImageEmbedding(**kwargs)
        return self._model

    def _embed_sync(self, image_bytes: bytes) -> list[float]:
        with Image.open(io.BytesIO(image_bytes)) as img:
            rgb = img.convert("RGB")
            vector = next(iter(self._get_model().embed([rgb], batch_size=1)))
        raw = [float(v) for v in vector]
        if len(raw) != self.dim:
            raise ProviderError(
                f"local model {self.model!r} returned {len(raw)}-d vectors, expected {self.dim}. "
                "Only CLIP ViT-B/32 (512-d) is supported by this provider."
            )
        return raw

    async def embed_image(self, image_bytes: bytes) -> list[float]:
        # Model load and ONNX inference are CPU-bound: keep the event loop free.
        raw = await asyncio.to_thread(self._embed_sync, image_bytes)
        return zero_pad(raw, self.pad_to)

    async def embed_text(self, text: str) -> list[float]:
        raise NotImplementedError(
            "local_clip wires the image tower only; the search pipeline never embeds text."
        )
