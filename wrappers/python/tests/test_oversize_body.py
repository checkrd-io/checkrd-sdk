"""Oversize-body guard: the 1 MB WASM inspection limit must not become a
policy-bypass vector. Strict mode denies; permissive mode allows with warning.
"""

from __future__ import annotations

import logging

import httpx

from checkrd.exceptions import CheckrdPolicyDenied
from checkrd.transports._httpx import (
    MAX_BODY_SIZE,
    _OVERSIZE_BODY_DENY_REASON,
    _check_oversized_body,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_request(size_bytes: int) -> httpx.Request:
    """httpx.Request with a body of the given size."""
    return httpx.Request(
        "POST",
        "https://api.example.com/v1/chat",
        content=b"x" * size_bytes,
        headers={"content-type": "application/octet-stream"},
    )


# ---------------------------------------------------------------------------
# Direct helper tests — easier to reason about than a full transport mock.
# ---------------------------------------------------------------------------


def test_small_body_passes_through():
    request = _build_request(1024)  # 1 KB
    assert _check_oversized_body(
        request, "strict", enforce=True,
        agent_id="a", dashboard_url="", batcher=None, on_deny=None,
    ) is None


def test_exactly_max_body_size_passes_through():
    """Boundary: <= MAX_BODY_SIZE is inspected, > is rejected."""
    request = _build_request(MAX_BODY_SIZE)
    assert _check_oversized_body(
        request, "strict", enforce=True,
        agent_id="a", dashboard_url="", batcher=None, on_deny=None,
    ) is None


def test_oversize_body_strict_returns_deny():
    """Strict + enforce must DENY, not silently skip body matching.

    Silent skipping lets an attacker pad the payload with filler to
    evade body-matcher rules — the inverse of the policy engine's
    purpose. Fail-closed is the only acceptable default here."""
    request = _build_request(MAX_BODY_SIZE + 1)
    result = _check_oversized_body(
        request, "strict", enforce=True,
        agent_id="a", dashboard_url="", batcher=None, on_deny=None,
    )
    assert isinstance(result, CheckrdPolicyDenied)
    assert result.reason == _OVERSIZE_BODY_DENY_REASON
    assert result.request_id  # every deny carries an ID


def test_oversize_body_permissive_passes_through_with_warning(caplog):
    """Permissive mode (the rollout opt-in) logs a warning and returns
    None. The request will proceed with body=None — body matchers won't
    apply. This is documented and the warning makes it observable."""
    request = _build_request(MAX_BODY_SIZE + 1)
    with caplog.at_level(logging.WARNING, logger="checkrd"):
        result = _check_oversized_body(
            request, "permissive", enforce=True,
            agent_id="a", dashboard_url="", batcher=None, on_deny=None,
        )
    assert result is None
    assert any("will NOT be applied" in r.message for r in caplog.records)


def test_oversize_body_strict_but_not_enforcing_passes_through():
    """Dry-run (enforce=False) is for observation — a would-deny must not
    actually block the request even in strict mode, matching the overall
    semantic of enforce=False."""
    request = _build_request(MAX_BODY_SIZE + 1)
    result = _check_oversized_body(
        request, "strict", enforce=False,
        agent_id="a", dashboard_url="", batcher=None, on_deny=None,
    )
    assert result is None


def test_oversize_telemetry_emitted_on_strict_deny():
    """Strict denies must still produce a telemetry row so the block is
    visible in the dashboard like any other policy deny."""
    events: list[dict] = []

    class FakeBatcher:
        def enqueue(self, event):
            events.append(event)

    request = _build_request(MAX_BODY_SIZE + 512)
    result = _check_oversized_body(
        request, "strict", enforce=True,
        agent_id="a", dashboard_url="", batcher=FakeBatcher(), on_deny=None,
    )
    assert result is not None
    assert len(events) == 1
    event = events[0]
    assert event["policy_result"] == "denied"
    assert _OVERSIZE_BODY_DENY_REASON in event["deny_reason"]
    assert event["request_id"] == result.request_id


def test_oversize_on_deny_hook_invoked():
    calls: list[dict] = []
    request = _build_request(MAX_BODY_SIZE + 1)
    _check_oversized_body(
        request, "strict", enforce=True,
        agent_id="a", dashboard_url="", batcher=None,
        on_deny=lambda ev: calls.append(ev),
    )
    assert len(calls) == 1
    assert calls[0]["policy_result"] == "denied"
    # Hook event must NOT carry credential headers (sanitized upstream).
    header_names = {k.lower() for k, _ in calls[0]["headers"]}
    assert "authorization" not in header_names


def test_oversize_hook_exception_does_not_break_deny(caplog):
    """If the user hook raises, we still return the deny. No silent pass."""
    def broken_hook(_ev):
        raise RuntimeError("hook bug")

    request = _build_request(MAX_BODY_SIZE + 1)
    with caplog.at_level(logging.WARNING, logger="checkrd"):
        result = _check_oversized_body(
            request, "strict", enforce=True,
            agent_id="a", dashboard_url="", batcher=None,
            on_deny=broken_hook,
        )
    assert isinstance(result, CheckrdPolicyDenied)
