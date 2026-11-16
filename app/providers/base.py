"""Provider interfaces: EmbeddingProvider + VisionProvider (architecture §2, §6).

Vector spaces are model-specific (D4): never mix models within one index. The
`model` name is written per embedding row and used as a retrieval filter.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class ProviderError(Exception):
    """Transient provider failure (timeout / 5xx). Retryable per §6.1."""


class ContentBlockedError(Exception):
    """Provider moderation flag on the query image (E14). No partial results."""


@dataclass
class RerankCandidate:
    sku: str
    title: str
    thumbs: bytes | None
    attributes: dict = field(default_factory=dict)
    price: float | None = None
    stock_status: str | None = None


@dataclass
class RerankResult:
    query_attributes: dict
    matches: dict[str, dict]  # sku -> {"attribute_match": float, "reason": str}
    est_cost_usd: float = 0.0


class EmbeddingProvider(Protocol):
    model: str          # canonical model name written to image_embeddings.model
    dim: int
    est_cost_per_call: float

    async def embed_image(self, image_bytes: bytes) -> list[float]: ...
    async def embed_text(self, text: str) -> list[float]: ...


class VisionProvider(Protocol):
    name: str           # 'mock' | 'gemini' | 'openai'
    model: str
    est_cost_per_call: float

    async def rerank(self, query_image: bytes, candidates: list[RerankCandidate]) -> RerankResult: ...
    async def enrich(self, product: dict, thumb: bytes | None) -> dict[str, str]: ...
