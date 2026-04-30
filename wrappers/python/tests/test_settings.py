"""Tests for checkrd._settings.resolve() and derive_agent_id().

These are pure-Python unit tests — no WASM, no httpx, no filesystem — so
they run on any interpreter and serve as the authoritative contract for
the precedence rules. End-to-end coverage through ``wrap()`` lives in
``test_wrap.py::TestWrapEnvResolution``.
"""

from __future__ import annotations

from typing import Mapping

import pytest

from checkrd._settings import (
    DEFAULT_BASE_URL,
    ENV_AGENT_ID,
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_DEV,
    ENV_DISABLED,
    ENV_ENFORCE,
    Settings,
    _parse_bool,
    _validate_url,
    derive_agent_id,
    resolve,
)


# ============================================================
# resolve(): the public precedence contract
# ============================================================


class TestResolveApiKey:
    """API key precedence: explicit arg > env var > None.

    Mirrors the Stripe/Anthropic convention where a caller-supplied key
    always beats the environment so programmatic config is testable.
    """

    def test_explicit_wins_over_env(self) -> None:
        settings = resolve(api_key="explicit", env={ENV_API_KEY: "env_value"})
        assert settings.api_key == "explicit"

    def test_env_used_when_explicit_none(self) -> None:
        settings = resolve(env={ENV_API_KEY: "env_value"})
        assert settings.api_key == "env_value"

    def test_none_when_both_unset(self) -> None:
        settings = resolve(env={})
        assert settings.api_key is None

    def test_empty_env_string_becomes_none(self) -> None:
        # An empty string in the env is almost always a mis-set variable,
        # not an intentional empty credential. Treat it as "unset".
        settings = resolve(env={ENV_API_KEY: ""})
        assert settings.api_key is None


class TestResolveBaseUrl:
    """Control-plane base URL precedence with the api_key-driven default.

    The autofill to ``DEFAULT_BASE_URL`` when an API key is present (but
    no URL is) is how we enable the true zero-config path against the
    hosted Checkrd Cloud.
    """

    def test_explicit_wins(self) -> None:
        settings = resolve(
            control_plane_url="https://explicit.example",
            env={ENV_BASE_URL: "https://env.example"},
        )
        assert settings.control_plane_url == "https://explicit.example"

    def test_env_var_used_when_no_explicit(self) -> None:
        settings = resolve(env={ENV_BASE_URL: "https://env.example"})
        assert settings.control_plane_url == "https://env.example"

    def test_default_when_api_key_set_but_no_url(self) -> None:
        # The zero-config happy path: user sets CHECKRD_API_KEY, nothing else,
        # and talks to the hosted control plane automatically.
        settings = resolve(env={ENV_API_KEY: "ck_live_xxx"})
        assert settings.control_plane_url == DEFAULT_BASE_URL

    def test_none_when_nothing_set(self) -> None:
        settings = resolve(env={})
        assert settings.control_plane_url is None

    def test_explicit_base_url_without_api_key(self) -> None:
        # Passing base_url but not api_key is valid for Tier 2 (self-hosted
        # control plane, no auth required) and Tier 3 setups using the URL
        # for another purpose. Don't auto-clear it.
        settings = resolve(control_plane_url="https://self-hosted.example", env={})
        assert settings.control_plane_url == "https://self-hosted.example"
        assert settings.api_key is None


