"""Local CLIP provider: padding maths + wiring. No model download (fake embedder only)."""
from __future__ import annotations

import asyncio
import math

import pytest

from app.context import load_defaults
from app.providers import SCHEMA_VECTOR_DIM, build_embedding_provider
from app.providers.local_clip import (
    LOCAL_CLIP_DIM,
    LOCAL_CLIP_MODEL,
    LocalClipEmbeddingProvider,
    zero_pad,
)
from app.settings import Settings


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm


def test_zero_padding_preserves_cosine_exactly():
    a = [math.sin(i * 0.37) for i in range(LOCAL_CLIP_DIM)]
    b = [math.cos(i * 0.11) + 0.3 for i in range(LOCAL_CLIP_DIM)]
    padded_a = zero_pad(a, SCHEMA_VECTOR_DIM)
    padded_b = zero_pad(b, SCHEMA_VECTOR_DIM)
    assert len(padded_a) == len(padded_b) == SCHEMA_VECTOR_DIM
    assert padded_a[LOCAL_CLIP_DIM:] == [0.0] * (SCHEMA_VECTOR_DIM - LOCAL_CLIP_DIM)
    assert _cosine(a, b) == _cosine(padded_a, padded_b)


def test_zero_padding_rejects_oversized_vector():
    with pytest.raises(ValueError):
        zero_pad([1.0] * (SCHEMA_VECTOR_DIM + 1), SCHEMA_VECTOR_DIM)


def test_local_clip_provider_metadata_no_model_load(tmp_path):
    provider = LocalClipEmbeddingProvider(cache_dir=tmp_path, threads=2)
    assert provider.model == LOCAL_CLIP_MODEL
    assert provider.dim == LOCAL_CLIP_DIM == 512
    assert provider.pad_to == SCHEMA_VECTOR_DIM
    assert provider.est_cost_per_call == 0.0
    assert provider._model is None, "construction must not download/load the model"


def test_registry_selects_local_clip(tmp_path):
    s = Settings(
        app_mode="mock",
        embedding_provider="local_clip",
        local_clip_cache_dir=tmp_path,
        local_clip_threads=2,
    )
    provider = build_embedding_provider(s, load_defaults())
    assert isinstance(provider, LocalClipEmbeddingProvider)
    assert provider.model == LOCAL_CLIP_MODEL
    assert provider.dim == LOCAL_CLIP_DIM
    assert provider.pad_to == SCHEMA_VECTOR_DIM
    assert provider.cache_dir == tmp_path


def test_embed_image_pads_stub_vector(monkeypatch, tmp_path):
    provider = LocalClipEmbeddingProvider(cache_dir=tmp_path)
    monkeypatch.setattr(provider, "_embed_sync", lambda _: [0.1] * LOCAL_CLIP_DIM)

    vector = asyncio.run(provider.embed_image(b"not-decoded"))

    assert len(vector) == SCHEMA_VECTOR_DIM
    assert vector[:LOCAL_CLIP_DIM] == [0.1] * LOCAL_CLIP_DIM
    assert vector[LOCAL_CLIP_DIM:] == [0.0] * (SCHEMA_VECTOR_DIM - LOCAL_CLIP_DIM)


def test_embed_text_is_explicitly_not_implemented(tmp_path):
    provider = LocalClipEmbeddingProvider(cache_dir=tmp_path)
    with pytest.raises(NotImplementedError, match="image tower only"):
        asyncio.run(provider.embed_text("brass pendant lamp"))
