"""Tests for the stable error code catalog on exceptions."""

from __future__ import annotations

from checkrd.exceptions import (
    CheckrdInitError,
    CheckrdPolicyDenied,
    _derive_deny_code,
    _derive_init_code,
)


class TestDeriveDenyCode:
    def test_named_rule(self) -> None:
        assert _derive_deny_code("denied by rule 'block-deletes'") == "policy_denied"

    def test_rate_limit(self) -> None:
        assert _derive_deny_code("rate limit 'api-calls' exceeded") == "rate_limit_exceeded"

    def test_default_policy(self) -> None:
        assert _derive_deny_code("denied by default policy") == "default_policy_denied"

    def test_kill_switch(self) -> None:
        assert _derive_deny_code("kill switch active") == "kill_switch_active"

    def test_unknown_fallback(self) -> None:
        assert _derive_deny_code("some unknown reason") == "policy_denied"


class TestDeriveInitCode:
    def test_wasm_not_found(self) -> None:
        assert _derive_init_code("WASM module not found at /path") == "wasm_not_found"

    def test_wasm_load_failed(self) -> None:
        assert _derive_init_code("Failed to instantiate WASM module") == "wasm_load_failed"

    def test_invalid_policy(self) -> None:
        assert _derive_init_code("Invalid policy JSON") == "invalid_policy"

    def test_invalid_key(self) -> None:
        assert _derive_init_code("Invalid key format") == "invalid_key"

    def test_unknown_fallback(self) -> None:
        assert _derive_init_code("Something unexpected happened") == "init_failed"


class TestCheckrdPolicyDeniedCode:
    def test_auto_derived_from_reason(self) -> None:
        exc = CheckrdPolicyDenied(
            reason="denied by rule 'my-rule'", request_id="req-1",
        )
        assert exc.code == "policy_denied"

    def test_explicit_code_overrides(self) -> None:
        exc = CheckrdPolicyDenied(
            reason="denied by rule 'my-rule'", request_id="req-1",
            code="custom_code",
        )
        assert exc.code == "custom_code"

    def test_kill_switch_code(self) -> None:
        exc = CheckrdPolicyDenied(
            reason="kill switch active", request_id="req-1",
        )
        assert exc.code == "kill_switch_active"

    def test_backward_compat_str(self) -> None:
        exc = CheckrdPolicyDenied(reason="denied", request_id="req-1")
        assert str(exc).startswith("Request req-1 denied:")

    def test_backward_compat_reason_attr(self) -> None:
        exc = CheckrdPolicyDenied(reason="denied", request_id="req-1")
        assert exc.reason == "denied"
        assert exc.request_id == "req-1"


class TestCheckrdInitErrorCode:
    def test_auto_derived_from_message(self) -> None:
        exc = CheckrdInitError("WASM module not found at /path/to/file")
        assert exc.code == "wasm_not_found"

    def test_explicit_code_overrides(self) -> None:
        exc = CheckrdInitError("something", code="custom")
        assert exc.code == "custom"

    def test_backward_compat_str(self) -> None:
        exc = CheckrdInitError("test message")
        assert str(exc) == "test message"
