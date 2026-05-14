"""Server-canonical policy bootstrap tests (Python SDK).

Mirrors `wrappers/javascript/tests/policy_bootstrap.test.ts`. Locks in
the industry-standard pattern:

  - ``init()`` / ``wrap()`` refuse `policy= + api_key` in production
    unless ``CHECKRD_ALLOW_LOCAL_POLICY=1`` is set (OPA / Envoy /
    LaunchDarkly style).
  - When ``api_key`` is configured + no local policy, the SDK fetches
    the signed bundle from ``GET /v1/agents/:id/control/state`` and
    installs it before returning.
  - When the fetch fails or the server returns no bundle, the engine
    stays on the deny-all baseline so every request fails closed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest

import checkrd
from checkrd.exceptions import CheckrdInitError
from tests.conftest import requires_wasm

ALLOW_ALL_LOCAL: dict[str, Any] = {
    "agent": "t",
    "mode": "enforce",
    "default": "allow",
    "rules": [],
}


class TestDevProdGate:
    """init() / wrap() refuse `policy= + api_key` without CHECKRD_ALLOW_LOCAL_POLICY=1."""

    def test_wrap_refuses_policy_plus_api_key_without_dev_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The autouse fixture sets CHECKRD_ALLOW_LOCAL_POLICY=1; clear it
        # here so the gate fires.
        monkeypatch.delenv("CHECKRD_ALLOW_LOCAL_POLICY", raising=False)
        with pytest.raises(CheckrdInitError, match="policy="):
            with httpx.Client() as client:
                checkrd.wrap(
                    client,
                    agent_id="a",
                    policy=ALLOW_ALL_LOCAL,
                    api_key="ck_live_x",
                    control_plane_url="https://api.example.test",
                )

    @requires_wasm
    def test_wrap_accepts_policy_plus_api_key_with_dev_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # autouse fixture already sets CHECKRD_ALLOW_LOCAL_POLICY=1
        with httpx.Client() as client:
            checkrd.wrap(
                client,
                agent_id="a",
                policy=ALLOW_ALL_LOCAL,
                api_key="ck_live_x",
                control_plane_url="https://api.example.test",
            )

    @requires_wasm
    def test_wrap_accepts_policy_alone_in_pure_local_mode(self) -> None:
        # No api_key -> pure-local mode, gate doesn't fire.
        with httpx.Client() as client:
            checkrd.wrap(client, agent_id="a", policy=ALLOW_ALL_LOCAL)


class TestServerCanonicalBootstrap:
    """initAsync-style bootstrap path: fetch + install on connect."""

    @requires_wasm
    def test_bootstrap_fetches_control_state_when_api_key_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CHECKRD_ALLOW_LOCAL_POLICY", raising=False)
        calls: list[str] = []

        def fake_get(self: Any, url: str, **_: Any) -> httpx.Response:
            calls.append(url)
            return httpx.Response(
                200,
                json={"kill_switch_active": False, "policy_envelope": None},
            )

        with patch.object(httpx.Client, "get", new=fake_get):
            with httpx.Client() as client:
                # No `policy=`, api_key configured -> bootstrap fires.
                checkrd.wrap(
                    client,
                    agent_id="boot-test-agent",
                    api_key="ck_live_bootstrap_test",
                    control_plane_url="https://api.example.test",
                )

        state_calls = [u for u in calls if "/control/state" in u]
        assert len(state_calls) >= 1
        assert "boot-test-agent/control/state" in state_calls[0]

    @requires_wasm
    def test_stays_on_deny_baseline_when_no_bundle_published(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CHECKRD_ALLOW_LOCAL_POLICY", raising=False)

        def fake_get(self: Any, url: str, **_: Any) -> httpx.Response:
            return httpx.Response(
                200,
                json={"kill_switch_active": False, "policy_envelope": None},
            )

        with patch.object(httpx.Client, "get", new=fake_get):
            with httpx.Client() as client:
                # Should not raise — the SDK degrades gracefully.
                checkrd.wrap(
                    client,
                    agent_id="agent-no-policy",
                    api_key="ck_live_x",
                    control_plane_url="https://api.example.test",
                )

    @requires_wasm
    def test_stays_on_deny_baseline_when_control_state_404(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CHECKRD_ALLOW_LOCAL_POLICY", raising=False)

        def fake_get(self: Any, url: str, **_: Any) -> httpx.Response:
            return httpx.Response(404, text="not found")

        with patch.object(httpx.Client, "get", new=fake_get):
            with httpx.Client() as client:
                checkrd.wrap(
                    client,
                    agent_id="agent-404",
                    api_key="ck_live_x",
                    control_plane_url="https://api.example.test",
                )

    @requires_wasm
    def test_skips_bootstrap_in_pure_local_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No api_key -> no bootstrap call.
        calls: list[str] = []

        def fake_get(self: Any, url: str, **_: Any) -> httpx.Response:
            calls.append(url)
            return httpx.Response(200, json={})

        with patch.object(httpx.Client, "get", new=fake_get):
            with httpx.Client() as client:
                checkrd.wrap(client, agent_id="local-only", policy=ALLOW_ALL_LOCAL)

        state_calls = [u for u in calls if "/control/state" in u]
        assert len(state_calls) == 0
