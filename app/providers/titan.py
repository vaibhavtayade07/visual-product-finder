"""Amazon Titan Multimodal Embeddings G1 (documented embedding fallback, D4).

1024-d — INCOMPATIBLE with the 1408-d vector column (architecture §3 vector-dim note).
The adapter is complete; enabling it requires the one-time re-embed migration that
retypes the vector column / index, so the registry refuses it while the schema is
1408-d. This is intentional: per-query provider failover would silently mix vector
spaces (D4, §6.1).
"""
from __future__ import annotations

import base64
import json
import logging

from ..errors import ProviderConfigError
from .base import ContentBlockedError, ProviderError

log = logging.getLogger(__name__)

TITAN_MODEL = "titan-mm-g1"
TITAN_DIM = 1024
TITAN_BEDROCK_MODEL_ID = "amazon.titan-embed-image-v1"


class TitanEmbeddingProvider:
    model = TITAN_MODEL
    dim = TITAN_DIM
    est_cost_per_call = 0.00007

    def __init__(self, region: str = "us-east-1") -> None:
        try:
            import boto3  # noqa: PLC0415  (optional extra; lazy on purpose)
        except ImportError as exc:
            raise ProviderConfigError(
                "Titan provider requires boto3. Install with: uv sync --extra cloud"
            ) from exc
        self._client = boto3.client("bedrock-runtime", region_name=region)

    async def _invoke(self, body: dict) -> list[float]:
        def _call() -> list[float]:
            resp = self._client.invoke_model(modelId=TITAN_BEDROCK_MODEL_ID, body=json.dumps(body))
            payload = json.loads(resp["body"].read())
            embedding = payload.get("embedding")
            if not embedding:
                raise ProviderError("titan returned no embedding")
            return list(embedding)

        import asyncio  # noqa: PLC0415

        try:
            return await asyncio.get_running_loop().run_in_executor(None, _call)
        except ProviderError:
            raise
        except Exception as exc:  # boto3 raises broad exception classes
            text = str(exc).lower()
            if "blocked" in text or "moderation" in text:
                raise ContentBlockedError("titan moderation flag") from exc
            raise ProviderError(f"titan embedding failed: {exc}") from exc

    async def embed_image(self, image_bytes: bytes) -> list[float]:
        return await self._invoke({"inputImage": base64.b64encode(image_bytes).decode()})

    async def embed_text(self, text: str) -> list[float]:
        return await self._invoke({"inputText": text})
