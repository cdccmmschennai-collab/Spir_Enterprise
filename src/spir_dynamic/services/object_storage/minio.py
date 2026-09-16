"""
MinioObjectStorage — the storage contract over one S3-compatible bucket.

Talks to MinIO (or any S3-compatible endpoint) through boto3. boto3/botocore
are imported lazily and only here: nothing outside this module sees an S3
client, a botocore exception or an S3 response dict.

Keys are object keys, not paths. An instance is scoped to one bucket and an
optional key prefix, so the same area-relative key the filesystem backend
uses ("abc.json") becomes "<prefix>abc.json" in the bucket:

    MinioObjectStorage.from_settings(cfg, prefix="extracted_rows/")
        .put_bytes("abc.json", ...)      ->  s3://spir-files/extracted_rows/abc.json

Transfers are plain single-request PUT / GET (put_object / get_object with a
streamed body). boto3's managed transfer layer (multipart, parallel ranged
GETs) is deliberately not used — that is a later phase.

Connection settings come from the Phase 3A MINIO_* settings only; this module
never reads the environment itself.
"""
from __future__ import annotations

import io
import mimetypes
from datetime import timezone
from pathlib import Path
from typing import IO, Any, ClassVar, Iterator

import structlog

from spir_dynamic.app.config import Settings
from spir_dynamic.services.object_storage.base import (
    ObjectInfo,
    ObjectNotFound,
    StorageConfigError,
    StorageError,
    StorageUnavailable,
    normalize_key,
    normalize_prefix,
)

log = structlog.stdlib.get_logger(__name__)

_DOWNLOAD_CHUNK = 1024 * 1024
_DEFAULT_CONTENT_TYPE = "application/octet-stream"

# S3 error codes that mean "the object is not there".
_NOT_FOUND_CODES = {"NoSuchKey", "404", "NotFound"}
# S3 error codes that mean "the backend itself is unusable" (config/auth/bucket).
_UNAVAILABLE_CODES = {
    "NoSuchBucket", "AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch",
    "403", "401", "InvalidBucketName", "AuthorizationHeaderMalformed",
}


def _endpoint_url(endpoint: str, secure: bool) -> str:
    """MINIO_ENDPOINT may or may not carry a scheme; MINIO_SECURE decides when it does not."""
    endpoint = endpoint.strip().rstrip("/")
    if "://" in endpoint:
        return endpoint
    return ("https://" if secure else "http://") + endpoint


class _BodyReader(io.RawIOBase):
    """Adapts the S3 response body to a real binary stream so botocore types never leak."""

    def __init__(self, body: Any) -> None:
        super().__init__()
        self._body = body

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        data = self._body.read(len(buffer))
        n = len(data)
        buffer[:n] = data
        return n

    def close(self) -> None:
        try:
            self._body.close()
        finally:
            super().close()


