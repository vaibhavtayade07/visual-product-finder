"""Object storage behind one interface (architecture §2/D8).

LocalStorage is the keyless default (query photos, catalog thumbnails, mock images).
R2Storage is the Cloudflare R2 (S3 API) implementation used in production; boto3 is an
optional extra (`uv sync --extra cloud`) imported lazily so mock mode needs no keys.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from .errors import ProviderConfigError

log = logging.getLogger(__name__)


class StorageProvider(Protocol):
    def put(self, key: str, data: bytes, content_type: str = "image/jpeg") -> str: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> bool: ...
    def url(self, key: str) -> str: ...


class LocalStorage:
    """Files under DATA_DIR/storage, served read-only at /files/{key}."""

    backend = "local"

    def __init__(self, root: Path, public_base_url: str) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.public_base_url = public_base_url.rstrip("/")

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise ValueError(f"unsafe storage key: {key!r}")
        return p

    def put(self, key: str, data: bytes, content_type: str = "image/jpeg") -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> bool:
        path = self._path(key)
        if path.exists():
            path.unlink()
            return True
        return False

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def url(self, key: str) -> str:
        return f"{self.public_base_url}/files/{key}"


class R2Storage:
    """Cloudflare R2 via S3 API. Requires R2_* env vars + `boto3` (extra: cloud)."""

    backend = "r2"

    def __init__(self, account_id: str, access_key_id: str, secret_access_key: str,
                 bucket: str, public_base_url: str) -> None:
        if not all((account_id, access_key_id, secret_access_key, bucket)):
            raise ProviderConfigError(
                "STORAGE_BACKEND=r2 requires R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
                "R2_SECRET_ACCESS_KEY and R2_BUCKET."
            )
        try:
            import boto3  # noqa: PLC0415  (optional extra; lazy on purpose)
        except ImportError as exc:  # pragma: no cover - exercised only without extra
            raise ProviderConfigError(
                "R2 storage requires boto3. Install with: uv sync --extra cloud"
            ) from exc
        self.bucket = bucket
        self.public_base_url = public_base_url.rstrip("/")
        self._s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
        )

    def put(self, key: str, data: bytes, content_type: str = "image/jpeg") -> str:
        self._s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        return key

    def get(self, key: str) -> bytes:
        obj = self._s3.get_object(Bucket=self.bucket, Key=key)
        return obj["Body"].read()

    def delete(self, key: str) -> bool:
        self._s3.delete_object(Bucket=self.bucket, Key=key)
        return True

    def exists(self, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    def url(self, key: str) -> str:
        base = self.public_base_url or f"https://{self.bucket}.r2.dev"
        return f"{base}/{key}"