class TestResolveAgentId:
    """Agent ID fallback: explicit > env > PaaS > hostname > random."""

    def test_explicit_wins(self) -> None:
        settings = resolve(
            agent_id="explicit",
            env={
                ENV_AGENT_ID: "env_value",
                "FLY_APP_NAME": "fly_value",
            },
        )
        assert settings.agent_id == "explicit"

    def test_env_var_wins_over_paas(self) -> None:
        settings = resolve(
            env={
                ENV_AGENT_ID: "env_value",
                "FLY_APP_NAME": "fly_value",
            }
        )
        assert settings.agent_id == "env_value"

    def test_whitespace_in_env_var_stripped(self) -> None:
        settings = resolve(env={ENV_AGENT_ID: "  padded  "})
        assert settings.agent_id == "padded"

    def test_empty_env_var_falls_through_to_derivation(self) -> None:
        # Empty env var should not suppress the derivation chain.
        settings = resolve(env={ENV_AGENT_ID: "   "})
        # Derivation ran — result is non-empty.
        assert settings.agent_id
        assert settings.agent_id != "   "

    def test_falls_back_to_paas_when_unset(self) -> None:
        settings = resolve(env={"FLY_APP_NAME": "my-service"})
        assert settings.agent_id == "my-service"


class TestResolveEnforce:
    """enforce precedence. Explicit booleans always ignore env vars so a
    caller asking for enforcement in code cannot be silently downgraded."""

    def test_explicit_true_ignores_env(self) -> None:
        settings = resolve(enforce=True, env={ENV_ENFORCE: "false"})
        assert settings.enforce_override is True

    def test_explicit_false_ignores_env(self) -> None:
        settings = resolve(enforce=False, env={ENV_ENFORCE: "true"})
        assert settings.enforce_override is False

    def test_auto_with_no_env_returns_none(self) -> None:
        # None means "auto" — the caller (wrap()) decides based on policy.
        settings = resolve(env={})
        assert settings.enforce_override is None

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "  true  "])
    def test_auto_honors_truthy_env(self, value: str) -> None:
        settings = resolve(env={ENV_ENFORCE: value})
        assert settings.enforce_override is True

    @pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", ""])
    def test_auto_honors_falsy_env(self, value: str) -> None:
        settings = resolve(env={ENV_ENFORCE: value})
        assert settings.enforce_override is False

    def test_auto_with_garbled_env_stays_auto(self) -> None:
        # A mis-set ``CHECKRD_ENFORCE=yess`` shouldn't silently enforce
        # or observe — it stays None so the policy-based decision runs.
        settings = resolve(env={ENV_ENFORCE: "yess"})
        assert settings.enforce_override is None

    def test_invalid_enforce_value_raises(self) -> None:
        with pytest.raises(ValueError, match="enforce must be"):
            resolve(enforce="maybe", env={})  # type: ignore[arg-type]


class TestResolveDisabled:
    @pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
    def test_truthy_values(self, value: str) -> None:
        settings = resolve(env={ENV_DISABLED: value})
        assert settings.disabled is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
    def test_falsy_values(self, value: str) -> None:
        settings = resolve(env={ENV_DISABLED: value})
        assert settings.disabled is False

    def test_unset_is_false(self) -> None:
        settings = resolve(env={})
        assert settings.disabled is False


class TestSettingsDataclass:
    def test_has_control_plane_requires_both(self) -> None:
        assert Settings(
            agent_id="a",
            api_key="k",
            control_plane_url="https://x",
            enforce_override=None,
            disabled=False,
            dashboard_url=None,
            debug=False,
        ).has_control_plane is True

    def test_has_control_plane_missing_key(self) -> None:
        assert Settings(
            agent_id="a",
            api_key=None,
            control_plane_url="https://x",
            enforce_override=None,
            disabled=False,
            dashboard_url=None,
            debug=False,
        ).has_control_plane is False

    def test_has_control_plane_missing_url(self) -> None:
        assert Settings(
            agent_id="a",
            api_key="k",
            control_plane_url=None,
            enforce_override=None,
            disabled=False,
            dashboard_url=None,
            debug=False,
        ).has_control_plane is False

    def test_frozen(self) -> None:
        # Settings is immutable so downstream code can safely share it.
        s = Settings(
            agent_id="a",
            api_key="k",
            control_plane_url="https://x",
            enforce_override=None,
            disabled=False,
            dashboard_url=None,
            debug=False,
        )
        with pytest.raises(Exception):  # FrozenInstanceError
            s.agent_id = "other"  # type: ignore[misc]


