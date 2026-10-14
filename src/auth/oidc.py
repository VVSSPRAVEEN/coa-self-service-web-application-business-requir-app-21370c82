"""Token-exchange authentication against an OIDC provider (Cognito by default).

This module implements the login half of the approved contract: the client
presents a bearer token it already obtained from the identity provider, and the
API exchanges it for a normalised :class:`User`. There is no local password
store, so nothing here performs a network call unless a JWKS endpoint has been
configured.

Verification strategy, in order of precedence:

* ``AUTH_HS256_SECRET`` set -> the HS256 signature is verified with
  :mod:`hmac` from the standard library. This is the symmetric mode used by the
  local/test deployment and by any environment that mints its own tokens.
* ``COGNITO_JWKS_URL`` set and :mod:`jwt` (PyJWT) importable -> the signing key
  is fetched from the JWKS document with :mod:`urllib` and the RS256 signature is
  verified by PyJWT against that key.
* ``COGNITO_JWKS_URL`` set but PyJWT *not* importable -> **signature verification
  is skipped** and only the structural and temporal claims are enforced. This is
  a deliberate, documented degradation so the package still works on an image
  without PyJWT, and it is reported through
  :func:`signature_verification_available` so a deployment can assert on it.
  Structure (three dot-separated base64url segments holding JSON objects) and
  expiry (``exp``, ``nbf``) are always enforced, and ``iss``/``aud`` are enforced
  whenever the provider configuration and the claims are both present.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

__all__ = [
    "AuthResponse",
    "TokenValidationError",
    "User",
    "get_provider_config",
    "login_flow",
    "signature_verification_available",
    "validate_token",
]

#: Clock skew tolerated on ``exp``/``nbf``, in seconds.
CLOCK_SKEW_SECONDS = 60

#: Algorithms accepted from an asymmetric provider.
JWKS_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512")

_JWKS_CLIENTS: dict[str, Any] = {}
_JWKS_LOCK = threading.Lock()


class TokenValidationError(ValueError):
    """Raised when a token is malformed, expired or fails verification.

    The reason is safe to log but is never returned to the caller: the HTTP layer
    answers every failure with one fixed message.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class User:
    """Normalised identity extracted from a verified token."""

    user_id: str
    username: str
    roles: list[str] = field(default_factory=list)
    plants: list[str] = field(default_factory=list)
    email: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-ready mapping used by the login contract."""
        return {
            "user_id": self.user_id,
            "username": self.username,
            "roles": list(self.roles),
            "plants": list(self.plants),
            "email": self.email,
        }


@dataclass(frozen=True)
class AuthResponse:
    """Result of a successful token exchange."""

    user: User
    token: str
    expires_at: str


@dataclass(frozen=True)
class _DecodedToken:
    """A structurally valid token whose temporal claims already passed."""

    header: dict[str, Any]
    claims: dict[str, Any]
    signature: str
    signing_input: str


def get_provider_config() -> dict[str, Any]:
    """Read the OIDC provider configuration from the environment.

    Returns:
        A mapping with ``issuer``, ``jwks_url`` and ``client_id`` (all empty
        strings when unset), plus a ``signature_mode`` summary of ``"hs256"``,
        ``"jwks"`` or ``"none"``. The HMAC secret itself is never returned.
    """
    issuer = os.getenv("COGNITO_ISSUER", "")
    jwks_url = os.getenv("COGNITO_JWKS_URL", "")
    client_id = os.getenv("COGNITO_CLIENT_ID", "")
    if os.getenv("AUTH_HS256_SECRET", ""):
        signature_mode = "hs256"
    elif jwks_url:
        signature_mode = "jwks"
    else:
        signature_mode = "none"
    return {
        "issuer": issuer,
        "jwks_url": jwks_url,
        "client_id": client_id,
        "signature_mode": signature_mode,
    }


def signature_verification_available() -> bool:
    """True when the process can actually verify a signature.

    ``False`` means the deployment is in the documented degraded mode: a JWKS URL
    is configured but PyJWT is not importable, so only structure and expiry are
    enforced.
    """
    config = get_provider_config()
    if config["signature_mode"] == "hs256":
        return True
    if config["signature_mode"] == "jwks":
        return _pyjwt_module() is not None
    return False


def _pyjwt_module() -> Any | None:
    """Return the PyJWT module when importable, else ``None``."""
    try:  # pragma: no cover - depends on the installed image
        import jwt  # type: ignore

        return jwt
    except ImportError:  # pragma: no cover - degraded mode documented above
        return None


def _b64decode(segment: str) -> bytes:
    """Decode one unpadded base64url JWT segment."""
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except (binascii.Error, ValueError) as exc:
        raise TokenValidationError("token segment is not valid base64url") from exc


def _b64encode(raw: bytes) -> str:
    """Encode bytes as unpadded base64url, matching JWT conventions."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_segment(segment: str, what: str) -> dict[str, Any]:
    """Decode and JSON-parse one JWT segment into a mapping."""
    try:
        raw = _b64decode(segment)
    except TokenValidationError as exc:
        raise TokenValidationError(f"token {what} is not valid base64url") from exc
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise TokenValidationError(f"token {what} is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise TokenValidationError(f"token {what} is not a JSON object")
    return parsed


def _decode_structure(token: str) -> _DecodedToken:
    """Split and parse a token, enforcing only its structure."""
    if not isinstance(token, str) or not token.strip():
        raise TokenValidationError("missing token")
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenValidationError("token is not a three-segment JWT")
    header_segment, payload_segment, signature = parts
    if not header_segment or not payload_segment:
        raise TokenValidationError("token header or payload is empty")
    header = _decode_segment(header_segment, "header")
    claims = _decode_segment(payload_segment, "payload")
    return _DecodedToken(
        header=header,
        claims=claims,
        signature=signature,
        signing_input=f"{header_segment}.{payload_segment}",
    )


def _numeric_claim(claims: dict[str, Any], name: str) -> float | None:
    """Return a numeric JWT claim, rejecting booleans and non-numbers."""
    value = claims.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TokenValidationError(f"token claim {name} is not numeric")
    return float(value)


def _verify_temporal_claims(claims: dict[str, Any], now: float) -> float:
    """Enforce ``exp`` (required) and ``nbf`` (when present); return ``exp``."""
    exp = _numeric_claim(claims, "exp")
    if exp is None:
        # Fail closed: a token without an expiry can never be proven current.
        raise TokenValidationError("token has no exp claim")
    if now - CLOCK_SKEW_SECONDS > exp:
        raise TokenValidationError("token expired")
    nbf = _numeric_claim(claims, "nbf")
    if nbf is not None and now + CLOCK_SKEW_SECONDS < nbf:
        raise TokenValidationError("token is not valid yet")
    return exp


def _verify_audience_and_issuer(claims: dict[str, Any]) -> None:
    """Enforce ``aud``/``iss`` when the provider configuration supplies them."""
    config = get_provider_config()
    audience = claims.get("aud")
    client_id = config["client_id"]
    if audience is not None and client_id:
        audiences = audience if isinstance(audience, (list, tuple)) else [audience]
        if client_id not in [str(item) for item in audiences]:
            raise TokenValidationError("token audience does not match this application")
    issuer = claims.get("iss")
    if issuer is not None and config["issuer"] and str(issuer) != config["issuer"]:
        raise TokenValidationError("token issuer does not match the provider")


def _verify_hs256(token: _DecodedToken, secret: str) -> None:
    """Verify an HS256 signature with the stdlib HMAC implementation."""
    if token.header.get("alg") != "HS256":
        raise TokenValidationError("token algorithm is not HS256")
    digest = hmac.new(
        secret.encode("utf-8"),
        token.signing_input.encode("ascii"),
        sha256,
    ).digest()
    expected = _b64encode(digest)
    # Tolerate either padding convention on the presented signature.
    presented = token.signature.rstrip("=")
    if not hmac.compare_digest(presented, expected):
        raise TokenValidationError("token signature verification failed")


def _jwks_client(url: str) -> Any:
    """Return a cached PyJWK client for ``url``, or raise when unavailable."""
    with _JWKS_LOCK:
        client = _JWKS_CLIENTS.get(url)
    if client is not None:
        return client
    jwt = _pyjwt_module()
    if jwt is None:
        raise TokenValidationError("token signature verification is unavailable")
    try:
        client = jwt.PyJWKClient(url)
    except Exception as exc:
        raise TokenValidationError("token signing keys are unavailable") from exc
    with _JWKS_LOCK:
        _JWKS_CLIENTS[url] = client
    return client


def _verify_with_jwks(raw_token: str, token: _DecodedToken) -> None:
    """Verify an RS256 (or EC) signature against the configured JWKS endpoint."""
    algorithm = str(token.header.get("alg") or "")
    if algorithm not in JWKS_ALGORITHMS:
        raise TokenValidationError(f"token algorithm {algorithm or 'missing'} is not supported")

    if _pyjwt_module() is None:
        # Documented degraded mode: structure and expiry are already enforced.
        return

    client = _jwks_client(get_provider_config()["jwks_url"])
    try:
        signing_key = client.get_signing_key_from_jwt(raw_token)
    except TokenValidationError:
        raise
    except Exception as exc:
        raise TokenValidationError("token signing key could not be resolved") from exc

    jwt = _pyjwt_module()
    try:
        jwt.decode(
            raw_token,
            signing_key.key,
            algorithms=[algorithm],
            options={"verify_exp": True, "verify_signature": True},
            leeway=CLOCK_SKEW_SECONDS,
        )
    except TokenValidationError:
        raise
    except Exception as exc:
        raise TokenValidationError("token signature verification failed") from exc


def _decode_and_verify(token: str) -> tuple[_DecodedToken, float]:
    """Decode ``token``, enforce its claims and verify its signature."""
    decoded = _decode_structure(token)
    now = time.time()
    exp = _verify_temporal_claims(decoded.claims, now)
    _verify_audience_and_issuer(decoded.claims)

    secret = os.getenv("AUTH_HS256_SECRET", "")
    if secret:
        _verify_hs256(decoded, secret)
    elif get_provider_config()["jwks_url"]:
        _verify_with_jwks(token, decoded)
    return decoded, exp


def _as_str_list(value: Any) -> list[str]:
    """Coerce a scalar or sequence claim into a list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value)]


def _build_user(claims: dict[str, Any]) -> User:
    """Map verified claims onto the normalised :class:`User`."""
    subject = claims.get("sub") or claims.get("user_id") or claims.get("username")
    if not subject:
        raise TokenValidationError("token has no subject claim")
    username = (
        claims.get("preferred_username")
        or claims.get("cognito:username")
        or claims.get("username")
        or claims.get("email")
        or subject
    )
    roles = claims.get("cognito:groups")
    if not roles:
        roles = claims.get("roles")
    plants = claims.get("custom:plants")
    if not plants:
        plants = claims.get("plants")
    if not plants:
        plants = claims.get("plant")
    email = claims.get("email")
    return User(
        user_id=str(subject),
        username=str(username),
        roles=_as_str_list(roles),
        plants=_as_str_list(plants),
        email=str(email) if email else None,
    )


def validate_token(token: str) -> User:
    """Verify ``token`` and return the normalised :class:`User` it represents.

    Args:
        token: The compact JWS presented by the client.

    Returns:
        The verified :class:`User`.

    Raises:
        TokenValidationError: If the token is malformed, expired, wrongly
            addressed, or its signature does not verify.
    """
    decoded, _ = _decode_and_verify(token)
    return _build_user(decoded.claims)


def login_flow(token: str) -> AuthResponse:
    """Exchange ``token`` for an :class:`AuthResponse`.

    Args:
        token: The compact JWS presented by the client.

    Returns:
        An :class:`AuthResponse` carrying the verified user, the token as issued
        and the ISO-8601 expiry taken from the token's own ``exp`` claim.

    Raises:
        TokenValidationError: Propagated from :func:`validate_token`.
    """
    decoded, exp = _decode_and_verify(token)
    user = _build_user(decoded.claims)
    expires_at = datetime.fromtimestamp(exp, tz=UTC).isoformat()
    return AuthResponse(user=user, token=token, expires_at=expires_at)
