"""Print actions are sensitive operations that require re-authentication and
immutable audit recording. This service enforces the re-auth contract and emits
audit events for every print attempt (success or failure).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Protocol

# Action names written into the immutable audit trail (FR8 / FR12).
PRINT_ATTEMPT = "print_attempt"
PRINT_SUCCESS = "print_success"
PRINT_DENIED_REAUTH = "print_denied_reauth"
PRINT_DENIED_VALIDATION = "print_denied_validation"


class ReauthError(ValueError):
    """Raised when re-authentication configuration is unusable."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SignedUrlResult(Protocol):
    """Structural contract of the presigned-URL result (FR7 / FR18).

    The concrete object is produced by the document service; this service only
    reads its public fields, so no competing model is defined here.
    """

    signed_url: str
    expires_in: int


@dataclass
class PrintAuditEvent:
    """An immutable audit record for a print action."""

    event_id: str
    timestamp: datetime
    user_id: str
    coa_id: str
    action: str
    detail: str
    scope_hash: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp.isoformat(),
            "user_id": self.user_id,
            "coa_id": self.coa_id,
            "action": self.action,
            "detail": self.detail,
            "scope_hash": self.scope_hash,
        }


@dataclass
class PrintRequest:
    """A print request requiring re-authentication.

    ``reauth_token`` is a fresh, short-lived credential the client must obtain
    after the original session authentication (a one-time re-auth code or a
    re-entered password verified by the auth provider).
    """

    user_id: str
    coa_id: str
    reauth_token: str
    print_scope_hash: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PrintResponse:
    """Result of a print request."""

    success: bool
    audit_event: PrintAuditEvent
    presigned_url: Optional[str] = None


