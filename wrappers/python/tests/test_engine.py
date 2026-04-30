"""Tests for checkrd.engine."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from checkrd.engine import WasmEngine, _verify_wasm_integrity
from checkrd.exceptions import CheckrdInitError
from tests.conftest import requires_wasm

_TS = "2026-03-28T14:30:00Z"
_TS_MS = 1774708200000

def _eval(
    engine: WasmEngine, method: str = "GET", url: str = "https://api.stripe.com/v1/charges"
) -> object:
    return engine.evaluate(
        request_id="req-001",
        method=method,
        url=url,
        headers=[],
        body=None,
        timestamp=_TS,
        timestamp_ms=_TS_MS,
    )


class TestWasmIntegrity:
    """Verify SHA-256 integrity check on the WASM binary.

    Catches tampered binaries that could bypass policy (allow-all), deny
    all requests (DoS), or forge telemetry/signatures.

    The integrity check is **fail-closed by default**: a missing hash file
    in production raises ``CheckrdInitError``. Only
    ``CHECKRD_SKIP_WASM_INTEGRITY=1`` relaxes this to a warning. This
    prevents a packaging or deployment error from silently disabling
    supply-chain verification.
    """

    @pytest.fixture(autouse=True)
    def _clear_integrity_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The session-wide `_dev_mode` fixture sets
        CHECKRD_SKIP_WASM_INTEGRITY=1 to unblock unrelated tests. Tests in
        THIS class are the ones that exercise the integrity-check path
        explicitly, so we start from a clean slate and opt back in where
        needed."""
        monkeypatch.delenv("CHECKRD_SKIP_WASM_INTEGRITY", raising=False)
        monkeypatch.delenv("CHECKRD_DEV", raising=False)

    def test_matching_hash_passes(self, tmp_path: Path) -> None:
        import types

        content = b"fake wasm binary for integrity test"
        wasm_path = tmp_path / "checkrd_core.wasm"
        wasm_path.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()

        fake_module = types.ModuleType("checkrd._wasm_integrity")
        fake_module.EXPECTED_SHA256 = expected  # type: ignore[attr-defined]

        with patch.dict("sys.modules", {"checkrd._wasm_integrity": fake_module}):
            # Should not raise
            _verify_wasm_integrity(wasm_path)

    def test_tampered_binary_raises(self, tmp_path: Path) -> None:
        import types

        wasm_path = tmp_path / "checkrd_core.wasm"
        wasm_path.write_bytes(b"original content")

        wrong_hash = hashlib.sha256(b"different content").hexdigest()
        fake_module = types.ModuleType("checkrd._wasm_integrity")
        fake_module.EXPECTED_SHA256 = wrong_hash  # type: ignore[attr-defined]

        with patch.dict("sys.modules", {"checkrd._wasm_integrity": fake_module}):
            with pytest.raises(CheckrdInitError, match="integrity check failed"):
                _verify_wasm_integrity(wasm_path)

    def test_missing_integrity_file_raises_in_production(
        self,
        tmp_path: Path,
    ) -> None:
        """Missing _wasm_integrity.py without the skip flag must raise.

        This is the critical fail-closed behavior: a production wheel that
        somehow lost the hash file should refuse to load rather than
        silently disabling supply-chain verification."""
        wasm_path = tmp_path / "checkrd_core.wasm"
        wasm_path.write_bytes(b"content")

        with patch.dict("sys.modules", {"checkrd._wasm_integrity": None}):
            with pytest.raises(CheckrdInitError, match="integrity file.*missing"):
                _verify_wasm_integrity(wasm_path)

    def test_missing_integrity_file_raises_with_skip_flag_false(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """CHECKRD_SKIP_WASM_INTEGRITY=0 (explicit false) still fails closed."""
        wasm_path = tmp_path / "checkrd_core.wasm"
        wasm_path.write_bytes(b"content")

        monkeypatch.setenv("CHECKRD_SKIP_WASM_INTEGRITY", "0")

        with patch.dict("sys.modules", {"checkrd._wasm_integrity": None}):
            with pytest.raises(CheckrdInitError, match="integrity file.*missing"):
                _verify_wasm_integrity(wasm_path)

    def test_missing_integrity_file_allowed_with_skip_flag(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """CHECKRD_SKIP_WASM_INTEGRITY=1 relaxes the check to a warning."""
        wasm_path = tmp_path / "checkrd_core.wasm"
        wasm_path.write_bytes(b"content")

        monkeypatch.setenv("CHECKRD_SKIP_WASM_INTEGRITY", "1")

        with patch.dict("sys.modules", {"checkrd._wasm_integrity": None}):
            with caplog.at_level("WARNING", logger="checkrd"):
                _verify_wasm_integrity(wasm_path)

        assert any("SKIPPED" in r.message for r in caplog.records)
        assert any(
            "CHECKRD_SKIP_WASM_INTEGRITY" in r.message for r in caplog.records
        )

    @pytest.mark.parametrize("truthy", ["true", "yes", "on", "1"])
    def test_skip_flag_truthy_variants(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        truthy: str,
    ) -> None:
        """All truthy values for CHECKRD_SKIP_WASM_INTEGRITY skip the check."""
        wasm_path = tmp_path / "checkrd_core.wasm"
        wasm_path.write_bytes(b"content")

        monkeypatch.setenv("CHECKRD_SKIP_WASM_INTEGRITY", truthy)

        with patch.dict("sys.modules", {"checkrd._wasm_integrity": None}):
            _verify_wasm_integrity(wasm_path)

    @pytest.mark.parametrize("falsy", ["false", "no", "off", "", "random"])
    def test_non_truthy_skip_values_still_fail_closed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        falsy: str,
    ) -> None:
        """Non-truthy values must NOT skip — a typo must not silently weaken."""
        wasm_path = tmp_path / "checkrd_core.wasm"
        wasm_path.write_bytes(b"content")

        monkeypatch.setenv("CHECKRD_SKIP_WASM_INTEGRITY", falsy)

        with patch.dict("sys.modules", {"checkrd._wasm_integrity": None}):
            with pytest.raises(CheckrdInitError, match="integrity file.*missing"):
                _verify_wasm_integrity(wasm_path)

    def test_error_message_includes_remediation(
        self,
        tmp_path: Path,
    ) -> None:
        """The error message tells users how to fix it."""
        wasm_path = tmp_path / "checkrd_core.wasm"
        wasm_path.write_bytes(b"content")

        with patch.dict("sys.modules", {"checkrd._wasm_integrity": None}):
            with pytest.raises(CheckrdInitError, match="pip install checkrd"):
                _verify_wasm_integrity(wasm_path)

    def test_legacy_dev_flag_still_skips_with_deprecation_warning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """CHECKRD_DEV=1 is the deprecated alias. It still works but warns.

        Backwards compat is important — existing CI pipelines that set
        CHECKRD_DEV shouldn't break on upgrade. 1.0 will remove it."""
        import importlib
        import checkrd._settings as settings_mod
        importlib.reload(settings_mod)  # reset one-shot warning guard

        wasm_path = tmp_path / "checkrd_core.wasm"
        wasm_path.write_bytes(b"content")

        monkeypatch.setenv("CHECKRD_DEV", "1")

        with patch.dict("sys.modules", {"checkrd._wasm_integrity": None}):
            with pytest.warns(DeprecationWarning, match="CHECKRD_DEV"):
                _verify_wasm_integrity(wasm_path)

    @requires_wasm
    def test_real_wasm_binary_passes_if_integrity_present(self) -> None:
        """If _wasm_integrity.py exists, the real binary should pass."""
        from checkrd.engine import _WASM_PATH

        try:
            from checkrd._wasm_integrity import EXPECTED_SHA256

            # If the file exists, verify it matches
            actual = hashlib.sha256(_WASM_PATH.read_bytes()).hexdigest()
            assert actual == EXPECTED_SHA256
        except ImportError:
            pytest.skip("_wasm_integrity.py not generated (run copy-wasm.sh)")


class TestWasmIntegrityProductionGuard:
    """Refuse to honor CHECKRD_SKIP_WASM_INTEGRITY in production-like envs.

    The bypass flag is a legitimate dev-time tool, but leaking the flag
    into a production deploy silently disables supply-chain verification.
    This class pins the safety net: any common ``ENV=production`` signal
    combined with the skip flag must refuse unless the operator types
    the break-glass acknowledgment phrase.
    """

    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in (
            "CHECKRD_SKIP_WASM_INTEGRITY",
            "CHECKRD_I_UNDERSTAND_WASM_INTEGRITY_RISK",
            "CHECKRD_DEV",
            "CHECKRD_ENV",
            "CHECKRD_ENVIRONMENT",
            "ENVIRONMENT",
            "ENV",
            "APP_ENV",
            "NODE_ENV",
            "RAILS_ENV",
            "DJANGO_ENV",
            "FLASK_ENV",
            "PYTHON_ENV",
            "DEPLOYMENT_ENVIRONMENT",
        ):
            monkeypatch.delenv(name, raising=False)

    @pytest.mark.parametrize(
        "prod_env_name",
        [
            "CHECKRD_ENV",
            "CHECKRD_ENVIRONMENT",
            "ENVIRONMENT",
            "ENV",
            "APP_ENV",
            "NODE_ENV",
            "RAILS_ENV",
            "DJANGO_ENV",
            "FLASK_ENV",
            "PYTHON_ENV",
            "DEPLOYMENT_ENVIRONMENT",
        ],
    )
    @pytest.mark.parametrize("prod_value", ["production", "prod", "canary", "live"])
    def test_skip_rejected_when_production_signal_set(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        prod_env_name: str,
        prod_value: str,
    ) -> None:
        wasm_path = tmp_path / "checkrd_core.wasm"
        wasm_path.write_bytes(b"content")
        monkeypatch.setenv("CHECKRD_SKIP_WASM_INTEGRITY", "1")
        monkeypatch.setenv(prod_env_name, prod_value)
        with patch.dict("sys.modules", {"checkrd._wasm_integrity": None}):
            with pytest.raises(
                CheckrdInitError, match="production-looking environment"
            ):
                _verify_wasm_integrity(wasm_path)

    def test_error_message_names_the_offending_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        wasm_path = tmp_path / "checkrd_core.wasm"
        wasm_path.write_bytes(b"content")
        monkeypatch.setenv("CHECKRD_SKIP_WASM_INTEGRITY", "1")
        monkeypatch.setenv("NODE_ENV", "production")
        with patch.dict("sys.modules", {"checkrd._wasm_integrity": None}):
            with pytest.raises(CheckrdInitError, match="NODE_ENV='production'"):
                _verify_wasm_integrity(wasm_path)

    def test_acknowledgment_phrase_permits_bypass(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        wasm_path = tmp_path / "checkrd_core.wasm"
        wasm_path.write_bytes(b"content")
        monkeypatch.setenv("CHECKRD_SKIP_WASM_INTEGRITY", "1")
        monkeypatch.setenv("NODE_ENV", "production")
        monkeypatch.setenv(
            "CHECKRD_I_UNDERSTAND_WASM_INTEGRITY_RISK",
            "i-understand-the-risk",
        )
        with patch.dict("sys.modules", {"checkrd._wasm_integrity": None}):
            with caplog.at_level("WARNING", logger="checkrd"):
                _verify_wasm_integrity(wasm_path)  # must not raise
        assert any("SKIPPED" in r.message for r in caplog.records)

    @pytest.mark.parametrize(
        "wrong_ack",
        ["1", "true", "yes", "i-understand", "I-UNDERSTAND-THE-RISK  "],
    )
    def test_wrong_acknowledgment_phrases_rejected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        wrong_ack: str,
    ) -> None:
        """Only the exact literal phrase permits the bypass."""
        wasm_path = tmp_path / "checkrd_core.wasm"
        wasm_path.write_bytes(b"content")
        monkeypatch.setenv("CHECKRD_SKIP_WASM_INTEGRITY", "1")
        monkeypatch.setenv("NODE_ENV", "production")
        monkeypatch.setenv(
            "CHECKRD_I_UNDERSTAND_WASM_INTEGRITY_RISK", wrong_ack,
        )
        with patch.dict("sys.modules", {"checkrd._wasm_integrity": None}):
            with pytest.raises(
                CheckrdInitError, match="production-looking environment"
            ):
                _verify_wasm_integrity(wasm_path)

    def test_non_production_values_allow_bypass(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        wasm_path = tmp_path / "checkrd_core.wasm"
        wasm_path.write_bytes(b"content")
        monkeypatch.setenv("CHECKRD_SKIP_WASM_INTEGRITY", "1")
        monkeypatch.setenv("NODE_ENV", "development")
        with patch.dict("sys.modules", {"checkrd._wasm_integrity": None}):
            with caplog.at_level("WARNING", logger="checkrd"):
                _verify_wasm_integrity(wasm_path)
        assert any("SKIPPED" in r.message for r in caplog.records)


@requires_wasm
class TestWasmEngineInit:
    def test_success(self, policy_json: str) -> None:
        engine = WasmEngine(policy_json, "test-agent")
        assert engine is not None

    def test_invalid_policy_json(self) -> None:
        with pytest.raises(CheckrdInitError):
            WasmEngine("not valid json", "test-agent")

    def test_wasm_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("checkrd.engine._WASM_PATH", Path("/nonexistent/checkrd_core.wasm"))
        # Clear the module cache so it re-reads _WASM_PATH
        monkeypatch.setattr("checkrd.engine._cached_module", None)
        monkeypatch.setattr("checkrd.engine._cached_wasm_engine", None)
        with pytest.raises(CheckrdInitError, match="not found"):
            WasmEngine("{}", "agent")


@requires_wasm
class TestEvaluate:
    def test_allowed(self, policy_json: str) -> None:
        engine = WasmEngine(policy_json, "test-agent")
        result = _eval(engine, "GET", "https://api.stripe.com/v1/charges")
        assert result.allowed
        assert result.deny_reason is None
        assert result.request_id == "req-001"

    def test_denied_by_rule(self, policy_json: str) -> None:
        engine = WasmEngine(policy_json, "test-agent")
        result = _eval(engine, "DELETE", "https://api.stripe.com/v1/charges")
        assert not result.allowed
        assert result.deny_reason is not None
        assert "block-deletes" in result.deny_reason

    def test_denied_by_default(self, policy_json: str) -> None:
        engine = WasmEngine(policy_json, "test-agent")
        result = _eval(engine, "GET", "https://unknown.com/api")
        assert not result.allowed
        assert result.deny_reason is not None
        assert "default policy" in result.deny_reason

    def test_telemetry_populated(self, policy_json: str) -> None:
        engine = WasmEngine(policy_json, "test-agent")
        result = _eval(engine, "GET", "https://api.stripe.com/v1/charges")
        telemetry = json.loads(result.telemetry_json)
        assert telemetry["agent_id"] == "test-agent"
        assert telemetry["request"]["url_host"] == "api.stripe.com"
        assert telemetry["request"]["method"] == "GET"
        assert telemetry["policy_result"] == "allowed"


@requires_wasm
class TestKillSwitch:
    def test_activate_denies(self, policy_json: str) -> None:
        engine = WasmEngine(policy_json, "test-agent")
        result = _eval(engine)
        assert result.allowed

        engine.set_kill_switch(True)
        result = _eval(engine)
        assert not result.allowed
        assert result.deny_reason is not None
        assert "kill switch" in result.deny_reason

    def test_deactivate_restores(self, policy_json: str) -> None:
        engine = WasmEngine(policy_json, "test-agent")
        engine.set_kill_switch(True)
        engine.set_kill_switch(False)
        result = _eval(engine)
        assert result.allowed


@requires_wasm
class TestReloadPolicy:
    def test_reload_changes_behavior(
        self, policy_json: str, allow_all_policy_json: str
    ) -> None:
        engine = WasmEngine(policy_json, "test-agent")
        # Default-deny, unknown URL denied
        result = _eval(engine, "GET", "https://unknown.com/api")
        assert not result.allowed

        # Reload to allow-all
        engine.reload_policy(allow_all_policy_json)
        result = _eval(engine, "GET", "https://unknown.com/api")
        assert result.allowed

    def test_invalid_reload_raises(self, policy_json: str) -> None:
        engine = WasmEngine(policy_json, "test-agent")
        with pytest.raises(CheckrdInitError, match="reload_policy"):
            engine.reload_policy("not valid json")


@requires_wasm
class TestPolicyVersionFFI:
    """End-to-end FFI tests for ``set_initial_policy_version`` and
    ``get_active_policy_version``.

    These exercise the WASM core through the wrapper bindings — the
    Rust unit tests in ``crates/core`` cover the in-process semantics
    via the Rust-native test harness; here we verify the Python
    bindings call the FFI correctly and round-trip integers across the
    WASM boundary.
    """

    def test_get_returns_zero_on_fresh_engine(
        self, policy_json: str
    ) -> None:
        engine = WasmEngine(policy_json, "test-agent")
        assert engine.get_active_policy_version() == 0

    def test_set_initial_then_get_returns_value(
        self, policy_json: str
    ) -> None:
        engine = WasmEngine(policy_json, "test-agent")
        engine.set_initial_policy_version(123)
        assert engine.get_active_policy_version() == 123

    def test_set_initial_twice_raises(
        self, policy_json: str
    ) -> None:
        engine = WasmEngine(policy_json, "test-agent")
        engine.set_initial_policy_version(10)
        # Second call must fail — the in-memory counter is the source of
        # truth once it's non-zero.
        with pytest.raises(CheckrdInitError, match="policy_version_already_set"):
            engine.set_initial_policy_version(5)
        # The original value must be preserved.
        assert engine.get_active_policy_version() == 10

    def test_set_initial_with_large_value(
        self, policy_json: str
    ) -> None:
        # u64 round-trip across the WASM boundary.
        engine = WasmEngine(policy_json, "test-agent")
        engine.set_initial_policy_version(2**40)
        assert engine.get_active_policy_version() == 2**40
