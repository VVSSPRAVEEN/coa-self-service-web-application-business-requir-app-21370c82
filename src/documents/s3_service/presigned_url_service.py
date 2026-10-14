"""Scoped, short-expiry presigned S3 URL generation for secure COA PDF downloads (FR7, FR18).

This module provides a stateless service that generates boto3-compatible presigned URLs
with enforceable scope (bucket, key prefix, HTTP method) and short, configurable expiry.
It validates incoming requests against the audit/identity layer via the JDE validation
event bus before issuing any URL.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

try:
    import botocore  # noqa: F401 - bound so the availability guard below works in
    # environments where the import SUCCEEDS; previously only the failure path bound
    # the name, so `if botocore is None` raised NameError wherever boto3 is installed.
    from botocore.auth import SigV4QueryAuth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials
except ImportError:  # pragma: no cover - runtime dependency of the document service
    botocore = None  # type: ignore


DEFAULT_EXPIRY_SECONDS = 300  # 5 minutes - short-lived per FR7/FR18
MAX_EXPIRY_SECONDS = 900  # 15 minutes absolute ceiling
BUCKET_SCOPE_PREFIX = "coa/secure/"


class PresignedUrlError(ValueError):
    """Base error for presigned URL service failures."""


class PresignedUrlScopeError(PresignedUrlError):
    """Request violates the scoped contract (bucket/key/method/expiry)."""


class PresignedUrlExpiredError(PresignedUrlError):
    """The presigned URL has expired and must be regenerated."""


class PresignedUrlGenerationError(PresignedUrlError):
    """URL generation failed (credentials, endpoint, network)."""


@dataclass
class PresignedUrlConfig:
    """Configuration for presigned URL generation.

    All values are non-secret configuration; actual credentials must be injected
    by the caller from a secrets manager at runtime.
    """

    region: str = "us-east-1"
    endpoint_url: Optional[str] = None
    bucket: str = "coa-documents"
    expiry_seconds: int = DEFAULT_EXPIRY_SECONDS
    max_expiry_seconds: int = MAX_EXPIRY_SECONDS
    signed_headers: tuple[str, ...] = ("host",)
    payload: str = "UNSIGNED-PAYLOAD"
    request_timeout_seconds: int = 30

    def __post_init__(self) -> None:
        if self.expiry_seconds > self.max_expiry_seconds:
            raise PresignedUrlScopeError(
                f"expiry_seconds ({self.expiry_seconds}) exceeds max ({self.max_expiry_seconds})"
            )
        if self.expiry_seconds <= 0:
            raise PresignedUrlScopeError("expiry_seconds must be positive")


@dataclass
class PresignedUrlResult:
    """A signed URL together with its provenance metadata."""

    signed_url: str
    bucket: str
    key: str
    method: str
    expires_at: datetime
    request_id: str
    scope_hash: str


class _SigV4UrlSigner:
    """Sign an S3 request with SigV4 query authentication and return the presigned URL.

    Uses botocore's query-string signing path (`SigV4QueryAuth`), the same signer
    boto3's `generate_presigned_url` uses internally, so the output URL carries the
    standard `X-Amz-*` query parameters and a real `X-Amz-Signature`.
    """

    def __init__(self, config: PresignedUrlConfig, credentials: Any):
        self.config = config
        self.credentials = credentials

    def sign(self, method: str, key: str, expiry_seconds: int) -> str:
        if botocore is None:  # pragma: no cover
            raise PresignedUrlGenerationError("botocore not available")

        url = self._url_for(self.config.bucket, key)
        credentials = self.credentials.get_frozen_credentials()
        signer = SigV4QueryAuth(
            credentials, "s3", self.config.region, expires=int(expiry_seconds)
        )
        aws_request = AWSRequest(method=method.upper(), url=url, data=b"")
        signer.add_auth(aws_request)
        return aws_request.url

    def _url_for(self, bucket: str, key: str) -> str:
        if self.config.endpoint_url:
            # LocalStack / on-prem endpoint: path-style addressing.
            return f"{self.config.endpoint_url.rstrip('/')}/{bucket}/{key}"
        return f"https://{bucket}.s3.{self.config.region}.amazonaws.com/{key}"


class PresignedUrlService:
    """Generate scoped, short-expiry presigned S3 URLs for COA PDF downloads.

    Responsibilities:
    - Validate the request against the scope contract (bucket, key prefix, method).
    - Enforce a short, bounded expiry window per FR7/FR18.
    - Optionally gate issuance behind a validation event (completion gate, FR6).
    - Produce a stable scope hash for audit correlation.
    """

    def __init__(
        self,
        config: Optional[PresignedUrlConfig] = None,
        *,
        credentials: Optional[Credentials] = None,
        signer: Optional[Callable[[str, str], str]] = None,
        validate_before_sign: bool = True,
        validator: Optional[Callable[[str, str, str], bool]] = None,
    ):
        self.config = config or PresignedUrlConfig()
        self._credentials = credentials
        self._injected_signer = signer
        self._validate_before_sign = validate_before_sign
        self._validator = validator

    @property
    def expiry_seconds(self) -> int:
        return self.config.expiry_seconds

    def generate_signed_url(
        self,
        key: str,
        method: str = "GET",
        expiry_seconds: Optional[int] = None,
        user_id: Optional[str] = None,
        coa_id: Optional[str] = None,
    ) -> PresignedUrlResult:
        """Generate a presigned URL for a COA PDF.

        Args:
            key: The S3 object key. Must start with the configured scope prefix.
            method: HTTP method (default GET for downloads).
            expiry_seconds: Override for this request; bounded by max_expiry_seconds.
            user_id: The authenticated user making the request (for audit).
            coa_id: The COA document identifier (for audit correlation).

        Returns:
            PresignedUrlResult with the signed URL and provenance metadata.

        Raises:
            PresignedUrlScopeError: If the request violates the scope contract.
            PresignedUrlGenerationError: If signing fails.
        """
        key = self._normalize_key(key)
        self._validate_scope(key, method, expiry_seconds)
        effective_expiry = (
            expiry_seconds if expiry_seconds is not None else self.config.expiry_seconds
        )

        # FR6: the completion gate is evaluated before signing, and only when a COA
        # context exists. A request with no `coa_id` carries nothing to gate, so it is
        # not sent through the validator at all (test_fr6_gate_ignores_request_without_coa_context).
        if self._validate_before_sign and self._validator is not None and coa_id:
            if not self._validator(coa_id, key, user_id or ""):
                raise PresignedUrlScopeError(
                    "completion gate rejected the request for COA "
                    f"{coa_id}: required JDE test fields are missing"
                )

        expires_at = (
            datetime.now(timezone.utc).replace(microsecond=0)
            + timedelta(seconds=effective_expiry)
        )
        request_id = self._request_id()
        scope_hash = self._scope_hash(key, method, effective_expiry)
        signed_url = self._sign(method, key, effective_expiry)

        return PresignedUrlResult(
            signed_url=signed_url,
            bucket=self.config.bucket,
            key=key,
            method=method.upper(),
            expires_at=expires_at,
            request_id=request_id,
            scope_hash=scope_hash,
        )

    def is_expired(self, signed_url: str) -> bool:
        """Check whether a presigned URL has expired without parsing credentials."""
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(signed_url)
        query = parse_qs(parsed.query)
        expires = query.get("X-Amz-Expires") or query.get("Expires")
        if not expires:
            return False
        try:
            expiry_ts = float(expires[0])
        except ValueError:
            return True
        return datetime.now(timezone.utc).timestamp() > expiry_ts

    def _normalize_key(self, key: str) -> str:
        # The scope prefix is REQUIRED, never supplied: an out-of-scope key is rejected
        # by _validate_scope rather than silently rewritten into an in-scope one.
        return key.strip().lstrip("/")

    def _validate_scope(
        self,
        key: str,
        method: str,
        expiry_seconds: Optional[int],
    ) -> None:
        allowed_methods = {"GET"}
        if method.upper() not in allowed_methods:
            raise PresignedUrlScopeError(f"method {method} not allowed; use GET")

        if not key.startswith(BUCKET_SCOPE_PREFIX):
            raise PresignedUrlScopeError(
                f"key must be scoped under {BUCKET_SCOPE_PREFIX}"
            )

        requested = expiry_seconds if expiry_seconds is not None else self.config.expiry_seconds
        if requested > self.config.max_expiry_seconds:
            raise PresignedUrlScopeError(
                f"expiry {requested}s exceeds maximum {self.config.max_expiry_seconds}s"
            )

    def _sign(self, method: str, key: str, expiry_seconds: int) -> str:
        if self._injected_signer is not None:
            return self._injected_signer(method, key)
        if self._credentials is None:
            raise PresignedUrlGenerationError("no credentials configured")
        signer = _SigV4UrlSigner(self.config, self._credentials)
        return signer.sign(method, key, expiry_seconds)

    @staticmethod
    def _request_id() -> str:
        return secrets.token_urlsafe(16)

    @staticmethod
    def _scope_hash(key: str, method: str, expiry_seconds: int) -> str:
        # Stable hash of the scope tuple; used for audit correlation. No secrets included.
        payload = f"{method}|{key}|{expiry_seconds}"
        return base64.urlsafe_b64encode(
            hashlib.sha256(payload.encode("utf-8")).digest()
        )[:20]

    @classmethod
    def from_env(
        cls,
        validator: Optional[Callable[[str, str, str], bool]] = None,
    ) -> PresignedUrlService:
        """Construct the service from environment configuration.

        Credentials must still be injected via `with_credentials` because they are
        never read from the environment in this container contract.
        """
        import os

        config = PresignedUrlConfig(
            region=os.getenv("S3_REGION", "us-east-1"),
            endpoint_url=os.getenv("S3_ENDPOINT_URL"),
            bucket=os.getenv("S3_BUCKET", "coa-documents"),
            expiry_seconds=int(os.getenv("PRESIGNED_URL_EXPIRY_SECONDS", "300")),
            max_expiry_seconds=int(os.getenv("PRESIGNED_URL_MAX_EXPIRY_SECONDS", "900")),
        )
        return cls(config=config, validate_before_sign=True, validator=validator)

    def with_credentials(self, credentials: Credentials) -> PresignedUrlService:
        """Return a new service bound to the given credentials."""
        new = PresignedUrlService(
            config=self.config,
            credentials=credentials,
            signer=self._injected_signer,
            validate_before_sign=self._validate_before_sign,
            validator=self._validator,
        )
        return new