class PrintService:
    """Enforce re-authentication and audit logging on print endpoints.

    Responsibilities:
    - Require a fresh re-auth token on every print request (FR8).
    - Reject stale, missing or invalid re-auth tokens with audit events.
    - Optionally gate print behind the completion-validation event (FR6).
    - Emit an immutable audit event for every outcome (FR12).
    - Generate a scoped, short-lived presigned URL on successful print
      (FR7 / FR18).
    """

    REAUTH_ALGORITHM = "HS256"
    # Re-auth tokens live at most 60s (FR8 short-lived credential contract).
    REAUTH_LIFETIME_SECONDS = 60

    def __init__(
        self,
        *,
        reauth_secret: bytes,
        presigned_url_service: Optional[Any] = None,
        validate_before_print: bool = True,
        print_validator: Optional[Callable[[str, str], bool]] = None,
        audit_logger: Optional[Callable[[PrintAuditEvent], None]] = None,
    ) -> None:
        if not reauth_secret:
            raise ValueError("reauth_secret must be a non-empty secret")
        self._reauth_secret = reauth_secret
        self._presigned_url_service = presigned_url_service
        self._validate_before_print = validate_before_print
        self._print_validator = print_validator
        self._audit_logger = audit_logger

    def issue_reauth_token(self, user_id: str, coa_id: str) -> str:
        """Mint a fresh, short-lived re-auth token for this user/COA pair.

        The token is the credential the print endpoint requires (FR8). It is
        HMAC-signed with the same secret ``from_env`` reads, so a token minted
        by the auth layer verifies here and nowhere else.
        """
        iat = int(datetime.now(timezone.utc).timestamp())
        header = self._b64encode(
            json.dumps({"alg": self.REAUTH_ALGORITHM, "typ": "JWT"}).encode("utf-8")
        )
        payload = self._b64encode(
            json.dumps({"user_id": user_id, "coa_id": coa_id, "iat": iat}).encode("utf-8")
        )
        signature = self._compute_signature(user_id, coa_id, float(iat))
        return f"{header}.{payload}.{signature}"

    def handle_print(self, request: PrintRequest) -> PrintResponse:
        """Process a print request with re-auth enforcement and audit."""
        user_id = request.user_id
        coa_id = request.coa_id
        timestamp = datetime.now(timezone.utc)

        # FR8: verify the re-auth token first; failure is a denied audit event.
        reauth_ok, reason = self._verify_reauth(request.reauth_token, user_id, coa_id)
        if not reauth_ok:
            return self._deny(
                user_id=user_id,
                coa_id=coa_id,
                timestamp=timestamp,
                action=PRINT_DENIED_REAUTH,
                detail=reason,
                scope_hash=request.print_scope_hash,
            )

        # FR6: completion-gate validation runs before any distribution.
        if self._validate_before_print and self._print_validator is not None:
            if not self._print_validator(coa_id, user_id):
                return self._deny(
                    user_id=user_id,
                    coa_id=coa_id,
                    timestamp=timestamp,
                    action=PRINT_DENIED_VALIDATION,
                    detail="completion gate validation failed for print",
                    scope_hash=request.print_scope_hash,
                )

        # Scoped presigned URL for the print download (FR7 / FR18).
        url: Optional[str] = None
        provider = self._presigned_url_service
        if provider is not None:
            try:
                result: SignedUrlResult = provider.generate_signed_url(
                    key=f"coa/secure/{coa_id}.pdf",
                    user_id=user_id,
                    coa_id=coa_id,
                )
                url = result.signed_url
            except Exception as exc:  # deny, never leak internals
                return self._deny(
                    user_id=user_id,
                    coa_id=coa_id,
                    timestamp=timestamp,
                    action=PRINT_DENIED_REAUTH,
                    detail=f"presigned URL generation failed: {exc}",
                    scope_hash=request.print_scope_hash,
                )

        scope = request.print_scope_hash or "none"
        event = PrintAuditEvent(
            event_id=self._event_id(),
            timestamp=timestamp,
            user_id=user_id,
            coa_id=coa_id,
            action=PRINT_SUCCESS,
            detail=f"print granted after re-auth; scope_hash={scope}",
            scope_hash=request.print_scope_hash,
        )
        self._emit(event)
        return PrintResponse(success=True, audit_event=event, presigned_url=url)

    def _deny(
        self,
        *,
        user_id: str,
        coa_id: str,
        timestamp: datetime,
        action: str,
        detail: str,
        scope_hash: Optional[str] = None,
    ) -> PrintResponse:
        """Emit a denial audit event and return a failed response."""
        event = PrintAuditEvent(
            event_id=self._event_id(),
            timestamp=timestamp,
            user_id=user_id,
            coa_id=coa_id,
            action=action,
            detail=detail,
            scope_hash=scope_hash,
        )
        self._emit(event)
        return PrintResponse(success=False, audit_event=event)

    def _verify_reauth(self, token: str, user_id: str, coa_id: str) -> tuple[bool, str]:
        if not token:
            return False, "missing re-auth token"

        parts = token.split(".")
        if len(parts) != 3:
            return False, "malformed re-auth token"
        encoded_header, encoded_payload, signature = parts

        try:
            header_bytes = self._b64decode(encoded_header)
            payload_bytes = self._b64decode(encoded_payload)
        except (ValueError, TypeError):
            return False, "malformed re-auth token"

        try:
            header_obj: Any = json.loads(header_bytes.decode("utf-8"))
            payload_obj: Any = json.loads(payload_bytes.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return False, "re-auth token contains invalid JSON"

        if not isinstance(header_obj, dict):
            return False, "re-auth token contains invalid JSON"
        if not isinstance(payload_obj, dict):
            return False, "re-auth token contains invalid JSON"
        if header_obj.get("alg") != self.REAUTH_ALGORITHM:
            return False, "unsupported re-auth token algorithm"
        if payload_obj.get("user_id") != user_id:
            return False, "re-auth token user_id does not match request"
        if payload_obj.get("coa_id") != coa_id:
            return False, "re-auth token coa_id does not match request"

        iat = payload_obj.get("iat")
        if isinstance(iat, bool) or not isinstance(iat, (int, float)):
            return False, "re-auth token missing valid issued-at timestamp"

        issued_at = float(iat)
        expected_sig = self._compute_signature(user_id, coa_id, issued_at)
        if not hmac.compare_digest(signature.rstrip("="), expected_sig):
            return False, "re-auth token signature verification failed"

        now = datetime.now(timezone.utc).timestamp()
        if issued_at > now:
            return False, "re-auth token not yet valid"
        if (now - issued_at) > self.REAUTH_LIFETIME_SECONDS:
            return False, "re-auth token expired"

        return True, "re-auth verified"

    def _compute_signature(self, user_id: str, coa_id: str, iat: float) -> str:
        payload = f"{user_id}|{coa_id}|{iat:.0f}"
        digest = hmac.new(self._reauth_secret, payload.encode("utf-8"), hashlib.sha256).digest()
        return self._b64encode(digest)

    @staticmethod
    def _b64encode(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _b64decode(segment: str) -> bytes:
        padding = "=" * (-len(segment) % 4)
        return base64.urlsafe_b64decode(segment + padding)

    @staticmethod
    def _event_id() -> str:
        return secrets.token_urlsafe(16)

    def _emit(self, event: PrintAuditEvent) -> None:
        if self._audit_logger is not None:
            self._audit_logger(event)

    @classmethod
    def from_env(cls, **overrides: Any) -> "PrintService":
        """Construct from environment configuration.

        The re-auth secret is read with the standard ``os.getenv`` configuration
        API and must be supplied by the deployment (secrets manager or the
        Compose ``environment`` mapping); it is never hard-coded here.
        """
        secret = os.getenv("PRINT_REAUTH_SECRET", "")
        if not secret:
            raise ReauthError("PRINT_REAUTH_SECRET is not configured")
        return cls(reauth_secret=secret.encode("utf-8"), **overrides)