# ============================================================
# derive_agent_id(): the PaaS + hostname fallback chain
# ============================================================


class TestDeriveAgentId:
    def test_fly_app_name_wins(self) -> None:
        env: Mapping[str, str] = {
            "FLY_APP_NAME": "fly-service",
            "K_SERVICE": "cloud-run-service",
            "AWS_LAMBDA_FUNCTION_NAME": "lambda",
        }
        assert derive_agent_id(env=env) == "fly-service"

    def test_cloud_run_when_no_fly(self) -> None:
        env = {"K_SERVICE": "cloud-run-service"}
        assert derive_agent_id(env=env) == "cloud-run-service"

    def test_lambda_when_no_fly_or_cloud_run(self) -> None:
        env = {"AWS_LAMBDA_FUNCTION_NAME": "my-lambda"}
        assert derive_agent_id(env=env) == "my-lambda"

    @pytest.mark.parametrize(
        "paas_key,value",
        [
            ("HEROKU_APP_NAME", "heroku-app"),
            ("RAILWAY_SERVICE_NAME", "railway-svc"),
            ("RENDER_SERVICE_NAME", "render-svc"),
            ("KOYEB_APP_NAME", "koyeb-app"),
            ("FUNCTION_TARGET", "gcf-func"),
        ],
    )
    def test_each_paas_provider(self, paas_key: str, value: str) -> None:
        assert derive_agent_id(env={paas_key: value}) == value

    def test_whitespace_stripped(self) -> None:
        assert derive_agent_id(env={"FLY_APP_NAME": "  padded  "}) == "padded"

    def test_empty_paas_value_skipped(self) -> None:
        # An empty PaaS env var shouldn't win over a later one.
        env = {"FLY_APP_NAME": "", "K_SERVICE": "real-service"}
        assert derive_agent_id(env=env) == "real-service"

    def test_hostname_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No PaaS vars set — fall back to {script}-{hostname}.
        monkeypatch.setattr("socket.gethostname", lambda: "my-host.local")
        monkeypatch.setattr("sys.argv", ["/usr/local/bin/my_agent"])
        result = derive_agent_id(env={})
        assert result == "my_agent-my-host"

    def test_hostname_strips_domain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("socket.gethostname", lambda: "prod-01.internal.example.com")
        monkeypatch.setattr("sys.argv", ["worker.py"])
        result = derive_agent_id(env={})
        assert result == "worker-prod-01"

    def test_fails_closed_when_hostname_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Fail-closed contract: a missing hostname is a configuration
        # error, not an excuse to invent a volatile identity. The
        # alternative would silently break kill-switch scoping and
        # telemetry signature verification on every container restart.
        from checkrd.exceptions import CheckrdInitError

        monkeypatch.setattr("socket.gethostname", lambda: "")
        with pytest.raises(CheckrdInitError) as exc_info:
            derive_agent_id(env={})
        assert exc_info.value.code == "agent_id_undetectable"
        assert "CHECKRD_AGENT_ID" in str(exc_info.value)

    def test_fails_closed_when_hostname_oserror(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from checkrd.exceptions import CheckrdInitError

        def _raise() -> str:
            raise OSError("no network namespace")

        monkeypatch.setattr("socket.gethostname", _raise)
        # OSError from gethostname is a real case in locked-down
        # containers; the fix is to set CHECKRD_AGENT_ID, not to invent
        # a random one that breaks identity tracking.
        with pytest.raises(CheckrdInitError) as exc_info:
            derive_agent_id(env={})
        assert exc_info.value.code == "agent_id_undetectable"


# ============================================================
# _parse_bool: the env-var boolean contract
# ============================================================


class TestParseBool:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes", "on", " 1 "])
    def test_truthy(self, value: str) -> None:
        assert _parse_bool(value) is True

    @pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", ""])
    def test_falsy(self, value: str) -> None:
        assert _parse_bool(value) is False

    def test_none_passthrough(self) -> None:
        assert _parse_bool(None) is None

    @pytest.mark.parametrize("value", ["maybe", "yess", "tru", "2", "Yes!"])
    def test_garbled_returns_none(self, value: str) -> None:
        # None lets callers distinguish "explicitly false" from "unset".
        assert _parse_bool(value) is None


# ============================================================
# resolve() defaults to os.environ when env= is omitted
# ============================================================


# ============================================================
# _validate_url(): TLS enforcement
# ============================================================


class TestValidateUrl:
    """Control-plane URLs must use HTTPS in production.

    Sending API keys and signed telemetry over plaintext HTTP is a
    credential leak. The validation rejects non-HTTPS URLs unless
    ``CHECKRD_DEV=1`` is set, which supports ``http://localhost:8080``
    for local development.
    """

    def test_https_accepted(self) -> None:
        _validate_url("https://api.checkrd.io", "control_plane_url", {})

    def test_https_with_port_accepted(self) -> None:
        _validate_url("https://api.checkrd.io:8443", "control_plane_url", {})

    def test_https_with_path_accepted(self) -> None:
        _validate_url("https://api.checkrd.io/v1", "control_plane_url", {})

    def test_http_rejected_in_production(self) -> None:
        with pytest.raises(ValueError, match="must use HTTPS"):
            _validate_url("http://api.checkrd.io", "control_plane_url", {})

    def test_http_rejected_with_flag_false(self) -> None:
        with pytest.raises(ValueError, match="must use HTTPS"):
            _validate_url(
                "http://api.checkrd.io", "control_plane_url",
                {"CHECKRD_ALLOW_INSECURE_HTTP": "0"},
            )

    def test_http_allowed_with_new_flag(self) -> None:
        _validate_url(
            "http://localhost:8080", "control_plane_url",
            {"CHECKRD_ALLOW_INSECURE_HTTP": "1"},
        )

    @pytest.mark.parametrize("truthy", ["true", "yes", "on", "1"])
    def test_http_allowed_with_truthy_flag_values(self, truthy: str) -> None:
        _validate_url(
            "http://localhost:8080", "control_plane_url",
            {"CHECKRD_ALLOW_INSECURE_HTTP": truthy},
        )

    def test_legacy_dev_flag_still_allows_http_with_deprecation_warning(
        self,
    ) -> None:
        """Backwards compat: CHECKRD_DEV=1 still works but warns.

        The new split is CHECKRD_ALLOW_INSECURE_HTTP + CHECKRD_SKIP_WASM_INTEGRITY
        — CHECKRD_DEV bundled both, so a single leaked env var disabled two
        independent security controls. We emit a DeprecationWarning once
        per process to flag that this will be removed in 1.0.
        """
        import importlib
        import checkrd._settings as settings_mod
        importlib.reload(settings_mod)  # reset one-shot warning guard

        with pytest.warns(DeprecationWarning, match="CHECKRD_DEV"):
            settings_mod._validate_url(
                "http://localhost:8080", "control_plane_url", {ENV_DEV: "1"}
            )

    def test_http_localhost_rejected_without_flag(self) -> None:
        """Even localhost requires HTTPS without an explicit opt-in."""
        with pytest.raises(ValueError, match="must use HTTPS"):
            _validate_url("http://localhost:8080", "control_plane_url", {})

    def test_ftp_rejected(self) -> None:
        with pytest.raises(ValueError, match="must use HTTPS"):
            _validate_url("ftp://files.example.com", "control_plane_url", {})

    def test_no_scheme_rejected(self) -> None:
        with pytest.raises(ValueError, match="no scheme"):
            _validate_url("api.checkrd.io", "control_plane_url", {})

    def test_no_hostname_rejected(self) -> None:
        with pytest.raises(ValueError, match="no hostname"):
            _validate_url("https://", "control_plane_url", {})

    def test_error_message_includes_param_name(self) -> None:
        with pytest.raises(ValueError, match="dashboard_url"):
            _validate_url("http://evil.com", "dashboard_url", {})

    def test_error_message_includes_remediation(self) -> None:
        with pytest.raises(ValueError, match="CHECKRD_ALLOW_INSECURE_HTTP"):
            _validate_url("http://localhost", "control_plane_url", {})

    def test_http_allowed_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Enabling HTTP should leave a loud breadcrumb in the logs."""
        import logging

        with caplog.at_level(logging.WARNING, logger="checkrd"):
            _validate_url(
                "http://localhost:8080", "control_plane_url",
                {"CHECKRD_ALLOW_INSECURE_HTTP": "1"},
            )
        assert any("non-HTTPS" in r.message for r in caplog.records)
        assert any(
            "CHECKRD_ALLOW_INSECURE_HTTP" in r.message for r in caplog.records
        )


class TestResolveUrlValidation:
    """Integration tests: ``resolve()`` calls ``_validate_url()`` on
    control-plane and dashboard URLs.
    """

    def test_resolve_rejects_http_control_plane_url(self) -> None:
        with pytest.raises(ValueError, match="must use HTTPS"):
            resolve(control_plane_url="http://api.example.com", env={})

    def test_resolve_rejects_http_in_env_base_url(self) -> None:
        with pytest.raises(ValueError, match="must use HTTPS"):
            resolve(env={ENV_BASE_URL: "http://api.example.com"})

    def test_resolve_accepts_https_control_plane_url(self) -> None:
        settings = resolve(control_plane_url="https://api.example.com", env={})
        assert settings.control_plane_url == "https://api.example.com"

    def test_resolve_accepts_none_url(self) -> None:
        """No control plane URL is valid (offline mode)."""
        settings = resolve(env={})
        assert settings.control_plane_url is None

    def test_resolve_http_allowed_with_flag(self) -> None:
        settings = resolve(
            control_plane_url="http://localhost:8080",
            env={"CHECKRD_ALLOW_INSECURE_HTTP": "1"},
        )
        assert settings.control_plane_url == "http://localhost:8080"

    def test_resolve_default_base_url_is_https(self) -> None:
        """The default autofill is HTTPS, so it passes validation."""
        settings = resolve(env={ENV_API_KEY: "ck_live_xxx"})
        assert settings.control_plane_url == DEFAULT_BASE_URL
        assert settings.control_plane_url.startswith("https://")


# ============================================================
# resolve() defaults to os.environ when env= is omitted
# ============================================================


class TestResolveDefaultEnv:
    """When ``env=`` is omitted, ``resolve()`` reads from ``os.environ``.

    Monkeypatched env isolates these tests so they don't leak state.
    """

    def test_reads_os_environ_for_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_API_KEY, "ck_live_from_os_environ")
        monkeypatch.delenv(ENV_BASE_URL, raising=False)
        monkeypatch.delenv(ENV_AGENT_ID, raising=False)
        settings = resolve()
        assert settings.api_key == "ck_live_from_os_environ"

    def test_reads_os_environ_for_base_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ENV_API_KEY, raising=False)
        monkeypatch.setenv(ENV_BASE_URL, "https://from-env.example")
        monkeypatch.delenv(ENV_AGENT_ID, raising=False)
        settings = resolve()
        assert settings.control_plane_url == "https://from-env.example"

    def test_reads_os_environ_for_agent_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ENV_API_KEY, raising=False)
        monkeypatch.delenv(ENV_BASE_URL, raising=False)
        monkeypatch.setenv(ENV_AGENT_ID, "env_agent")
        settings = resolve()
        assert settings.agent_id == "env_agent"
