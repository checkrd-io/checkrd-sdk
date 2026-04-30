"""Raw-response wrappers for power-user access to control-plane responses.

Mirrors the OpenAI / Anthropic ``with_raw_response`` /
``with_streaming_response`` pattern. Every future user-facing
control-plane method on :class:`checkrd.Checkrd` (e.g. ``alerts.create``,
``policies.list``) MUST expose both variants so observability tooling
can read ``X-Request-Id``, rate-limit headers, raw bytes, etc.

Usage shape::

    response = client.alerts.with_raw_response.create(...)
    # response is APIResponse[Alert]
    print(response.request_id)               # "req_abc123"
    print(response.headers["x-rate-limit-remaining"])
    alert = response.parse()                 # typed Alert instance
    raw_bytes = response.read()              # bytes

Streaming variant::

    with client.events.with_streaming_response.list() as stream:
        for chunk in stream.iter_bytes():
            ...

Why both variants:
  - ``with_raw_response`` buffers the body before returning so callers
    can ``parse()`` and ``read()`` repeatedly.
  - ``with_streaming_response`` keeps the underlying connection open;
    callers must consume the stream and close it (use it as a context
    manager).

This module currently defines the wrapper types only. The Checkrd
SDK does not yet expose any user-facing control-plane endpoints
that return parsed bodies — the helper methods (``wrap``,
``instrument*``) are fire-and-forget. When the first such endpoint
is added, it will go through these wrappers; defining them now
prevents the kind of "we'll add raw-response later" drift that
forces breaking changes in OSS SDKs.
"""

from __future__ import annotations

from typing import Any, Callable, Generic, Mapping, Optional, TypeVar


T = TypeVar("T")


class APIResponse(Generic[T]):
    """Buffered raw response from a control-plane call.

    Attributes:
        http_response: The underlying ``httpx.Response`` (status, headers,
            request, body bytes).
        status_code:   HTTP status code (mirrors ``http_response.status_code``
            for callers who don't want to drill into ``http_response``).
        headers:       Lower-cased response headers.
        request_id:    Server-issued request ID for support tickets,
            from ``Checkrd-Request-Id`` / ``X-Request-Id``.
        content:       Raw response body bytes.

    Use :meth:`parse` to materialise the typed body. Subsequent calls to
    :meth:`parse` return the cached value rather than re-parsing — the
    body bytes are already in memory.
    """

    http_response: Any
    status_code: int
    headers: Mapping[str, str]
    request_id: Optional[str]
    content: bytes

    def __init__(
        self,
        http_response: Any,
        *,
        parse: Callable[[bytes], T],
    ) -> None:
        self.http_response = http_response
        self.status_code = int(getattr(http_response, "status_code", 0))
        try:
            raw_headers = http_response.headers
            self.headers = {str(k).lower(): str(v) for k, v in raw_headers.items()}
        except Exception:
            self.headers = {}
        self.request_id = (
            self.headers.get("checkrd-request-id")
            or self.headers.get("x-request-id")
        )
        try:
            self.content = http_response.read()
        except Exception:
            self.content = b""
        self._parse_fn = parse
        self._parsed: Optional[T] = None

    def parse(self) -> T:
        """Return the typed body. Cached after first call."""
        if self._parsed is None:
            self._parsed = self._parse_fn(self.content)
        return self._parsed

    def read(self) -> bytes:
        """Return the raw body bytes."""
        return self.content

    def text(self) -> str:
        """Return the raw body as a UTF-8 string."""
        return self.content.decode("utf-8", errors="replace")


class StreamingAPIResponse(Generic[T]):
    """Streaming raw response from a control-plane call.

    Use as a context manager so the underlying connection is released::

        with client.events.with_streaming_response.list() as stream:
            for chunk in stream.iter_bytes():
                ...

    Mirrors the shape of OpenAI's :class:`Stream` / Anthropic's
    ``Stream`` SSE wrapper:
      - ``consumed`` guard prevents double-iteration of the same stream
        (httpx raises ``StreamConsumed`` on its own, but a Pythonic
        message is more useful for callers).

    Attributes mirror :class:`APIResponse` minus ``content`` /
    :meth:`parse` — those are not available without consuming the stream.
    """

    http_response: Any
    status_code: int
    headers: Mapping[str, str]
    request_id: Optional[str]

    def __init__(self, http_response: Any) -> None:
        self.http_response = http_response
        self.status_code = int(getattr(http_response, "status_code", 0))
        try:
            raw_headers = http_response.headers
            self.headers = {str(k).lower(): str(v) for k, v in raw_headers.items()}
        except Exception:
            self.headers = {}
        self.request_id = (
            self.headers.get("checkrd-request-id")
            or self.headers.get("x-request-id")
        )
        self._consumed = False

    @property
    def consumed(self) -> bool:
        """Whether ``iter_bytes`` / ``iter_text`` has been called yet."""
        return self._consumed

    def _mark_consumed(self) -> None:
        if self._consumed:
            raise RuntimeError(
                "StreamingAPIResponse can only be consumed once. "
                "Re-iterating after the first read yields zero chunks; "
                "buffer the bytes if you need fanout."
            )
        self._consumed = True

    def iter_bytes(self, chunk_size: Optional[int] = None) -> Any:
        """Yield raw response chunks as ``bytes``."""
        self._mark_consumed()
        return self.http_response.iter_bytes(chunk_size=chunk_size)

    def iter_text(self, chunk_size: Optional[int] = None) -> Any:
        """Yield decoded response chunks as ``str``."""
        self._mark_consumed()
        return self.http_response.iter_text(chunk_size=chunk_size)

    def __enter__(self) -> "StreamingAPIResponse[T]":
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            self.http_response.close()
        except Exception:
            pass


__all__ = ["APIResponse", "StreamingAPIResponse"]
