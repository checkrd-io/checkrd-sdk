"""Platform / runtime / SDK-version headers for control-plane requests.

Mirrors the JS SDK's ``_platform.ts`` and the ``X-Stainless-*`` header
family shipped by the OpenAI and Anthropic SDKs. The ``X-Checkrd-SDK-*``
prefix keeps the headers clearly namespaced so ingestion can grep them
apart from the underlying HTTP library's headers.

Why they matter:
  - An operator rolling out an SDK upgrade can see "we still have old
    (<0.3.0) clients calling" from the dashboard, without asking every
    service owner to upgrade (Stripe / OpenAI pattern).
  - Python-version-specific bugs (e.g. "3.9 users see this, 3.12 does
    not") surface without per-customer forensics.
  - Supply-chain incidents — a compromised transitive dep that flips
    ``X-Checkrd-SDK-Lang`` to a wrong value — are visible immediately.

Detection never raises. Any field we can't resolve sends as
``"unknown"`` rather than being omitted so ingestion can distinguish
"older SDK that didn't send the header" from "newer SDK that couldn't
detect the field".
"""

from __future__ import annotations

import platform
import sys
import uuid
from dataclasses import dataclass
from typing import Dict, Optional

from checkrd._version import __version__


def new_idempotency_key() -> str:
    """Generate a fresh ``Idempotency-Key`` value for a control-plane POST.

    Mirrors the JS SDK's ``newIdempotencyKey()``. Format: ``checkrd-<uuid4>``.

    **Generate once per logical operation, then reuse across retries.**
    The Stripe convention is the same key on every attempt of the same
    request so the control plane can dedupe — a fresh key per attempt
    defeats the entire point of idempotency. The :class:`TelemetryBatcher`
    and the public-key registrar both follow this pattern: one key
    captured outside the retry loop, reused on every attempt.
    """
    return f"checkrd-{uuid.uuid4()}"


@dataclass(frozen=True)
class PlatformInfo:
    """Snapshot of detected platform information.

    Computed once per process via :func:`platform_info` and memoized —
    nothing in this snapshot changes at runtime within a single process,
    so the telemetry hot path should not pay for re-detection.

    Attributes:
        lang: Language identifier — always ``"python"`` for this SDK.
        sdk_version: Package version (``__version__``).
        runtime: Python implementation (``"cpython"``, ``"pypy"``, …).
        runtime_version: Runtime version (e.g. ``"3.12.0"``).
        os: OS family (``"darwin"``, ``"linux"``, ``"windows"``).
        arch: CPU architecture (``"arm64"``, ``"x86_64"``, …).
    """

    lang: str
    sdk_version: str
    runtime: str
    runtime_version: str
    os: str
    arch: str


_cached: Optional[PlatformInfo] = None


def _detect() -> PlatformInfo:
    """Detect the current Python runtime. Cheap; never raises."""
    try:
        impl = platform.python_implementation().lower()
    except Exception:
        impl = "unknown"
    try:
        impl_version = platform.python_version()
    except Exception:
        impl_version = "unknown"
    try:
        # `sys.platform` is canonical for cross-ref with CI matrix names.
        os_name = sys.platform.lower() or "unknown"
    except Exception:
        os_name = "unknown"
    try:
        arch = platform.machine().lower() or "unknown"
    except Exception:
        arch = "unknown"
    return PlatformInfo(
        lang="python",
        sdk_version=__version__,
        runtime=impl,
        runtime_version=impl_version,
        os=os_name,
        arch=arch,
    )


def platform_info() -> PlatformInfo:
    """Return the memoized :class:`PlatformInfo` snapshot."""
    global _cached  # noqa: PLW0603
    if _cached is None:
        _cached = _detect()
    return _cached


def _reset_platform_info_for_testing() -> None:
    """Reset the memoized snapshot. Test-only hook."""
    global _cached  # noqa: PLW0603
    _cached = None


def platform_headers(info: Optional[PlatformInfo] = None) -> Dict[str, str]:
    """Return the ``X-Checkrd-SDK-*`` header set for a control-plane request.

    All six headers are always included. Missing fields are sent as
    ``"unknown"`` so ingestion can distinguish the older-SDK case from
    the can't-detect case.

    Args:
        info: Optional override — primarily for testing. Defaults to
            :func:`platform_info`.
    """
    if info is None:
        info = platform_info()
    return {
        "X-Checkrd-SDK-Lang": info.lang,
        "X-Checkrd-SDK-Version": info.sdk_version,
        "X-Checkrd-SDK-Runtime": info.runtime,
        "X-Checkrd-SDK-Runtime-Version": info.runtime_version,
        "X-Checkrd-SDK-OS": info.os,
        "X-Checkrd-SDK-Arch": info.arch,
    }


def default_control_headers(
    api_key: str,
    *,
    api_version: str = "",
    idempotency_key: Optional[str] = None,
    content_type: str = "application/json",
) -> Dict[str, str]:
    """Standard header set for a Checkrd control-plane request.

    Consolidated so the telemetry batcher, public-key registrar, and
    SSE receiver all send identical metadata — operators looking at
    ingestion logs see one consistent shape.

    Always-on:
        - ``Content-Type`` (default ``application/json``; pass ``""`` to omit)
        - ``X-API-Key``
        - ``User-Agent: checkrd-python/<version>``
        - ``X-Checkrd-SDK-*`` family

    Optional:
        - ``Checkrd-Version`` when ``api_version`` is a non-empty string
          (Stripe-style date pin).
        - ``Idempotency-Key`` when ``idempotency_key`` is provided —
          callers generate and reuse a UUID per retry loop, so this
          helper does NOT auto-generate one (unlike the JS side, which
          does fresh-per-call because the JS retry loop captures the
          header set once up front).
    """
    headers: Dict[str, str] = {
        "X-API-Key": api_key,
        "User-Agent": f"checkrd-python/{__version__}",
    }
    if content_type:
        headers["Content-Type"] = content_type
    headers.update(platform_headers())
    if api_version:
        headers["Checkrd-Version"] = api_version
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers
