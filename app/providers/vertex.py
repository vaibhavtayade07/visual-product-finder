"""Vertex AI multimodalembedding@001 (primary embedding, D4). Key-gated."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from pathlib import Path

import httpx

from ..errors import ProviderConfigError
from .base import ContentBlockedError, ProviderError

log = logging.getLogger(__name__)

VERTEX_MODEL = "multimodalembedding@001"
VERTEX_DIM = 1408


class VertexEmbeddingProvider:
    model = VERTEX_MODEL
    dim = VERTEX_DIM
    est_cost_per_call = 0.0001  # ≈$0.0001/image, verify at build (architecture §1)

    def __init__(self, project_id: str, sa_json: str, location: str, timeout_s: float = 8.0) -> None:
        if not project_id:
            raise ProviderConfigError("EMBEDDING_PROVIDER=vertex requires GCP_PROJECT_ID.")
        if not sa_json:
            raise ProviderConfigError("EMBEDDING_PROVIDER=vertex requires GCP_SA_JSON (path or raw JSON).")
        try:
            from google.auth.transport.requests import Request  # noqa: PLC0415
            from google.oauth2 import service_account  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise ProviderConfigError(
                "Vertex provider requires google-auth. Install with: uv sync --extra cloud"
            ) from exc
        try:
            if Path(sa_json).exists():
                creds = service_account.Credentials.from_service_account_file(sa_json)
            else:
                creds = service_account.Credentials.from_service_account_info(json.loads(sa_json))
        except Exception as exc:
            raise ProviderConfigError(f"GCP_SA_JSON is not a usable service-account (path or JSON): {exc}") from exc
        self._creds = creds.with_scopes(["https://www.googleapis.com/auth/cloud-platform"])
        self._request_cls = Request
        self._token: str | None = None
        self._token_expiry = 0.0
        self._timeout = timeout_s
        self._endpoint = (
            f"https://{location}-aiplatform.googleapis.com/v1/projects/{project_id}"
            f"/locations/{location}/publishers/google/models/{VERTEX_MODEL}:predict"
        )
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._creds.refresh, self._request_cls())
        self._token = self._creds.token
        expiry = getattr(self._creds, "expiry", None)
        self._token_expiry = expiry.timestamp() if expiry else time.time() + 3000
        return self._token

    async def _predict(self, instances: list[dict]) -> list[dict]:
        token = await self._access_token()
        try:
            resp = await self._client.post(
                self._endpoint,
                json={"instances": instances},
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.TimeoutException as exc:
            raise ProviderError("vertex embedding timeout") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"vertex embedding transport error: {exc}") from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise ProviderError(f"vertex embedding {resp.status_code}: {resp.text[:300]}")
        if resp.status_code >= 400:
            body = resp.text.lower()
            if "safety" in body or "blocked" in body:
                raise ContentBlockedError("vertex moderation flag")
            raise ProviderError(f"vertex embedding {resp.status_code}: {resp.text[:300]}")
        return resp.json().get("predictions", [])

    async def embed_image(self, image_bytes: bytes) -> list[float]:
        preds = await self._predict([{"image": {"bytesBase64Encoded": base64.b64encode(image_bytes).decode()}}])
        if not preds:
            raise ProviderError("vertex returned no predictions")
        return list(preds[0]["imageEmbedding"])

    async def embed_text(self, text: str) -> list[float]:
        preds = await self._predict([{"text": text}])
        if not preds:
            raise ProviderError("vertex returned no predictions")
        return list(preds[0]["textEmbedding"])
