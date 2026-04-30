"""Shared test fixtures and helpers for Checkrd tests."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Iterator, Optional
from unittest.mock import Mock

import httpx
import pytest

import checkrd.engine as _engine_mod
from checkrd.engine import EvalResult, WasmEngine

WASM_PATH = Path(__file__).parent.parent / "src" / "checkrd" / "checkrd_core.wasm"

requires_wasm = pytest.mark.skipif(
    not WASM_PATH.exists(),
    reason="checkrd_core.wasm not found. Run build-wasm.sh && copy-wasm.sh first.",
)


def wait_for(condition: Any, *, timeout: float = 5.0, poll: float = 0.01) -> None:
    """Poll until ``condition()`` is truthy, or raise after *timeout* seconds.

    Replaces fragile ``time.sleep(N)`` patterns in timing-dependent tests.
    Succeeds immediately when the condition is met (fast on normal runs)
    and tolerates slow CI environments with a generous deadline.

    Usage::

        from tests.conftest import wait_for

        wait_for(lambda: mock.call_count >= 1)
        wait_for(lambda: thread.is_alive(), timeout=10)
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(poll)
    raise AssertionError(f"condition not met within {timeout}s")


def unique_id() -> str:
    """Short unique identifier for test data isolation.

    Mirrors the Rust ``unique_id()`` helper. Use this anywhere a test creates
    data that could collide with parallel runs (agent IDs, org names, file
    paths). 8 hex chars give 16^8 = 4.3 billion combinations -- collision odds
    are negligible even at thousands of tests per run.

    Usage::

        agent_id = unique_id()
        org_name = f"TestOrg-{unique_id()}"
    """
    return uuid.uuid4().hex[:8]


def unique_uuid() -> str:
    """Unique UUID string for test data isolation.

    Use this when a test needs a full UUID (e.g., agent_id field that must be
    a valid UUID format). For non-UUID fields, prefer ``unique_id()``.
    """
    return str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _reset_rate_limit_filter() -> Iterator[None]:
    """Reset the rate-limit filter's seen dict between tests.

    The RateLimitFilter suppresses duplicate log messages per call site.
    Without this reset, test B might not see a log message that test A
    already triggered from the same call site within the rate limit window.
    """
    import logging

    checkrd_logger = logging.getLogger("checkrd")
    for f in checkrd_logger.filters:
        if hasattr(f, "_seen"):
            f._seen.clear()
    yield
    for f in checkrd_logger.filters:
        if hasattr(f, "_seen"):
            f._seen.clear()


@pytest.fixture(autouse=True)
def _dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Relax production-only checks for the test environment.

    Tests routinely use ``http://localhost`` URLs and may run without
    ``_wasm_integrity.py``. Both of these trigger hard errors in
    production mode. We set both new-style flags here:

    - ``CHECKRD_ALLOW_INSECURE_HTTP=1`` accepts HTTP control-plane URLs.
    - ``CHECKRD_SKIP_WASM_INTEGRITY=1`` treats a missing integrity hash
      as a warning, not a fatal error.

    We intentionally avoid the deprecated ``CHECKRD_DEV=1`` alias — it
    would emit a DeprecationWarning on every test, which collides with
    any test that sets ``warnings.filterwarnings("error", ...)``.

    Tests that exercise production-mode behavior (``TestWasmIntegrity``,
    ``TestValidateUrl``) explicitly control these vars via ``monkeypatch``
    — pytest's inner setenv/delenv scoping wins over this default.
    """
    monkeypatch.setenv("CHECKRD_ALLOW_INSECURE_HTTP", "1")
    monkeypatch.setenv("CHECKRD_SKIP_WASM_INTEGRITY", "1")


@pytest.fixture(autouse=True)
def _guard_wasm_cache() -> Iterator[None]:
    """Prevent WASM module cache mutations from leaking between tests.

    Saves the global cache state before each test and restores it afterward.
    This is a no-op for tests that don't touch the cache, but protects against
    tests that monkeypatch ``_cached_module`` or ``_cached_wasm_engine`` without
    proper cleanup -- which would silently break subsequent tests.
    """
    saved_engine = _engine_mod._cached_wasm_engine
    saved_module = _engine_mod._cached_module
    yield
    _engine_mod._cached_wasm_engine = saved_engine
    _engine_mod._cached_module = saved_module


SAMPLE_POLICY = {
    "agent": "test-agent",
    "default": "deny",
    "rules": [
        {
            "name": "allow-get-stripe",
            "allow": {
                "method": ["GET"],
                "url": "api.stripe.com/v1/charges",
            },
        },
        {
            "name": "block-deletes",
            "deny": {
                "method": ["DELETE"],
                "url": "*",
            },
        },
    ],
}

ALLOW_ALL_POLICY = {
    "agent": "test-agent",
    "default": "allow",
    "rules": [],
}


@pytest.fixture()
def policy_json() -> str:
    return json.dumps(SAMPLE_POLICY)


@pytest.fixture()
def allow_all_policy_json() -> str:
    return json.dumps(ALLOW_ALL_POLICY)


# ============================================================
# Mock engine + transport fixtures
# ============================================================


def make_mock_engine(
    *,
    allowed: bool = True,
    deny_reason: Optional[str] = None,
) -> Mock:
    """Create a mock WasmEngine with configurable behavior.

    Use directly for custom configurations. For common cases, prefer the
    ``mock_engine_allowed`` / ``mock_engine_denied`` fixtures.
    """
    engine = Mock(spec=WasmEngine)
    engine.evaluate.return_value = EvalResult(
        allowed=allowed,
        deny_reason=deny_reason,
        telemetry_json="{}",
        request_id="req-001",
    )
    return engine


@pytest.fixture()
def mock_engine_allowed() -> Mock:
    """A mock WasmEngine that allows all requests."""
    return make_mock_engine(allowed=True)


@pytest.fixture()
def mock_engine_denied() -> Mock:
    """A mock WasmEngine that denies all requests."""
    return make_mock_engine(allowed=False, deny_reason="blocked by policy")


@pytest.fixture()
def mock_transport() -> Mock:
    """A mock sync httpx transport that returns 200."""
    transport = Mock(spec=httpx.BaseTransport)
    transport.handle_request.return_value = httpx.Response(200)
    return transport


@pytest.fixture()
def mock_async_transport() -> Mock:
    """A mock async httpx transport that returns 200."""
    transport = Mock(spec=httpx.AsyncBaseTransport)
    transport.handle_async_request.return_value = httpx.Response(200)
    return transport