class MinioObjectStorage:
    backend: ClassVar[str] = "minio"

    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool = False,
        prefix: str = "",
        connect_timeout: float = 5.0,
        read_timeout: float = 60.0,
        max_attempts: int = 2,
    ) -> None:
        if not endpoint or not access_key or not secret_key or not bucket:
            raise StorageConfigError("minio backend needs endpoint, access key, secret key and bucket")
        self.endpoint_url = _endpoint_url(endpoint, secure)
        self.bucket = bucket
        self.prefix = normalize_prefix(prefix)
        self._access_key = access_key
        self._secret_key = secret_key
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._max_attempts = max_attempts
        self._client: Any = None

    @classmethod
    def from_settings(cls, settings: Settings, *, prefix: str = "") -> "MinioObjectStorage":
        if not settings.minio_configured:
            raise StorageConfigError(
                "MINIO_ENDPOINT, MINIO_ACCESS_KEY and MINIO_SECRET_KEY must all be set "
                "for the minio storage backend"
            )
        return cls(
            endpoint=settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            bucket=settings.minio_bucket,
            secure=settings.minio_secure,
            prefix=prefix,
        )

    def __repr__(self) -> str:
        return f"MinioObjectStorage(endpoint={self.endpoint_url!r}, bucket={self.bucket!r}, prefix={self.prefix!r})"

    # ── client / error translation (botocore stays inside these two helpers) ─

    def _s3(self) -> Any:
        if self._client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:   # pragma: no cover - environment problem
                raise StorageUnavailable("boto3 is not installed") from exc
            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self._access_key,
                aws_secret_access_key=self._secret_key,
                region_name="us-east-1",   # required by SigV4; MinIO ignores it
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": "path"},   # bucket in the path, not a subdomain
                    connect_timeout=self._connect_timeout,
                    read_timeout=self._read_timeout,
                    retries={"max_attempts": self._max_attempts, "mode": "standard"},
                    # Plain SigV4 payloads — no trailing-checksum chunked encoding.
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                ),
            )
        return self._client

    def _translate(self, exc: Exception, key: str | None) -> StorageError:
        """Map a botocore exception to the contract's error hierarchy."""
        if isinstance(exc, StorageError):
            return exc
        try:
            from botocore import exceptions as be
        except ImportError:   # pragma: no cover - environment problem
            return StorageUnavailable(str(exc))

        if isinstance(exc, be.ClientError):
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if key is not None and code in _NOT_FOUND_CODES:
                return ObjectNotFound(key)
            if code in _UNAVAILABLE_CODES:
                return StorageUnavailable(f"{code}: {exc}")
            return StorageError(f"{code or 'S3 error'}: {exc}")
        if isinstance(exc, (
            be.EndpointConnectionError, be.ConnectTimeoutError, be.ReadTimeoutError,
            be.ConnectionClosedError, be.NoCredentialsError, be.SSLError, be.ProxyConnectionError,
        )):
            return StorageUnavailable(str(exc))
        if isinstance(exc, be.BotoCoreError):
            return StorageError(str(exc))
        return StorageError(str(exc))

    def _key(self, key: str) -> str:
        return self.prefix + normalize_key(key)

    def _strip(self, full_key: str) -> str:
        return full_key[len(self.prefix):] if self.prefix and full_key.startswith(self.prefix) else full_key

    def _info(self, key: str, head: dict[str, Any]) -> ObjectInfo:
        lm = head["LastModified"]
        if lm.tzinfo is None:
            lm = lm.replace(tzinfo=timezone.utc)
        return ObjectInfo(
            key=key,
            size=int(head.get("ContentLength", head.get("Size", 0))),
            last_modified=lm.astimezone(timezone.utc),
            content_type=head.get("ContentType"),
        )

    # ── contract ─────────────────────────────────────────────────────────────

    def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> ObjectInfo:
        ctype = content_type or mimetypes.guess_type(key)[0] or _DEFAULT_CONTENT_TYPE
        try:
            self._s3().put_object(Bucket=self.bucket, Key=self._key(key), Body=data, ContentType=ctype)
        except Exception as exc:
            raise self._translate(exc, None) from exc
        return self.stat(key)

    def put_file(self, key: str, source: Path, *, content_type: str | None = None) -> ObjectInfo:
        source = Path(source)
        if not source.is_file():
            raise FileNotFoundError(f"source file not found: {source}")
        ctype = content_type or mimetypes.guess_type(key)[0] or _DEFAULT_CONTENT_TYPE
        try:
            with source.open("rb") as fh:
                self._s3().put_object(Bucket=self.bucket, Key=self._key(key), Body=fh, ContentType=ctype)
        except Exception as exc:
            raise self._translate(exc, None) from exc
        return self.stat(key)

    def get_bytes(self, key: str) -> bytes:
        try:
            resp = self._s3().get_object(Bucket=self.bucket, Key=self._key(key))
            with resp["Body"] as body:
                return body.read()
        except Exception as exc:
            raise self._translate(exc, key) from exc

    def get_file(self, key: str, dest: Path) -> Path:
        dest = Path(dest)
        try:
            resp = self._s3().get_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as exc:
            raise self._translate(exc, key) from exc
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with resp["Body"] as body, dest.open("wb") as out:
                while chunk := body.read(_DOWNLOAD_CHUNK):
                    out.write(chunk)
        except Exception as exc:
            dest.unlink(missing_ok=True)
            if isinstance(exc, OSError):
                raise StorageError(f"download failed for {key}: {exc}") from exc
            raise self._translate(exc, key) from exc
        return dest

    def open_read(self, key: str) -> IO[bytes]:
        try:
            resp = self._s3().get_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as exc:
            raise self._translate(exc, key) from exc
        return io.BufferedReader(_BodyReader(resp["Body"]))

    def exists(self, key: str) -> bool:
        try:
            self.stat(key)
        except ObjectNotFound:
            return False
        return True

    def delete(self, key: str) -> bool:
        # S3 DELETE is silent for missing keys; a HEAD first gives the contract's bool.
        if not self.exists(key):
            return False
        try:
            self._s3().delete_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as exc:
            raise self._translate(exc, None) from exc
        return True

    def stat(self, key: str) -> ObjectInfo:
        try:
            head = self._s3().head_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as exc:
            raise self._translate(exc, key) from exc
        return self._info(key, head)

    def list_objects(self, prefix: str = "") -> Iterator[ObjectInfo]:
        full_prefix = self.prefix + normalize_prefix(prefix)
        try:
            paginator = self._s3().get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
                for obj in page.get("Contents", []):
                    yield self._info(self._strip(obj["Key"]), obj)
        except Exception as exc:
            raise self._translate(exc, None) from exc

    def ping(self) -> None:
        try:
            self._s3().head_bucket(Bucket=self.bucket)
        except Exception as exc:
            err = self._translate(exc, None)
            raise StorageUnavailable(str(err)) from exc
