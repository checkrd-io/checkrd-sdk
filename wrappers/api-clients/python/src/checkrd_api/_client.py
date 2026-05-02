"""Sync and async client classes — the public entry points.

Two distinct classes (:class:`Checkrd` and :class:`AsyncCheckrd`)
with the same resource surface; pick by program style. Mirrors the
OpenAI / Anthropic Python SDK shape exactly so callers who already
know those libraries can use this one without re-learning anything.

The classes are *thin*. They own:

- One :class:`httpx.Client` / :class:`httpx.AsyncClient`.
- The default request options (timeout, max_retries, headers).
- The retry loop.
- A few low-level dispatch methods (``_get``, ``_post``, …) that
  resource classes call into.

Resources live next to this file under ``_resources/``; new
resources just append a ``cached_property`` here.

Reference shape: ``openai-python/src/openai/_client.py`` and
``anthropic-sdk-python/src/anthropic/_client.py``.
"""
from __future__ import annotations

import json
import os
import random
import time
from functools import cached_property
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Mapping,
    Optional,
    TypeVar,
    Union,
)

import httpx

from ._exceptions import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    make_status_error,
)
from ._pagination import AsyncPage, SyncPage, _PageState

if TYPE_CHECKING:
    from ._resources.agents import Agents, AsyncAgents

#: Default base URL for the production control plane. Override via
#: the ``base_url`` constructor argument or the ``CHECKRD_BASE_URL``
#: environment variable.
DEFAULT_BASE_URL = "https://api.checkrd.io"

#: Default timeout for any single HTTP attempt. Stripe and OpenAI
#: use 60s; we match.
DEFAULT_TIMEOUT_SECS = 60.0

#: Default retry budget per call. Mirrors OpenAI's default of 2 —
#: enough to ride out a transient 5xx without doubling latency on a
#: deterministic 4xx.
DEFAULT_MAX_RETRIES = 2

#: Pinned API version. Matches the ``Checkrd-Version`` date header
#: the server's version registry expects. Bump deliberately.
DEFAULT_API_VERSION = "2026-04-15"

_T = TypeVar("_T")


class _BaseClient:
    """Shared options + sentinel parsing. Concrete clients
    (:class:`Checkrd`, :class:`AsyncCheckrd`) extend this with their
    own httpx instance and dispatch methods."""

    base_url: str
    api_key: Optional[str]
    bearer_token: Optional[str]
    timeout: float
    max_retries: int
    api_version: str
    default_headers: Mapping[str, str]

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        bearer_token: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT_SECS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        api_version: str = DEFAULT_API_VERSION,
        default_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        # Auth: API key takes precedence; both can be set so a caller
        # can rotate without restarting. At least one is required for
        # anything other than ``/health``.
        self.api_key = api_key or os.environ.get("CHECKRD_API_KEY")
        self.bearer_token = bearer_token or os.environ.get("CHECKRD_BEARER_TOKEN")
        self.base_url = (base_url or os.environ.get("CHECKRD_BASE_URL") or DEFAULT_BASE_URL).rstrip(
            "/"
        )
        self.timeout = timeout
        self.max_retries = max_retries
        self.api_version = api_version
        self.default_headers = dict(default_headers or {})

    def _build_headers(self, extra: Optional[Mapping[str, str]] = None) -> dict[str, str]:
        """Merge default + per-call headers; inject auth + version
        last so callers can't accidentally clobber them."""
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": _user_agent(),
            "Checkrd-Version": self.api_version,
        }
        headers.update(self.default_headers)
        if extra:
            headers.update(extra)
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        elif self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        return headers

    @staticmethod
    def _should_retry(status_code: int) -> bool:
        """Stripe/OpenAI dispatch table: connect errors + 408/409/429/≥500.

        Connect errors are handled by exception type in the dispatch
        loop; this method is just for HTTP statuses.
        """
        return status_code in (408, 409, 429) or status_code >= 500

    @staticmethod
    def _retry_delay(attempt: int) -> float:
        """Exponential backoff with full jitter.

        ``attempt`` is the 1-indexed try count; first retry waits
        ~0.5s, second ~1s, third ~2s, capped at 8s. The full-jitter
        term prevents thundering-herd retries.
        """
        base = min(0.5 * (2 ** (attempt - 1)), 8.0)
        return base * random.uniform(0.5, 1.0)


