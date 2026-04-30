"""Tests for request lifecycle hooks (on_deny, on_allow, before_request).

Uses ``mock_wrap()`` from ``checkrd.testing`` so these tests are WASM-free
and fast. The hooks fire inside the real ``CheckrdTransport``, so they
exercise the exact code path production uses.
"""

from __future__ import annotations

from typing import Optional
from unittest.mock import MagicMock

import httpx
import pytest

from checkrd.exceptions import CheckrdPolicyDenied
from checkrd.hooks import CheckrdEvent
from checkrd.testing import mock_wrap, mock_wrap_async

DENY_RULES = [{"name": "block-deletes", "deny": {"method": ["DELETE"], "url": "*"}}]


def _mock_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


# ============================================================
# on_deny
# ============================================================


class TestOnDeny:
    def test_on_deny_called_on_denied_request(self) -> None:
        hook = MagicMock()
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(client, rules=DENY_RULES, on_deny=hook)

            with pytest.raises(CheckrdPolicyDenied):
                client.delete("https://api.example.com/resource")

            hook.assert_called_once()
            event = hook.call_args[0][0]
            assert isinstance(event, CheckrdEvent)
            assert event.allowed is False
            assert event.method == "DELETE"

    def test_on_deny_receives_rule_name(self) -> None:
        hook = MagicMock()
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(client, rules=DENY_RULES, on_deny=hook)

            with pytest.raises(CheckrdPolicyDenied):
                client.delete("https://api.example.com")

            event = hook.call_args[0][0]
            assert event.rule_name == "block-deletes"
            assert event.deny_reason is not None
            assert event.suggestion is not None

    def test_on_deny_not_called_on_allowed_request(self) -> None:
        hook = MagicMock()
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(client, default="allow", on_deny=hook)

            client.get("https://api.example.com")
            hook.assert_not_called()

    def test_on_deny_fires_in_dry_run_mode(self) -> None:
        hook = MagicMock()
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(client, rules=DENY_RULES, enforce=False, on_deny=hook)

            # In dry-run, the request goes through but on_deny still fires.
            response = client.delete("https://api.example.com")
            assert response.status_code == 200
            hook.assert_called_once()

    def test_on_deny_exception_does_not_crash_request(self) -> None:
        hook = MagicMock(side_effect=RuntimeError("hook crashed"))
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(client, rules=DENY_RULES, on_deny=hook)

            # The hook crashes but the deny still raises normally.
            with pytest.raises(CheckrdPolicyDenied):
                client.delete("https://api.example.com")
            hook.assert_called_once()


# ============================================================
# on_allow
# ============================================================


class TestOnAllow:
    def test_on_allow_called_on_allowed_request(self) -> None:
        hook = MagicMock()
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(client, default="allow", on_allow=hook)

            client.get("https://api.example.com/resource")

            hook.assert_called_once()
            event = hook.call_args[0][0]
            assert isinstance(event, CheckrdEvent)
            assert event.allowed is True

    def test_on_allow_not_called_on_denied_request(self) -> None:
        hook = MagicMock()
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(client, default="deny", on_allow=hook)

            with pytest.raises(CheckrdPolicyDenied):
                client.get("https://api.example.com")
            hook.assert_not_called()

    def test_on_allow_exception_does_not_crash_request(self) -> None:
        hook = MagicMock(side_effect=RuntimeError("hook crashed"))
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(client, default="allow", on_allow=hook)

            response = client.get("https://api.example.com")
            assert response.status_code == 200
            hook.assert_called_once()


# ============================================================
# before_request
# ============================================================


class TestBeforeRequest:
    def test_before_request_returning_event_proceeds_normally(self) -> None:
        def hook(event: CheckrdEvent) -> Optional[CheckrdEvent]:
            return event
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(client, default="deny", before_request=hook)

            # Policy says deny, but before_request returned the event so
            # evaluation proceeds -> denied.
            with pytest.raises(CheckrdPolicyDenied):
                client.get("https://api.example.com")

    def test_before_request_returning_none_skips_evaluation(self) -> None:
        def hook(event: CheckrdEvent) -> None:
            return None  # skip evaluation
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(client, default="deny", before_request=hook)

            # Policy says deny, but before_request returned None -> pass-through.
            response = client.get("https://api.example.com")
            assert response.status_code == 200

    def test_before_request_receives_request_info(self) -> None:
        captured: list[CheckrdEvent] = []

        def hook(event: CheckrdEvent) -> CheckrdEvent:
            captured.append(event)
            return event
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(client, default="allow", before_request=hook)

            client.get("https://api.example.com/resource")

            assert len(captured) == 1
            assert captured[0].method == "GET"
            assert "api.example.com" in captured[0].url
            assert captured[0].allowed is None  # not yet evaluated

    def test_before_request_exception_does_not_crash(self) -> None:
        def hook(event: CheckrdEvent) -> CheckrdEvent:
            raise RuntimeError("hook crashed")
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(client, default="allow", before_request=hook)

            # Hook crashes but the request proceeds normally.
            response = client.get("https://api.example.com")
            assert response.status_code == 200


# ============================================================
# Async hooks
# ============================================================


class TestAsyncHooks:
    @pytest.mark.asyncio
    async def test_async_on_deny(self) -> None:
        hook = MagicMock()
        async with httpx.AsyncClient(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap_async(client, rules=DENY_RULES, on_deny=hook)
            with pytest.raises(CheckrdPolicyDenied):
                await client.delete("https://api.example.com")
        hook.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_on_allow(self) -> None:
        hook = MagicMock()
        async with httpx.AsyncClient(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap_async(client, default="allow", on_allow=hook)
            await client.get("https://api.example.com")
        hook.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_before_request_none_skips(self) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap_async(
                client, default="deny",
                before_request=lambda e: None,
            )
            response = await client.get("https://api.example.com")
            assert response.status_code == 200


# ============================================================
# Combined hooks
# ============================================================


class TestCombinedHooks:
    def test_all_hooks_on_allowed_request(self) -> None:
        before = MagicMock(side_effect=lambda e: e)
        allow = MagicMock()
        deny = MagicMock()
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(
                client, default="allow",
                before_request=before, on_allow=allow, on_deny=deny,
            )

            client.get("https://api.example.com")

            before.assert_called_once()
            allow.assert_called_once()
            deny.assert_not_called()

    def test_all_hooks_on_denied_request(self) -> None:
        before = MagicMock(side_effect=lambda e: e)
        allow = MagicMock()
        deny = MagicMock()
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(
                client, rules=DENY_RULES,
                before_request=before, on_allow=allow, on_deny=deny,
            )

            with pytest.raises(CheckrdPolicyDenied):
                client.delete("https://api.example.com")

            before.assert_called_once()
            deny.assert_called_once()
            allow.assert_not_called()
