"""Gemini vision provider (primary reranker, D5) via REST. Key-gated."""
from __future__ import annotations

import base64
import logging

import httpx

from ..errors import ProviderConfigError
from .base import ContentBlockedError, ProviderError, RerankCandidate, RerankResult
from .prompting import RERANK_SYSTEM, extract_json, parse_rerank_response, rerank_user_text

log = logging.getLogger(__name__)

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiVisionProvider:
    name = "gemini"
    est_cost_per_call = 0.002  # ~$0.002/query §2 cost estimate

    def __init__(self, api_key: str, model: str = "gemini-3-flash", timeout_s: float = 20.0) -> None:
        if not api_key:
            raise ProviderConfigError("VISION_PROVIDER=gemini requires GEMINI_API_KEY.")
        self.model = model
        self._key = api_key
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def _generate(self, parts: list[dict]) -> str:
        url = f"{GEMINI_BASE}/{self.model}:generateContent"
        try:
            resp = await self._client.post(
                url,
                params={"key": self._key},
                json={
                    "contents": [{"parts": parts}],
                    "generationConfig": {"response_mime_type": "application/json", "temperature": 0.2},
                },
            )
        except httpx.TimeoutException as exc:
            raise ProviderError("gemini timeout") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"gemini transport error: {exc}") from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise ProviderError(f"gemini {resp.status_code}: {resp.text[:300]}")
        if resp.status_code >= 400:
            raise ProviderError(f"gemini {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        feedback = data.get("promptFeedback") or {}
        if feedback.get("blockReason"):
            raise ContentBlockedError(f"gemini blocked: {feedback.get('blockReason')}")
        candidates = data.get("candidates") or []
        if not candidates:
            raise ContentBlockedError("gemini returned no candidates (possible safety block)")
        parts_out = candidates[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts_out)

    async def rerank(self, query_image: bytes, candidates: list[RerankCandidate]) -> RerankResult:
        parts: list[dict] = [
            {"text": RERANK_SYSTEM},
            {"text": "Query photo:"},
            {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(query_image).decode()}},
            {"text": rerank_user_text(candidates)},
        ]
        for c in candidates:
            if c.thumbs:
                parts.append({"text": f"Candidate image for SKU={c.sku}:"})
                parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(c.thumbs).decode()}})
        text = await self._generate(parts)
        query_attrs, matches = parse_rerank_response(text, candidates)
        return RerankResult(query_attributes=query_attrs, matches=matches, est_cost_usd=self.est_cost_per_call)

    async def enrich(self, product: dict, thumb: bytes | None) -> dict[str, str]:
        from .prompting import ENRICH_SYSTEM  # noqa: PLC0415

        meta = {k: v for k, v in product.items() if k in ("sku", "title", "description", "category", "attributes")}
        parts: list[dict] = [{"text": ENRICH_SYSTEM}, {"text": f"Product: {meta}"}]
        if thumb:
            parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(thumb).decode()}})
        text = await self._generate(parts)
        data = extract_json(text)
        return {k: str(v) for k, v in data.items() if isinstance(v, (str, int, float))}
