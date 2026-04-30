"""Tests for checkrd._trust — trusted policy signing key resolution."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from checkrd._trust import _PRODUCTION_TRUSTED_KEYS, trusted_policy_keys


class TestTrustedPolicyKeys:
    """trusted_policy_keys() resolves the trusted key list from env or production."""

    def test_returns_production_keys_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CHECKRD_POLICY_TRUST_OVERRIDE_JSON", raising=False)
        monkeypatch.delenv("CHECKRD_ALLOW_TRUST_OVERRIDE", raising=False)
        result = trusted_policy_keys()
        assert result == _PRODUCTION_TRUSTED_KEYS

    def test_returns_copy_not_reference(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mutating the returned list must not affect future calls."""
        monkeypatch.delenv("CHECKRD_POLICY_TRUST_OVERRIDE_JSON", raising=False)
        monkeypatch.delenv("CHECKRD_ALLOW_TRUST_OVERRIDE", raising=False)
        first = trusted_policy_keys()
        first.append({"keyid": "mutant"})
        second = trusted_policy_keys()
        assert {"keyid": "mutant"} not in second


class TestTrustOverrideDoubleGate:
    """The trust override requires BOTH env vars to be set (defense-in-depth)."""

    def test_override_without_gate_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Setting the override JSON without the gate returns production keys."""
        override = [{"keyid": "rogue", "public_key_hex": "a" * 64}]
        monkeypatch.setenv("CHECKRD_POLICY_TRUST_OVERRIDE_JSON", json.dumps(override))
        monkeypatch.delenv("CHECKRD_ALLOW_TRUST_OVERRIDE", raising=False)
        result = trusted_policy_keys()
        assert result == _PRODUCTION_TRUSTED_KEYS

    def test_override_without_gate_logs_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Missing gate should produce a clear warning."""
        monkeypatch.setenv("CHECKRD_POLICY_TRUST_OVERRIDE_JSON", '[{"keyid":"x"}]')
        monkeypatch.delenv("CHECKRD_ALLOW_TRUST_OVERRIDE", raising=False)
        with caplog.at_level(logging.WARNING, logger="checkrd"):
            trusted_policy_keys()
        assert any("CHECKRD_ALLOW_TRUST_OVERRIDE" in r.message for r in caplog.records)

    def test_override_with_wrong_gate_value_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Gate must be exactly '1', 'true', or 'yes'."""
        monkeypatch.setenv("CHECKRD_POLICY_TRUST_OVERRIDE_JSON", '[{"keyid":"x"}]')
        monkeypatch.setenv("CHECKRD_ALLOW_TRUST_OVERRIDE", "0")
        assert trusted_policy_keys() == _PRODUCTION_TRUSTED_KEYS

        monkeypatch.setenv("CHECKRD_ALLOW_TRUST_OVERRIDE", "false")
        assert trusted_policy_keys() == _PRODUCTION_TRUSTED_KEYS

        monkeypatch.setenv("CHECKRD_ALLOW_TRUST_OVERRIDE", "")
        assert trusted_policy_keys() == _PRODUCTION_TRUSTED_KEYS

    @pytest.mark.parametrize("gate_value", ["1", "true", "yes"])
    def test_override_with_valid_gate_applies(
        self, monkeypatch: pytest.MonkeyPatch, gate_value: str
    ) -> None:
        override: list[dict[str, Any]] = [
            {
                "keyid": "test-key-1",
                "public_key_hex": "a" * 64,
                "valid_from": 0,
                "valid_until": 9999999999,
            }
        ]
        monkeypatch.setenv("CHECKRD_POLICY_TRUST_OVERRIDE_JSON", json.dumps(override))
        monkeypatch.setenv("CHECKRD_ALLOW_TRUST_OVERRIDE", gate_value)
        result = trusted_policy_keys()
        assert result == override

    def test_override_with_gate_logs_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Active override should warn so operators notice it in logs."""
        monkeypatch.setenv("CHECKRD_POLICY_TRUST_OVERRIDE_JSON", '[{"keyid":"x"}]')
        monkeypatch.setenv("CHECKRD_ALLOW_TRUST_OVERRIDE", "1")
        with caplog.at_level(logging.WARNING, logger="checkrd"):
            trusted_policy_keys()
        assert any("DO NOT use this in production" in r.message for r in caplog.records)

    def test_override_with_multiple_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        override: list[dict[str, Any]] = [
            {
                "keyid": "key-v1",
                "public_key_hex": "a" * 64,
                "valid_from": 1000000000,
                "valid_until": 1700000000,
            },
            {
                "keyid": "key-v2",
                "public_key_hex": "b" * 64,
                "valid_from": 1600000000,
                "valid_until": 2000000000,
            },
        ]
        monkeypatch.setenv("CHECKRD_POLICY_TRUST_OVERRIDE_JSON", json.dumps(override))
        monkeypatch.setenv("CHECKRD_ALLOW_TRUST_OVERRIDE", "1")
        result = trusted_policy_keys()
        assert len(result) == 2
        assert result[0]["keyid"] == "key-v1"
        assert result[1]["keyid"] == "key-v2"

    def test_override_empty_list_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Empty override list is valid but dangerous — warn about it."""
        monkeypatch.setenv("CHECKRD_POLICY_TRUST_OVERRIDE_JSON", "[]")
        monkeypatch.setenv("CHECKRD_ALLOW_TRUST_OVERRIDE", "1")
        with caplog.at_level(logging.WARNING, logger="checkrd"):
            result = trusted_policy_keys()
        assert result == []
        assert any("empty list" in r.message for r in caplog.records)

    def test_invalid_json_falls_back_to_production(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("CHECKRD_POLICY_TRUST_OVERRIDE_JSON", "not-valid-json{{{")
        monkeypatch.setenv("CHECKRD_ALLOW_TRUST_OVERRIDE", "1")
        with caplog.at_level(logging.WARNING, logger="checkrd"):
            result = trusted_policy_keys()
        assert result == _PRODUCTION_TRUSTED_KEYS
        assert any("not valid JSON" in r.message for r in caplog.records)

    def test_non_list_json_falls_back_to_production(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A JSON object (not array) is rejected — override must be a list."""
        monkeypatch.setenv(
            "CHECKRD_POLICY_TRUST_OVERRIDE_JSON",
            json.dumps({"keyid": "bad", "public_key_hex": "a" * 64}),
        )
        monkeypatch.setenv("CHECKRD_ALLOW_TRUST_OVERRIDE", "1")
        result = trusted_policy_keys()
        assert result == _PRODUCTION_TRUSTED_KEYS

    def test_json_string_falls_back_to_production(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CHECKRD_POLICY_TRUST_OVERRIDE_JSON", '"just-a-string"')
        monkeypatch.setenv("CHECKRD_ALLOW_TRUST_OVERRIDE", "1")
        result = trusted_policy_keys()
        assert result == _PRODUCTION_TRUSTED_KEYS

    def test_json_number_falls_back_to_production(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CHECKRD_POLICY_TRUST_OVERRIDE_JSON", "42")
        monkeypatch.setenv("CHECKRD_ALLOW_TRUST_OVERRIDE", "1")
        result = trusted_policy_keys()
        assert result == _PRODUCTION_TRUSTED_KEYS

    def test_empty_string_env_var_falls_back_to_production(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty string is falsy — should return production keys."""
        monkeypatch.setenv("CHECKRD_POLICY_TRUST_OVERRIDE_JSON", "")
        monkeypatch.setenv("CHECKRD_ALLOW_TRUST_OVERRIDE", "1")
        result = trusted_policy_keys()
        assert result == _PRODUCTION_TRUSTED_KEYS

    def test_unset_env_var_returns_production(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CHECKRD_POLICY_TRUST_OVERRIDE_JSON", raising=False)
        monkeypatch.delenv("CHECKRD_ALLOW_TRUST_OVERRIDE", raising=False)
        result = trusted_policy_keys()
        assert result == _PRODUCTION_TRUSTED_KEYS


class TestKeyRotationScenarios:
    """Verify the trust list supports overlapping validity windows for key rotation."""

    def test_overlapping_validity_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        old_key = {
            "keyid": "prod-v1",
            "public_key_hex": "aa" * 32,
            "valid_from": 1700000000,
            "valid_until": 1800000000,
        }
        new_key = {
            "keyid": "prod-v2",
            "public_key_hex": "bb" * 32,
            "valid_from": 1750000000,
            "valid_until": 1900000000,
        }
        monkeypatch.setenv(
            "CHECKRD_POLICY_TRUST_OVERRIDE_JSON", json.dumps([old_key, new_key])
        )
        monkeypatch.setenv("CHECKRD_ALLOW_TRUST_OVERRIDE", "1")
        result = trusted_policy_keys()
        assert len(result) == 2
        keyids = {k["keyid"] for k in result}
        assert keyids == {"prod-v1", "prod-v2"}

    def test_expired_key_still_in_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        expired_key = {
            "keyid": "legacy",
            "public_key_hex": "cc" * 32,
            "valid_from": 1000000000,
            "valid_until": 1100000000,
        }
        current_key = {
            "keyid": "current",
            "public_key_hex": "dd" * 32,
            "valid_from": 1700000000,
            "valid_until": 2000000000,
        }
        monkeypatch.setenv(
            "CHECKRD_POLICY_TRUST_OVERRIDE_JSON",
            json.dumps([expired_key, current_key]),
        )
        monkeypatch.setenv("CHECKRD_ALLOW_TRUST_OVERRIDE", "1")
        result = trusted_policy_keys()
        assert len(result) == 2

    def test_override_preserves_field_structure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        key = {
            "keyid": "test-roundtrip",
            "public_key_hex": "ef" * 32,
            "valid_from": 1700000000,
            "valid_until": 1800000000,
        }
        monkeypatch.setenv("CHECKRD_POLICY_TRUST_OVERRIDE_JSON", json.dumps([key]))
        monkeypatch.setenv("CHECKRD_ALLOW_TRUST_OVERRIDE", "1")
        result = trusted_policy_keys()
        assert len(result) == 1
        assert result[0]["keyid"] == "test-roundtrip"
        assert result[0]["public_key_hex"] == "ef" * 32
        assert result[0]["valid_from"] == 1700000000
        assert result[0]["valid_until"] == 1800000000


# ============================================================
# Production trust-status diagnostics
# ============================================================
#
# `production_trust_status()` is the source of truth for both the
# `checkrd policy trust-status` CI guard and the one-shot startup
# warning fired by `ControlReceiver.start`. Tests here cover the four
# distinct level outputs and the boundary conditions the runtime cares
# about.


class TestProductionTrustStatus:
    """The pure diagnostic — every level + boundary covered."""

    def test_returns_ok_when_production_keys_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from checkrd._trust import production_trust_status

        # Inject a non-empty production list via the same module-level
        # patch the bootstrap script will perform when run.
        monkeypatch.setattr(
            "checkrd._trust._PRODUCTION_TRUSTED_KEYS",
            [{"keyid": "x", "public_key_hex": "a" * 64,
              "valid_from": 0, "valid_until": 9999999999}],
        )
        level, message = production_trust_status(
            base_url="https://api.checkrd.io", env={},
        )
        assert level == "ok"
        assert "1 key" in message

    def test_returns_override_when_double_gate_active(self) -> None:
        from checkrd._trust import production_trust_status

        env = {
            "CHECKRD_POLICY_TRUST_OVERRIDE_JSON": '[{"keyid":"x"}]',
            "CHECKRD_ALLOW_TRUST_OVERRIDE": "1",
        }
        level, message = production_trust_status(base_url=None, env=env)
        assert level == "override"
        assert "DO NOT" not in message  # warning text lives in trusted_policy_keys()

    def test_returns_override_for_each_valid_gate_value(self) -> None:
        from checkrd._trust import production_trust_status

        for gate in ("1", "true", "yes"):
            env = {
                "CHECKRD_POLICY_TRUST_OVERRIDE_JSON": "[]",
                "CHECKRD_ALLOW_TRUST_OVERRIDE": gate,
            }
            level, _ = production_trust_status(base_url=None, env=env)
            assert level == "override", f"gate={gate!r} should be honored"

    def test_invalid_gate_value_does_not_count_as_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An override JSON without the gate set to a recognized value
        # should fall through to the empty-list path, NOT report
        # "override". Mirrors `trusted_policy_keys`'s own gate check.
        from checkrd._trust import production_trust_status

        monkeypatch.setattr("checkrd._trust._PRODUCTION_TRUSTED_KEYS", [])
        env = {
            "CHECKRD_POLICY_TRUST_OVERRIDE_JSON": "[]",
            "CHECKRD_ALLOW_TRUST_OVERRIDE": "0",
        }
        level, _ = production_trust_status(base_url=None, env=env)
        assert level != "override"

    def test_returns_empty_dev_for_localhost(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from checkrd._trust import production_trust_status

        monkeypatch.setattr("checkrd._trust._PRODUCTION_TRUSTED_KEYS", [])
        level, _ = production_trust_status(
            base_url="http://localhost:8080", env={},
        )
        assert level == "empty_dev"

    def test_returns_empty_dev_when_base_url_is_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from checkrd._trust import production_trust_status

        monkeypatch.setattr("checkrd._trust._PRODUCTION_TRUSTED_KEYS", [])
        level, _ = production_trust_status(base_url=None, env={})
        assert level == "empty_dev"

    def test_returns_empty_production_for_real_endpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from checkrd._trust import production_trust_status

        monkeypatch.setattr("checkrd._trust._PRODUCTION_TRUSTED_KEYS", [])
        level, message = production_trust_status(
            base_url="https://api.checkrd.io", env={},
        )
        assert level == "empty_production"
        assert "scripts/generate-policy-signing-key.py" in message

    def test_empty_production_detected_via_substring_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Sub-domains, custom CNAMEs, and non-standard ports all count
        # as "production" if the host marker is anywhere in the URL.
        from checkrd._trust import production_trust_status

        monkeypatch.setattr("checkrd._trust._PRODUCTION_TRUSTED_KEYS", [])
        for url in (
            "https://api.checkrd.io",
            "https://api.staging.checkrd.io",
            "https://api.checkrd.io:8443",
            "wss://api.checkrd.io/v1/agents/x/control",
        ):
            level, _ = production_trust_status(base_url=url, env={})
            assert level == "empty_production", url


class TestWarnIfMisconfigured:
    """One-shot startup warning fires only on the empty_production state."""

    def test_fires_critical_log_for_empty_production(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from checkrd._trust import _reset_warning_state_for_tests, warn_if_misconfigured

        monkeypatch.setattr("checkrd._trust._PRODUCTION_TRUSTED_KEYS", [])
        _reset_warning_state_for_tests()
        with caplog.at_level("CRITICAL", logger="checkrd"):
            warn_if_misconfigured(base_url="https://api.checkrd.io")

        critical = [r for r in caplog.records if r.levelname == "CRITICAL"]
        assert len(critical) == 1
        assert "production trust list is empty" in critical[0].message

    def test_fires_at_most_once_per_process(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from checkrd._trust import _reset_warning_state_for_tests, warn_if_misconfigured

        monkeypatch.setattr("checkrd._trust._PRODUCTION_TRUSTED_KEYS", [])
        _reset_warning_state_for_tests()
        with caplog.at_level("CRITICAL", logger="checkrd"):
            warn_if_misconfigured(base_url="https://api.checkrd.io")
            warn_if_misconfigured(base_url="https://api.checkrd.io")
            warn_if_misconfigured(base_url="https://api.checkrd.io")

        critical = [r for r in caplog.records if r.levelname == "CRITICAL"]
        assert len(critical) == 1, "warning must dedupe across repeated calls"

    def test_does_not_fire_for_empty_dev(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from checkrd._trust import _reset_warning_state_for_tests, warn_if_misconfigured

        monkeypatch.setattr("checkrd._trust._PRODUCTION_TRUSTED_KEYS", [])
        _reset_warning_state_for_tests()
        with caplog.at_level("CRITICAL", logger="checkrd"):
            warn_if_misconfigured(base_url="http://localhost:8080")
            warn_if_misconfigured(base_url=None)

        critical = [r for r in caplog.records if r.levelname == "CRITICAL"]
        assert critical == [], "dev URLs must not trigger the critical warning"

    def test_does_not_fire_when_keys_present(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from checkrd._trust import _reset_warning_state_for_tests, warn_if_misconfigured

        monkeypatch.setattr(
            "checkrd._trust._PRODUCTION_TRUSTED_KEYS",
            [{"keyid": "x", "public_key_hex": "a" * 64,
              "valid_from": 0, "valid_until": 9999999999}],
        )
        _reset_warning_state_for_tests()
        with caplog.at_level("CRITICAL", logger="checkrd"):
            warn_if_misconfigured(base_url="https://api.checkrd.io")

        critical = [r for r in caplog.records if r.levelname == "CRITICAL"]
        assert critical == []

    def test_reset_re_arms_the_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Use an explicit mock logger so the test isn't entangled with
        # caplog state — reset → call → reset → call should produce two
        # `critical` invocations.
        from unittest.mock import Mock

        from checkrd._trust import _reset_warning_state_for_tests, warn_if_misconfigured

        monkeypatch.setattr("checkrd._trust._PRODUCTION_TRUSTED_KEYS", [])

        logger = Mock()
        _reset_warning_state_for_tests()
        warn_if_misconfigured(base_url="https://api.checkrd.io", logger=logger)
        assert logger.critical.call_count == 1

        _reset_warning_state_for_tests()
        warn_if_misconfigured(base_url="https://api.checkrd.io", logger=logger)
        assert logger.critical.call_count == 2
