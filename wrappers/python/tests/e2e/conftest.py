"""Shared fixtures for the staging-canary tests.

The skip predicate is the contract: every test depends on
``staging_url`` and ``staging_api_key`` (both fixtures), and the
fixtures call ``pytest.skip`` if the corresponding env var is unset.
That makes ``pytest tests/e2e`` a no-op in any environment that
hasn't opted in — including PR CI, contributor laptops, and
release-build workflows.
"""

from __future__ import annotations

import os
from typing import Iterator

import pytest


@pytest.fixture(scope="session")
def staging_url() -> str:
    """Resolve the staging control-plane base URL.

    Pytest skips the entire test when the env var is missing — no
    incidentally-running canary against ``api.checkrd.io`` (production)
    even if a contributor exports ``CHECKRD_API_KEY`` for unrelated
    work. Production canaries belong in a separate test directory
    with stricter guards.
    """
    url = os.environ.get("CHECKRD_STAGING_URL")
    if not url:
        pytest.skip("CHECKRD_STAGING_URL not set; skipping staging canary")
    return url


@pytest.fixture(scope="session")
def staging_api_key() -> str:
    """Resolve the staging API key. Skips when unset.

    A separate variable from ``CHECKRD_STAGING_URL`` so a smoke run
    can hit a public ``/health`` endpoint without leaking a secret
    into a misconfigured CI log.
    """
    key = os.environ.get("CHECKRD_STAGING_API_KEY")
    if not key:
        pytest.skip("CHECKRD_STAGING_API_KEY not set; skipping staging canary")
    return key


@pytest.fixture
def staging_agent_id() -> Iterator[str]:
    """Generate a unique agent ID per test run.

    Per-run isolation prevents two parallel canary jobs (one in CI,
    one a contributor's local debug) from racing on the same ID — the
    control plane scopes most state by ``agent_id``, so collisions
    look like real regressions in the SSE / telemetry counters.
    """
    import uuid

    agent_id = f"canary-{uuid.uuid4().hex[:8]}"
    yield agent_id
