"""Durable object storage for released dataset artifacts.

On platforms with ephemeral filesystems (Vercel functions write only to /tmp)
export batches must land somewhere durable. Configure any S3-compatible store:

    HDF_EXPORT_S3_BUCKET    bucket name (enables the backend)
    HDF_EXPORT_S3_ENDPOINT  optional endpoint URL (Cloudflare R2, MinIO, ...)
    HDF_EXPORT_S3_PREFIX    optional key prefix (default "exports")

Credentials resolve through the standard AWS chain (env vars, profile, role).
When no bucket is configured, exports stay local-only, as before.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol


class ExportStorage(Protocol):
    def upload_batch(self, local_dir: Path, batch_id: str,
                     only: set[str] | None = None) -> dict:
        """Upload files in `local_dir` (optionally restricted to `only` names);
        returns a manifest `storage` block."""
        ...


class S3ExportStorage:
    def __init__(self, bucket: str, endpoint_url: str | None = None,
                 prefix: str = "exports") -> None:
        import boto3  # lazy: only needed when a bucket is configured

        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.endpoint_url = endpoint_url or None
        self._client = boto3.client("s3", endpoint_url=self.endpoint_url)

    def upload_batch(self, local_dir: Path, batch_id: str,
                     only: set[str] | None = None) -> dict:
        keys: dict[str, str] = {}
        for path in sorted(local_dir.iterdir()):
            if not path.is_file() or (only is not None and path.name not in only):
                continue
            key = f"{self.prefix}/{batch_id}/{path.name}"
            self._client.upload_file(str(path), self.bucket, key)
            keys[path.name] = key
        return {
            "backend": "s3",
            "bucket": self.bucket,
            "endpoint": self.endpoint_url,
            "keys": keys,
        }


def get_storage() -> ExportStorage | None:
    bucket = os.environ.get("HDF_EXPORT_S3_BUCKET", "")
    if not bucket:
        return None
    return S3ExportStorage(
        bucket=bucket,
        endpoint_url=os.environ.get("HDF_EXPORT_S3_ENDPOINT", ""),
        prefix=os.environ.get("HDF_EXPORT_S3_PREFIX", "exports"),
    )
