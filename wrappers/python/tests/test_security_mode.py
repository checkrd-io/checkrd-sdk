"""Security-mode invariants: strict fails closed, permissive opts in to fail-open.

These tests pin the single most important behavior the SDK advertises to
enterprise consumers — that the security layer cannot silently disable
itself. Breaking any of these is a publishable CVE.
"""

from __future__ import annotations

import pytest

import checkrd
from checkrd._settings import (
    DEFAULT_SECURITY_MODE,
    ENV_SECURITY_MODE,
    _resolve_security_mode,
    resolve,
)
from checkrd.exceptions import CheckrdInitError


# ---------------------------------------------------------------------------
# Settings-level invariants
# ---------------------------------------------------------------------------


def test_default_security_mode_is_strict():
    """The default must be fail-closed. A permissive default would let a
    broken security layer silently allow traffic."""
    assert DEFAULT_SECURITY_MODE == "strict"


def test_resolve_security_mode_explicit_wins_over_env():
    assert _resolve_security_mode("permissive", {ENV_SECURITY_MODE: "strict"}) == "permissive"
    assert _resolve_security_mode("strict", {ENV_SECURITY_MODE: "permissive"}) == "strict"


def test_resolve_security_mode_env_when_no_explicit():
    assert _resolve_security_mode(None, {ENV_SECURITY_MODE: "permissive"}) == "permissive"
    assert _resolve_security_mode(None, {ENV_SECURITY_MODE: "strict"}) == "strict"


def test_resolve_security_mode_unknown_env_falls_back_to_default():
    """A typo must not silently weaken the posture. Unknown values fall back
    to the default (strict). This is the same safety stance urllib3 uses
    for CERT_ env vars."""
    assert _resolve_security_mode(None, {ENV_SECURITY_MODE: "lax"}) == "strict"
    assert _resolve_security_mode(None, {ENV_SECURITY_MODE: "enabled"}) == "strict"
    assert _resolve_security_mode(None, {ENV_SECURITY_MODE: ""}) == "strict"


def test_resolve_security_mode_rejects_bad_explicit():
    with pytest.raises(ValueError, match="security_mode must be"):
        _resolve_security_mode("lax", {})  # type: ignore[arg-type]


def test_settings_exposes_security_mode():
    settings = resolve(env={})
    assert settings.security_mode == "strict"

    settings = resolve(env={ENV_SECURITY_MODE: "permissive"})
    assert settings.security_mode == "permissive"

    settings = resolve(security_mode="permissive", env={})
    assert settings.security_mode == "permissive"


# ---------------------------------------------------------------------------
# Dev-flag split — legacy CHECKRD_DEV must still work but warn.
# ---------------------------------------------------------------------------


def test_checkrd_dev_alias_emits_deprecation_warning():
    # Reset the once-per-process guard by reloading the module.
    import importlib
    import checkrd._settings as settings_mod
    importlib.reload(settings_mod)

    with pytest.warns(DeprecationWarning, match="CHECKRD_DEV is deprecated"):
        assert settings_mod._http_allowed({"CHECKRD_DEV": "1"}) is True

    # New flags emit nothing.
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert settings_mod._http_allowed({"CHECKRD_ALLOW_INSECURE_HTTP": "1"}) is True
        assert settings_mod._wasm_integrity_skipped(
            {"CHECKRD_SKIP_WASM_INTEGRITY": "1"}
        ) is True


def test_new_http_flag_does_not_skip_wasm_integrity():
    """Splitting the flags means each one controls exactly one thing.
    CHECKRD_ALLOW_INSECURE_HTTP must NOT bypass WASM integrity checks —
    the audit finding that motivated this split was that leaking one env
    var disabled two independent controls."""
    from checkrd._settings import _http_allowed, _wasm_integrity_skipped

    env = {"CHECKRD_ALLOW_INSECURE_HTTP": "1"}
    assert _http_allowed(env) is True
    assert _wasm_integrity_skipped(env) is False

    env = {"CHECKRD_SKIP_WASM_INTEGRITY": "1"}
    assert _http_allowed(env) is False
    assert _wasm_integrity_skipped(env) is True


# ---------------------------------------------------------------------------
# _build_runtime — strict raises, permissive degrades.
# ---------------------------------------------------------------------------


def test_strict_mode_raises_on_engine_failure(monkeypatch):
    """If WASM engine creation blows up, strict mode must raise. A silent
    degradation here is the CVE-class pattern enterprise auditors flag."""
    from checkrd import _build_runtime

    def boom(*_a, **_kw):
        raise RuntimeError("simulated wasmtime crash")

    monkeypatch.setattr("checkrd._create_engine_from_json", boom)

    with pytest.raises(CheckrdInitError, match="engine failed to load"):
        _build_runtime(
            agent_id="test",
            policy=None,
            identity=None,
            enforce="auto",
            control_plane_url=None,
            api_key=None,
            telemetry_sink=None,
            security_mode="strict",
        )


def test_permissive_mode_degrades_on_engine_failure(monkeypatch, caplog):
    """Permissive preserves the pre-0.2 fail-open behavior so teams can
    roll out gradually. The SDK returns None and sets degraded=True."""
    from checkrd import _build_runtime
    from checkrd._state import is_degraded, set_degraded

    set_degraded(False)

    def boom(*_a, **_kw):
        raise RuntimeError("simulated wasmtime crash")

    monkeypatch.setattr("checkrd._create_engine_from_json", boom)

    import logging
    with caplog.at_level(logging.WARNING, logger="checkrd"):
        runtime = _build_runtime(
            agent_id="test",
            policy=None,
            identity=None,
            enforce="auto",
            control_plane_url=None,
            api_key=None,
            telemetry_sink=None,
            security_mode="permissive",
        )

    assert runtime is None
    assert is_degraded()
    assert any(
        "security_mode='permissive'" in r.message
        for r in caplog.records
    )
    set_degraded(False)


def test_strict_raises_checkrdiniterror_via_wrap(monkeypatch):
    """Same invariant via the public wrap() entry point."""
    import httpx

    def boom(*_a, **_kw):
        raise RuntimeError("simulated wasmtime crash")

    monkeypatch.setattr("checkrd._create_engine_from_json", boom)

    with httpx.Client() as client, pytest.raises(CheckrdInitError):
        checkrd.wrap(client, api_key="test", security_mode="strict")


def test_env_var_default_is_strict_via_wrap(monkeypatch):
    """When neither kwarg nor env var is set, we must land on strict."""
    monkeypatch.delenv("CHECKRD_SECURITY_MODE", raising=False)

    def boom(*_a, **_kw):
        raise RuntimeError("simulated wasmtime crash")

    monkeypatch.setattr("checkrd._create_engine_from_json", boom)

    import httpx
    with httpx.Client() as client, pytest.raises(CheckrdInitError):
        checkrd.wrap(client, api_key="test")