class Checkrd(_BaseClient):
    """Synchronous Checkrd control-plane client.

    Use for blocking scripts and CI tooling. For async / asyncio
    contexts, use :class:`AsyncCheckrd` instead — same surface.

    Example::

        from checkrd_api import Checkrd

        client = Checkrd(api_key="ck_live_…")
        for agent in client.agents.list():
            print(agent.name, agent.kill_switch_active)
    """

    _http: httpx.Client

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        bearer_token: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT_SECS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        api_version: str = DEFAULT_API_VERSION,
        default_headers: Optional[Mapping[str, str]] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        super().__init__(
            api_key=api_key,
            bearer_token=bearer_token,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            api_version=api_version,
            default_headers=default_headers,
        )
        self._http = http_client or httpx.Client(timeout=timeout)
        self._owns_http = http_client is None

    def __enter__(self) -> "Checkrd":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying httpx client. Idempotent. Safe to
        call from a finally block; ``__exit__`` calls it for you
        when used as a context manager."""
        if self._owns_http:
            self._http.close()

    def with_options(
        self,
        *,
        api_key: Optional[str] = None,
        bearer_token: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        api_version: Optional[str] = None,
        default_headers: Optional[Mapping[str, str]] = None,
    ) -> "Checkrd":
        """Return a new :class:`Checkrd` with the given overrides
        layered on top. Useful for one-off increases of
        ``max_retries`` or per-call header injection without
        mutating the long-lived client.

        Example::

            client.with_options(max_retries=5).agents.list()
        """
        merged_headers = dict(self.default_headers)
        if default_headers:
            merged_headers.update(default_headers)
        return Checkrd(
            api_key=api_key if api_key is not None else self.api_key,
            bearer_token=bearer_token if bearer_token is not None else self.bearer_token,
            base_url=base_url if base_url is not None else self.base_url,
            timeout=timeout if timeout is not None else self.timeout,
            max_retries=max_retries if max_retries is not None else self.max_retries,
            api_version=api_version if api_version is not None else self.api_version,
            default_headers=merged_headers,
        )

    # -------------------------------------------------------------
    # Resource accessors (lazy)
    # -------------------------------------------------------------

    @cached_property
    def agents(self) -> "Agents":
        from ._resources.agents import Agents

        return Agents(self)

    # -------------------------------------------------------------
    # Low-level dispatch — called by resource classes
    # -------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Optional[Any] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """Execute a single API request with retry. Returns the parsed
        JSON body on success; raises one of the
        :mod:`checkrd_api._exceptions` classes on failure.

        Resource classes are the only intended callers.
        """
        url = self.base_url + path
        headers = self._build_headers(extra_headers)
        attempts = self.max_retries + 1
        last_error: Exception = APIConnectionError(
            "no attempts made", httpx.Request(method, url)
        )
        for attempt in range(1, attempts + 1):
            try:
                response = self._http.request(
                    method,
                    url,
                    params=_clean_params(params),
                    json=json_body,
                    headers=headers,
                    timeout=timeout if timeout is not None else self.timeout,
                )
            except httpx.TimeoutException as exc:
                last_error = APITimeoutError(str(exc), httpx.Request(method, url))
                if attempt < attempts:
                    time.sleep(self._retry_delay(attempt))
                    continue
                raise last_error
            except httpx.HTTPError as exc:
                last_error = APIConnectionError(str(exc), httpx.Request(method, url))
                if attempt < attempts:
                    time.sleep(self._retry_delay(attempt))
                    continue
                raise last_error
            if 200 <= response.status_code < 300:
                if response.status_code == 204 or not response.content:
                    return None
                return response.json()
            if self._should_retry(response.status_code) and attempt < attempts:
                time.sleep(self._retry_delay(attempt))
                continue
            raise make_status_error(response, _safe_json(response))
        raise last_error  # pragma: no cover - unreachable but keeps mypy happy

    def _get(self, path: str, **kwargs: Any) -> Any:
        return self._request("GET", path, **kwargs)

    def _post(self, path: str, **kwargs: Any) -> Any:
        return self._request("POST", path, **kwargs)

    def _put(self, path: str, **kwargs: Any) -> Any:
        return self._request("PUT", path, **kwargs)

    def _delete(self, path: str, **kwargs: Any) -> Any:
        return self._request("DELETE", path, **kwargs)

    def _get_api_list(
        self,
        path: str,
        *,
        params: Mapping[str, Any],
        item_decoder: Callable[[dict], _T],
    ) -> SyncPage[_T]:
        """Issue a GET to a list endpoint and wrap the response in
        a :class:`SyncPage` that knows how to fetch subsequent
        pages on demand."""
        body = self._get(path, params=params)
        return SyncPage(
            self,
            _PageState(
                data=[item_decoder(item) for item in (body or {}).get("data", [])],
                has_more=bool((body or {}).get("has_more")),
                next_cursor=(body or {}).get("next_cursor"),
                path=path,
                params=dict(params),
                item_decoder=item_decoder,
            ),
        )


class AsyncCheckrd(_BaseClient):
    """Asynchronous Checkrd control-plane client. Identical surface
    to :class:`Checkrd` but every method is a coroutine and
    ``list()`` returns an :class:`AsyncPage` that you ``async for``
    over.

    Example::

        async with AsyncCheckrd(api_key="ck_live_…") as client:
            async for agent in client.agents.list():
                print(agent.name)
    """

    _http: httpx.AsyncClient

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        bearer_token: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT_SECS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        api_version: str = DEFAULT_API_VERSION,
        default_headers: Optional[Mapping[str, str]] = None,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        super().__init__(
            api_key=api_key,
            bearer_token=bearer_token,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            api_version=api_version,
            default_headers=default_headers,
        )
        self._http = http_client or httpx.AsyncClient(timeout=timeout)
        self._owns_http = http_client is None

    async def __aenter__(self) -> "AsyncCheckrd":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def with_options(
        self,
        *,
        api_key: Optional[str] = None,
        bearer_token: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        api_version: Optional[str] = None,
        default_headers: Optional[Mapping[str, str]] = None,
    ) -> "AsyncCheckrd":
        merged_headers = dict(self.default_headers)
        if default_headers:
            merged_headers.update(default_headers)
        return AsyncCheckrd(
            api_key=api_key if api_key is not None else self.api_key,
            bearer_token=bearer_token if bearer_token is not None else self.bearer_token,
            base_url=base_url if base_url is not None else self.base_url,
            timeout=timeout if timeout is not None else self.timeout,
            max_retries=max_retries if max_retries is not None else self.max_retries,
            api_version=api_version if api_version is not None else self.api_version,
            default_headers=merged_headers,
        )

    @cached_property
    def agents(self) -> "AsyncAgents":
        from ._resources.agents import AsyncAgents

        return AsyncAgents(self)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Optional[Any] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        import asyncio

        url = self.base_url + path
        headers = self._build_headers(extra_headers)
        attempts = self.max_retries + 1
        last_error: Exception = APIConnectionError(
            "no attempts made", httpx.Request(method, url)
        )
        for attempt in range(1, attempts + 1):
            try:
                response = await self._http.request(
                    method,
                    url,
                    params=_clean_params(params),
                    json=json_body,
                    headers=headers,
                    timeout=timeout if timeout is not None else self.timeout,
                )
            except httpx.TimeoutException as exc:
                last_error = APITimeoutError(str(exc), httpx.Request(method, url))
                if attempt < attempts:
                    await asyncio.sleep(self._retry_delay(attempt))
                    continue
                raise last_error
            except httpx.HTTPError as exc:
                last_error = APIConnectionError(str(exc), httpx.Request(method, url))
                if attempt < attempts:
                    await asyncio.sleep(self._retry_delay(attempt))
                    continue
                raise last_error
            if 200 <= response.status_code < 300:
                if response.status_code == 204 or not response.content:
                    return None
                return response.json()
            if self._should_retry(response.status_code) and attempt < attempts:
                await asyncio.sleep(self._retry_delay(attempt))
                continue
            raise make_status_error(response, _safe_json(response))
        raise last_error  # pragma: no cover

    async def _get(self, path: str, **kwargs: Any) -> Any:
        return await self._request("GET", path, **kwargs)

    async def _post(self, path: str, **kwargs: Any) -> Any:
        return await self._request("POST", path, **kwargs)

    async def _put(self, path: str, **kwargs: Any) -> Any:
        return await self._request("PUT", path, **kwargs)

    async def _delete(self, path: str, **kwargs: Any) -> Any:
        return await self._request("DELETE", path, **kwargs)

    async def _get_api_list(
        self,
        path: str,
        *,
        params: Mapping[str, Any],
        item_decoder: Callable[[dict], _T],
    ) -> AsyncPage[_T]:
        body = await self._get(path, params=params)
        return AsyncPage(
            self,
            _PageState(
                data=[item_decoder(item) for item in (body or {}).get("data", [])],
                has_more=bool((body or {}).get("has_more")),
                next_cursor=(body or {}).get("next_cursor"),
                path=path,
                params=dict(params),
                item_decoder=item_decoder,
            ),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user_agent() -> str:
    """Build the ``User-Agent`` header. Same shape OpenAI/Anthropic
    use so server-side analytics can recognize the SDK."""
    from . import __version__ as sdk_version
    import platform
    import sys

    return (
        f"checkrd-api-python/{sdk_version} "
        f"Python/{sys.version_info.major}.{sys.version_info.minor} "
        f"({platform.system()})"
    )


def _clean_params(params: Optional[Mapping[str, Any]]) -> Optional[dict]:
    """Drop ``None`` values so ``?cursor=&limit=20`` doesn't become
    ``?cursor=None&limit=20`` in the URL."""
    if params is None:
        return None
    return {k: v for k, v in params.items() if v is not None}


def _safe_json(response: httpx.Response) -> Optional[Mapping[str, Any]]:
    """Best-effort JSON parse for error response bodies. Returns
    ``None`` when the body isn't JSON; callers fall back to the
    status-code-derived message."""
    try:
        return response.json()
    except (json.JSONDecodeError, ValueError):
        return None
