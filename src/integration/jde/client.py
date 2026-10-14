"""Transport boundary between the COA platform and the JD Edwards (JDE) estate (FR6, FR14).

Everything in this module is about *how* a JDE document is fetched, never about
what the answer means: the meaning is owned by :mod:`~src.integration.jde.reader`
(test fields), :mod:`~src.integration.jde.status` (COA lifecycle status) and
:mod:`~src.integration.jde.reconciliation` (the release verdict). Keeping the
transport in its own leaf module means the readers depend on a narrow, injectable
seam instead of a live socket.

Two transports ship here:

* :class:`HttpJSONTransport` - the production transport. It speaks JSON over
  HTTP to the JDE integration endpoint, whose base URL comes from the
  ``JDE_BASE_URL`` environment variable. Nothing else is read from the
  environment, and no credential is embedded here.
* :class:`RecordingTransport` - an in-process transport that records every call
  and replays queued payloads. It exists so the readers, the reconciliation and
  the HTTP contract can be exercised with no network and no JDE instance, and so
  a failing JDE read can be rehearsed deterministically.

Every failure mode a caller has to distinguish is expressed as a
:class:`JDEError` subclass:

``JDEConfigurationError``
    JDE is not configured for this deployment. The caller is expected to fall
    back to the Mule-carried record and say so, not to fail closed silently.
``JDETransportError``
    JDE *is* configured but the read failed. This is the "degrade and surface"
    case: the platform may fall back to the Mule-carried record, but the
    failure has to reach the caller.
``JDEPayloadError``
    JDE answered, but the payload is not the shape the reader requires.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

__all__ = [
    "DEFAULT_JDE_TIMEOUT_SECONDS",
    "ExpectedFieldProvider",
    "HttpJSONTransport",
    "JDE_BASE_URL_ENV",
    "JDEConfigurationError",
    "JDEError",
    "JDEPayloadError",
    "JDETransport",
    "JDETransportError",
    "RecordedRequest",
    "RecordingTransport",
    "TestFieldsPath",
    "CoaStatusPath",
    "coerce_field_names",
]

#: Environment variable naming the JDE integration endpoint.
JDE_BASE_URL_ENV = "JDE_BASE_URL"

#: Socket timeout applied to every live JDE read, in seconds.
DEFAULT_JDE_TIMEOUT_SECONDS = 15.0

#: Path of the JDE document carrying a COA's lab/test field results.
TestFieldsPath = "jde/coas/test-fields"

#: Path of the JDE document carrying a COA's lifecycle status.
CoaStatusPath = "jde/coas/status"


class JDEError(RuntimeError):
    """Base error for every JDE integration failure."""


class JDEConfigurationError(JDEError):
    """JDE is not configured for this deployment (no ``JDE_BASE_URL``)."""


class JDETransportError(JDEError):
    """JDE is configured but could not be read (network, timeout, non-2xx)."""


class JDEPayloadError(JDEError):
    """JDE answered with a payload the reader cannot interpret."""


class JDETransport(Protocol):
    """Structural contract every JDE transport satisfies.

    Implementations return the decoded JSON body of ``GET base_url + path`` and
    raise a :class:`JDEError` subclass for every failure, so a reader never has
    to know which transport it holds.
    """

    def get_json(self, path: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        """Return the decoded JSON body of one JDE read.

        Args:
            path: Path relative to the JDE base URL.
            params: Query parameters to append.

        Returns:
            The decoded JSON value, normally a mapping.

        Raises:
            JDETransportError: If the request could not be completed.
            JDEPayloadError: If the response body is not valid JSON.
        """
        ...


@dataclass(frozen=True)
class RecordedRequest:
    """One JDE read captured by :class:`RecordingTransport`."""

    path: str
    params: dict[str, Any] = field(default_factory=dict)

    def url(self, base_url: str = "") -> str:
        """Return the fully-qualified URL this request stood for."""
        if not self.params:
            return f"{base_url}{self.path}" if base_url else self.path
        query = urllib_parse.urlencode(
            {key: value for key, value in self.params.items() if value is not None}
        )
        return f"{base_url}{self.path}?{query}" if base_url else f"{self.path}?{query}"


class HttpJSONTransport:
    """Read JDE documents over HTTP JSON.

    Args:
        base_url: Root URL of the JDE integration endpoint.
        timeout: Per-request socket timeout in seconds.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = DEFAULT_JDE_TIMEOUT_SECONDS,
    ) -> None:
        if not base_url or not base_url.strip():
            raise JDEConfigurationError("a JDE base URL is required")
        self._base_url = base_url.strip().rstrip("/")
        self._timeout = timeout

    @property
    def base_url(self) -> str:
        """The configured JDE root URL, without a trailing slash."""
        return self._base_url

    def get_json(self, path: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        """Return the decoded JSON body of ``GET <base_url>/<path>``.

        Raises:
            JDETransportError: On a transport failure or a non-2xx status.
            JDEPayloadError: If the body is not decodable JSON.
        """
        clean_path = str(path or "").lstrip("/")
        query = urllib_parse.urlencode(
            {key: value for key, value in (params or {}).items() if value is not None}
        )
        url = f"{self._base_url}/{clean_path}"
        if query:
            url = f"{url}?{query}"

        if not url.startswith("https://"):
            raise JDETransportError(f"Refusing non-HTTPS JDE URL for {clean_path}")

        http_request = urllib_request.Request(url, method="GET", headers={"Accept": "application/json"})
        try:
            # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- the URL is built from operator-configured base_url + a path, and non-HTTPS targets are refused above; no request-controlled scheme reaches urlopen.
            with urllib_request.urlopen(http_request, timeout=self._timeout) as response:
                body = response.read()
        except urllib_error.HTTPError as exc:
            raise JDETransportError(
                f"JDE returned HTTP {exc.code} for {clean_path}"
            ) from exc
        except (urllib_error.URLError, OSError, ValueError) as exc:
            raise JDETransportError(f"JDE is unreachable at {self._base_url}: {exc}") from exc

        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise JDEPayloadError(f"JDE returned a body that is not JSON for {clean_path}") from exc

    @classmethod
    def from_env(
        cls,
        *,
        env: Optional[Mapping[str, str]] = None,
        timeout: float = DEFAULT_JDE_TIMEOUT_SECONDS,
    ) -> "HttpJSONTransport":
        """Build the transport from ``JDE_BASE_URL``.

        Args:
            env: Environment mapping to read; defaults to ``os.environ``.
            timeout: Per-request socket timeout in seconds.

        Raises:
            JDEConfigurationError: If ``JDE_BASE_URL`` is unset or blank.
        """
        source = os.environ if env is None else env
        base_url = (source.get(JDE_BASE_URL_ENV) or "").strip()
        if not base_url:
            raise JDEConfigurationError(
                f"{JDE_BASE_URL_ENV} is not configured; live JDE state is unavailable"
            )
        return cls(base_url, timeout=timeout)


class RecordingTransport:
    """In-process JDE transport that records reads and replays queued payloads.

    Args:
        responses: Optional initial ``{path: payload}`` queue.
        error: Optional exception raised by every read, used to rehearse the
            "JDE is configured but unreachable" degradation path.

    Every read is appended to :attr:`requests` before the queue is consulted, so
    a test can assert on the path and parameters the readers asked for.
    """

    def __init__(
        self,
        responses: Optional[Mapping[str, Any]] = None,
        *,
        error: Optional[BaseException] = None,
    ) -> None:
        self._responses: dict[str, list[Any]] = {}
        self._error = error
        self._requests: list[RecordedRequest] = []
        for path, payload in (responses or {}).items():
            self.queue(path, payload)

    def queue(self, path: str, payload: Any) -> "RecordingTransport":
        """Append ``payload`` to the queue answered for ``path``."""
        self._responses.setdefault(str(path), []).append(payload)
        return self

    def fail_with(self, error: BaseException) -> "RecordingTransport":
        """Make every subsequent read raise ``error``."""
        self._error = error
        return self

    @property
    def requests(self) -> list[RecordedRequest]:
        """Every read performed against this transport, in order."""
        return list(self._requests)

    def last_request(self) -> Optional[RecordedRequest]:
        """The most recent read, or ``None`` when nothing has been read yet."""
        return self._requests[-1] if self._requests else None

    def get_json(self, path: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        """Record the read and return the next queued payload for ``path``."""
        self._requests.append(RecordedRequest(path=str(path), params=dict(params or {})))
        if self._error is not None:
            raise self._error
        queued = self._responses.get(str(path))
        if not queued:
            raise JDEPayloadError(f"RecordingTransport has no queued payload for {path!r}")
        if len(queued) == 1:
            return queued[0]
        return queued.pop(0)


def coerce_field_names(values: Any) -> list[str]:
    """Normalise a catalog provider's answer into ordered, de-duplicated names."""
    if values is None:
        return []
    if isinstance(values, str):
        candidates: Sequence[Any] = [
            part for part in values.replace(";", ",").split(",") if part.strip()
        ]
    else:
        try:
            candidates = list(values)
        except TypeError:
            candidates = [values]
    seen: set[str] = set()
    names: list[str] = []
    for candidate in candidates:
        name = str(candidate).strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


#: Signature every expected-field provider must satisfy.
ExpectedFieldProvider = Callable[[str], Any]