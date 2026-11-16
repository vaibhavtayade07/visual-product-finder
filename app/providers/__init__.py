"""Provider selection + key gating (architecture §6.1).

Rules:
  * mock mode (default, keyless): mock providers, always available.
  * prod mode: `auto` picks Vertex/Gemini when creds are present, otherwise fails
    loudly at boot — never silently degrades to mock embeddings (which would corrupt
    the index with vectors from a different space).
  * Titan is selectable but refused while the schema column is 1408-d: switching
    spaces is a re-embed migration (D4), not a config flip.
  * local_clip is an explicit opt-in (EMBEDDING_PROVIDER=local_clip): keyless local
    CLIP image embeddings, zero-padded 512 -> 1408 to fit the schema (see
    providers/local_clip.py). `auto` never selects it.
"""
from __future__ import annotations

import logging

from ..errors import ProviderConfigError
from ..settings import Settings
from .base import EmbeddingProvider, ProviderError, VisionProvider
from .local_clip import LOCAL_CLIP_MODEL, LocalClipEmbeddingProvider
from .mock import MOCK_EMBEDDING_MODEL, MockEmbeddingProvider, MockVisionProvider
from .titan import TITAN_DIM
from .vertex import VertexEmbeddingProvider

log = logging.getLogger(__name__)

SCHEMA_VECTOR_DIM = 1408  # image_embeddings.embedding vector(1408) — migration to change


def build_embedding_provider(settings: Settings, cfg: dict) -> EmbeddingProvider:
    choice = (settings.embedding_provider or "auto").lower()
    if choice == "auto":
        choice = "mock" if settings.is_mock else "vertex"

    if choice == "mock":
        dim = int(cfg.get("embedding", {}).get("dim", SCHEMA_VECTOR_DIM))
        return MockEmbeddingProvider(dim=dim)

    if choice == "local_clip":
        return LocalClipEmbeddingProvider(
            model=settings.local_clip_model or LOCAL_CLIP_MODEL,
            cache_dir=settings.local_clip_cache_dir,
            threads=settings.local_clip_threads,
            pad_to=SCHEMA_VECTOR_DIM,
        )

    if choice == "vertex":
        provider = VertexEmbeddingProvider(
            project_id=settings.gcp_project_id,
            sa_json=settings.gcp_sa_json,
            location=settings.vertex_location,
            timeout_s=float(cfg.get("embed", {}).get("timeout_s", 8)),
        )
        if provider.dim != SCHEMA_VECTOR_DIM:
            raise ProviderConfigError(
                f"model {provider.model} is {provider.dim}-d but schema is {SCHEMA_VECTOR_DIM}-d. "
                "Run the re-embed migration before switching (architecture §3 vector-dim note)."
            )
        return provider

    if choice == "titan":
        # Adapter exists (TitanEmbeddingProvider) but the schema is 1408-d today.
        raise ProviderConfigError(
            f"titan-mm-g1 is {TITAN_DIM}-d; the schema column is vector({SCHEMA_VECTOR_DIM}). "
            "Adopting Titan requires the one-time re-embed migration (retype column + re-embed "
            "catalog) described in architecture §3/§6.1 — it is not a per-query failover."
        )

    raise ProviderConfigError(f"unknown EMBEDDING_PROVIDER: {choice!r}")


def build_vision_provider(settings: Settings, cfg: dict) -> VisionProvider | None:
    """Returns None when RERANK_MODE=text_only (no LLM at all, §5b cost valve)."""
    if (settings.rerank_mode or "vision").lower() == "text_only":
        return None

    choice = (settings.vision_provider or "auto").lower()
    timeout_s = float(cfg.get("degrade", {}).get("llm_timeout_s", 20))

    if choice == "auto":
        if settings.gemini_api_key:
            choice = "gemini"
        elif settings.openai_api_key:
            choice = "openai"
        elif settings.is_mock:
            choice = "mock"
        else:
            raise ProviderConfigError(
                "prod mode needs a vision provider: set GEMINI_API_KEY or OPENAI_API_KEY "
                "(or VISION_PROVIDER=mock / RERANK_MODE=text_only)."
            )

    if choice == "mock":
        return MockVisionProvider(
            delay_s=settings.mock_llm_delay_s,
            force_category=settings.mock_query_category,
            force_confidence=settings.mock_query_confidence,
        )
    if choice == "gemini":
        from .gemini import GeminiVisionProvider  # noqa: PLC0415

        return GeminiVisionProvider(settings.gemini_api_key, settings.gemini_model, timeout_s)
    if choice == "openai":
        from .openai_llm import OpenAIVisionProvider  # noqa: PLC0415

        return OpenAIVisionProvider(settings.openai_api_key, settings.openai_model, timeout_s)
    raise ProviderConfigError(f"unknown VISION_PROVIDER: {choice!r}")


__all__ = [
    "SCHEMA_VECTOR_DIM",
    "EmbeddingProvider",
    "VisionProvider",
    "ProviderError",
    "build_embedding_provider",
    "build_vision_provider",
    "MOCK_EMBEDDING_MODEL",
    "LOCAL_CLIP_MODEL",
]
