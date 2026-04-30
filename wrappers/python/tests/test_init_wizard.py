"""Tests for ``checkrd init`` CLI wizard.

Every wizard step is tested in isolation with mocked I/O so the tests
are fast, deterministic, and don't touch the network. The full wizard
flow is tested via ``run_wizard()`` with all HTTP calls mocked.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from checkrd._init_wizard import (
    detect_existing_config,
    generate_keypair,
    print_code_snippet,
    register_agent,
    register_public_key,
    resolve_agent_id,
    resolve_api_key,
    run_wizard,
    verify_connection,
    write_env_file,
)
from checkrd.cli import build_parser, main
from tests.conftest import requires_wasm


# ============================================================
# detect_existing_config
# ============================================================


class TestDetectExistingConfig:
    def test_empty_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        for k in ("CHECKRD_API_KEY", "CHECKRD_BASE_URL", "CHECKRD_AGENT_ID"):
            monkeypatch.delenv(k, raising=False)
        assert detect_existing_config() == {}

    def test_reads_env_vars(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CHECKRD_API_KEY", "ck_test_abc")
        found = detect_existing_config()
        assert found["CHECKRD_API_KEY"] == "ck_test_abc"

    def test_reads_dot_env_file(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        for k in ("CHECKRD_API_KEY", "CHECKRD_BASE_URL", "CHECKRD_AGENT_ID"):
            monkeypatch.delenv(k, raising=False)
        (tmp_path / ".env").write_text("CHECKRD_AGENT_ID=from-file\n")
        found = detect_existing_config()
        assert found["CHECKRD_AGENT_ID"] == "from-file"


# ============================================================
# resolve_api_key
# ============================================================


class TestResolveApiKey:
    def test_explicit_key_wins(self) -> None:
        assert resolve_api_key(explicit_key="ck_test_explicit") == "ck_test_explicit"

    def test_env_var_fallback(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CHECKRD_API_KEY", "ck_test_env")
        assert resolve_api_key() == "ck_test_env"

    def test_non_interactive_returns_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("CHECKRD_API_KEY", raising=False)
        assert resolve_api_key(interactive=False) is None


# ============================================================
# resolve_agent_id
# ============================================================


class TestResolveAgentId:
    def test_explicit_wins(self) -> None:
        assert resolve_agent_id(explicit_id="my-agent") == "my-agent"

    def test_env_fallback(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CHECKRD_AGENT_ID", "env-agent")
        assert resolve_agent_id(interactive=False) == "env-agent"

    def test_derived_fallback(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("CHECKRD_AGENT_ID", raising=False)
        result = resolve_agent_id(interactive=False)
        assert result  # non-empty derived value


# ============================================================
# generate_keypair
# ============================================================


@requires_wasm
class TestGenerateKeypair:
    def test_returns_b64_and_hex(self) -> None:
        import base64

        private_b64, public_hex = generate_keypair()
        # Private key is valid base64 -> 32 bytes.
        private_bytes = base64.b64decode(private_b64)
        assert len(private_bytes) == 32
        # Public key is valid hex -> 32 bytes.
        assert len(bytes.fromhex(public_hex)) == 32


# ============================================================
# register_agent / register_public_key
# ============================================================


class TestRegisterAgent:
    def test_success_returns_id(self) -> None:
        response = MagicMock()
        response.status = 201
        response.read.return_value = json.dumps({"id": "agent-uuid"}).encode()
        response.__enter__ = lambda s: s
        response.__exit__ = MagicMock(return_value=False)

        with patch("checkrd._init_wizard.urlopen", return_value=response):
            result = register_agent(
                base_url="https://api.checkrd.io",
                api_key="ck_test_x",
                agent_id="test-agent",
            )
        assert result == "agent-uuid"

    def test_network_error_returns_none(self) -> None:
        with patch(
            "checkrd._init_wizard.urlopen",
            side_effect=OSError("connection refused"),
        ):
            result = register_agent(
                base_url="https://api.checkrd.io",
                api_key="ck_test_x",
                agent_id="test-agent",
            )
        assert result is None


class TestRegisterPublicKey:
    def test_success_returns_true(self) -> None:
        response = MagicMock()
        response.status = 200
        response.__enter__ = lambda s: s
        response.__exit__ = MagicMock(return_value=False)

        with patch("checkrd._init_wizard.urlopen", return_value=response):
            ok = register_public_key(
                base_url="https://api.checkrd.io",
                api_key="ck_test_x",
                agent_id="agent-uuid",
                public_key_hex="ab" * 32,
            )
        assert ok is True

    def test_409_is_idempotent(self) -> None:
        from urllib.error import HTTPError

        err = HTTPError("url", 409, "conflict", {}, None)  # type: ignore[arg-type]
        with patch("checkrd._init_wizard.urlopen", side_effect=err):
            ok = register_public_key(
                base_url="https://api.checkrd.io",
                api_key="ck_test_x",
                agent_id="agent-uuid",
                public_key_hex="ab" * 32,
            )
        assert ok is True


# ============================================================
# verify_connection
# ============================================================


class TestVerifyConnection:
    def test_success(self) -> None:
        response = MagicMock()
        response.status = 200
        response.__enter__ = lambda s: s
        response.__exit__ = MagicMock(return_value=False)

        with patch("checkrd._init_wizard.urlopen", return_value=response):
            assert verify_connection(
                base_url="https://api.checkrd.io",
                api_key="ck_test_x",
            )

    def test_failure_returns_false(self) -> None:
        with patch(
            "checkrd._init_wizard.urlopen",
            side_effect=OSError("timeout"),
        ):
            assert not verify_connection(
                base_url="https://api.checkrd.io",
                api_key="ck_test_x",
            )


# ============================================================
# write_env_file
# ============================================================


class TestWriteEnvFile:
    def test_writes_env_vars(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        write_env_file(
            api_key="ck_test_abc",
            agent_id="my-agent",
            agent_key_b64="key123==",
            path=path,
        )
        content = path.read_text()
        assert "CHECKRD_API_KEY=ck_test_abc" in content
        assert "CHECKRD_AGENT_ID=my-agent" in content
        assert "CHECKRD_AGENT_KEY=key123==" in content

    def test_preserves_existing_non_checkrd_lines(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("DATABASE_URL=postgres://localhost\n")
        write_env_file(
            api_key="ck_test_abc",
            agent_id="my-agent",
            agent_key_b64="key123==",
            path=path,
        )
        content = path.read_text()
        assert "DATABASE_URL=postgres://localhost" in content
        assert "CHECKRD_API_KEY=ck_test_abc" in content

    def test_replaces_existing_checkrd_lines(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("CHECKRD_API_KEY=old_key\nOTHER=value\n")
        write_env_file(
            api_key="new_key",
            agent_id="my-agent",
            agent_key_b64="key123==",
            path=path,
        )
        content = path.read_text()
        assert "old_key" not in content
        assert "CHECKRD_API_KEY=new_key" in content
        assert "OTHER=value" in content

    def test_no_api_key_omits_line(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        write_env_file(
            api_key=None,
            agent_id="my-agent",
            agent_key_b64="key123==",
            path=path,
        )
        content = path.read_text()
        assert "CHECKRD_API_KEY" not in content
        assert "CHECKRD_AGENT_ID=my-agent" in content


# ============================================================
# print_code_snippet
# ============================================================


class TestPrintCodeSnippet:
    def test_with_api_key(self, capsys: pytest.CaptureFixture[str]) -> None:
        print_code_snippet(agent_id="my-agent", has_api_key=True)
        output = capsys.readouterr().out
        assert "checkrd.init()" in output
        assert "checkrd.instrument()" in output

    def test_without_api_key(self, capsys: pytest.CaptureFixture[str]) -> None:
        print_code_snippet(agent_id="my-agent", has_api_key=False)
        output = capsys.readouterr().out
        assert "my-agent" in output
        assert "CHECKRD_API_KEY" in output


# ============================================================
# CLI integration: `checkrd init` argparse
# ============================================================


class TestCliInit:
    def test_init_subcommand_parses(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["init", "--non-interactive", "--api-key", "ck_test_x"])
        assert args.command == "init"
        assert args.api_key == "ck_test_x"
        assert args.non_interactive is True

    def test_init_help_shows_description(self, capsys: pytest.CaptureFixture[str]) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["init", "--help"])
        output = capsys.readouterr().out
        assert "wizard" in output.lower() or "bootstrap" in output.lower()


# ============================================================
# Full wizard flow (non-interactive, mocked HTTP)
# ============================================================


@requires_wasm
class TestRunWizard:
    def test_non_interactive_with_api_key(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Non-interactive mode with explicit API key: should write .env
        and attempt registration without any prompts."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CHECKRD_CONFIG_DIR", str(tmp_path))
        for k in ("CHECKRD_API_KEY", "CHECKRD_AGENT_ID"):
            monkeypatch.delenv(k, raising=False)

        # Mock all HTTP calls to succeed.
        ok_response = MagicMock()
        ok_response.status = 200
        ok_response.read.return_value = json.dumps({"id": "agent-uuid"}).encode()
        ok_response.__enter__ = lambda s: s
        ok_response.__exit__ = MagicMock(return_value=False)

        with patch("checkrd._init_wizard.urlopen", return_value=ok_response):
            rc = run_wizard(
                api_key="ck_test_wizard",
                agent_id="wizard-agent",
                non_interactive=True,
                env_file=str(tmp_path / ".env"),
            )

        assert rc == 0
        content = (tmp_path / ".env").read_text()
        assert "CHECKRD_API_KEY=ck_test_wizard" in content
        assert "CHECKRD_AGENT_ID=wizard-agent" in content
        assert "CHECKRD_AGENT_KEY=" in content

    def test_non_interactive_offline(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No API key, non-interactive: should generate a keypair and
        write a local-only .env without attempting registration."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CHECKRD_CONFIG_DIR", str(tmp_path))
        for k in ("CHECKRD_API_KEY", "CHECKRD_AGENT_ID"):
            monkeypatch.delenv(k, raising=False)

        rc = run_wizard(
            agent_id="offline-agent",
            non_interactive=True,
            env_file=str(tmp_path / ".env"),
        )

        assert rc == 0
        content = (tmp_path / ".env").read_text()
        assert "CHECKRD_API_KEY" not in content  # no key to write
        assert "CHECKRD_AGENT_ID=offline-agent" in content
        assert "CHECKRD_AGENT_KEY=" in content  # keypair still generated

    def test_network_failure_does_not_crash(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """All HTTP calls fail: wizard should still complete with a local
        .env and exit 0."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CHECKRD_CONFIG_DIR", str(tmp_path))

        with patch(
            "checkrd._init_wizard.urlopen",
            side_effect=OSError("no network"),
        ):
            rc = run_wizard(
                api_key="ck_test_x",
                agent_id="test",
                non_interactive=True,
                env_file=str(tmp_path / ".env"),
            )

        assert rc == 0
        assert (tmp_path / ".env").exists()

    def test_cli_entry_point(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify ``checkrd init --non-interactive`` works through the
        argparse entry point."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CHECKRD_CONFIG_DIR", str(tmp_path))
        for k in ("CHECKRD_API_KEY", "CHECKRD_AGENT_ID"):
            monkeypatch.delenv(k, raising=False)

        rc = main([
            "init",
            "--non-interactive",
            "--agent-id", "cli-agent",
            "--env-file", str(tmp_path / ".env"),
        ])
        assert rc == 0
        assert (tmp_path / ".env").exists()
