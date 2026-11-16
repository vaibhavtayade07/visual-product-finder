"""OpenAI vision provider (fallback reranker, D5) via REST. Key-gated."""
from __future__ import annotations

import base64
import logging

import httpx

from ..errors import ProviderConfigError
from .base import ContentBlockedError, ProviderError, RerankCandidate, RerankResult
from .prompting import ENRICH_SYSTEM, RERANK_SYSTEM, extract_json, parse_rerank_response, rerank_user_text

log = logging.getLogger(__name__)

OPENAI_URL = "https://api.openai.com/v1/chat/completions"


class OpenAIVisionProvider:
    name = "openai"
    est_cost_per_call = 0.002

    def __init__(self, api_key: str, model: str = "gpt-5-mini", timeout_s: float = 20.0) -> None:
        if not api_key:
            raise ProviderConfigError("VISION_PROVIDER=openai requires OPENAI_API_KEY.")
        self.model = model
        self._key = api_key
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def _complete(self, content: list[dict]) -> str:
        try:
            resp = await self._client.post(
                OPENAI_URL,
                headers={"Authorization": f"Bearer {self._key}"},
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": content}],
                    "response_format": {"type": "json_object"},
                },
            )
        except httpx.TimeoutException as exc:
            raise ProviderError("openai timeout") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"openai transport error: {exc}") from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise ProviderError(f"openai {resp.status_code}: {resp.text[:300]}")
        if resp.status_code >= 400:
            body = resp.text.lower()
            if "content_policy" in body or "moderation" in body:
                raise ContentBlockedError("openai moderation flag")
            raise ProviderError(f"openai {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise ContentBlockedError("openai returned no choices")
        return choices[0]["message"]["content"]

    async def rerank(self, query_image: bytes, candidates: list[RerankCandidate]) -> RerankResult:
        content: list[dict] = [
            {"type": "text", "text": RERANK_SYSTEM},
            {"type": "text", "text": "Query photo:"},
            {"type": "image_url", "image_url": {"url": _data_uri(query_image)}},
            {"type": "text", "text": rerank_user_text(candidates)},
        ]
        for c in candidates:
            if c.thumbs:
                content.append({"type": "text", "text": f"Candidate image for SKU={c.sku}:"})
                content.append({"type": "image_url", "image_url": {"url": _data_uri(c.thumbs)}})
        text = await self._complete(content)
        query_attrs, matches = parse_rerank_response(text, candidates)
        return RerankResult(query_attributes=query_attrs, matches=matches, est_cost_usd=self.est_cost_per_call)

    async def enrich(self, product: dict, thumb: bytes | None) -> dict[str, str]:
        meta = {k: v for k, v in product.items() if k in ("sku", "title", "description", "category", "attributes")}
        content: list[dict] = [
            {"type": "text", "text": ENRICH_SYSTEM},
            {"type": "text", "text": f"Product: {meta}"},
        ]
        if thumb:
            content.append({"type": "image_url", "image_url": {"url": _data_uri(thumb)}})
        text = await self._complete(content)
        data = extract_json(text)
        return {k: str(v) for k, v in data.items() if isinstance(v, (str, int, float))}


def _data_uri(image_bytes: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode()
