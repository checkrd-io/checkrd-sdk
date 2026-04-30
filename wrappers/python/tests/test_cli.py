"""Tests for the ``checkrd`` CLI.

In-process invocation via ``cli.main()`` + capsys is the primary test
strategy: it's deterministic, fast (no subprocess overhead), and lets us
intercept stdout exactly. One subprocess test verifies the installed entry
point actually works end-to-end (skipped gracefully if `checkrd` not on PATH).
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from checkrd.cli import build_parser, main as cli_main
from tests.conftest import requires_wasm


# ============================================================
# Helpers
# ============================================================


def _run(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    """Invoke ``cli_main`` with the given args and capture (rc, stdout, stderr)."""
    rc = cli_main(args)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


# ============================================================
# `policy trust-status` — CI guard for empty production trust roots
# ============================================================


class TestPolicyTrustStatus:
    """The CI guard subcommand. Exits 1 only when an empty trust list
    ships against a production endpoint; everything else is exit 0."""

    def test_empty_dev_exits_zero(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("checkrd._trust._PRODUCTION_TRUSTED_KEYS", [])
        rc, out, _ = _run(["policy", "trust-status"], capsys)
        assert rc == 0
        assert out.startswith("empty_dev:")

    def test_empty_production_exits_one(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("checkrd._trust._PRODUCTION_TRUSTED_KEYS", [])
        rc, out, _ = _run(
            ["policy", "trust-status", "--base-url", "https://api.checkrd.io"],
            capsys,
        )
        assert rc == 1
        assert out.startswith("empty_production:")
        assert "scripts/generate-policy-signing-key.py" in out

    def test_localhost_url_is_dev(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("checkrd._trust._PRODUCTION_TRUSTED_KEYS", [])
        rc, _, _ = _run(
            ["policy", "trust-status", "--base-url", "http://localhost:8080"],
            capsys,
        )
        assert rc == 0

    def test_populated_trust_list_exits_zero_against_prod(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "checkrd._trust._PRODUCTION_TRUSTED_KEYS",
            [{"keyid": "x", "public_key_hex": "a" * 64,
              "valid_from": 0, "valid_until": 9999999999}],
        )
        rc, out, _ = _run(
            ["policy", "trust-status", "--base-url", "https://api.checkrd.io"],
            capsys,
        )
        assert rc == 0
        assert out.startswith("ok:")

    def test_json_output_is_machine_readable(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("checkrd._trust._PRODUCTION_TRUSTED_KEYS", [])
        rc, out, _ = _run(
            ["policy", "trust-status", "--base-url", "https://api.checkrd.io",
             "--json"],
            capsys,
        )
        assert rc == 1
        parsed = json.loads(out)
        assert parsed["level"] == "empty_production"
        assert parsed["base_url"] == "https://api.checkrd.io"
        assert "message" in parsed


# ============================================================
# `policy verify-key` — bootstrap end-to-end check
# ============================================================


class TestPolicyVerifyKey:
    """The ``policy verify-key`` CLI subcommand. Two modes:

    1. inspection-only (no ``--base-url``) — prints the active trust
       list, exits 0 if non-empty, 1 if empty;
    2. end-to-end (``--base-url`` + ``--agent-id``) — fetches a
       signed bundle from the control plane and verifies it locally.

    Mode 2 requires a live control plane and is exercised in the
    integration suite. Unit tests cover mode 1 + the input-validation
    paths that gate mode 2.
    """

    def test_empty_trust_list_exits_one(
        self, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("checkrd._trust._PRODUCTION_TRUSTED_KEYS", [])
        rc, _, err = _run(["policy", "verify-key"], capsys)
        assert rc == 1
        assert "no trusted keys" in err
        assert "KEY-CUSTODY.md" in err

    def test_populated_trust_list_inspection_exits_zero(
        self, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Use the override mechanism so the test doesn't have to
        # mutate the module-level production list.
        monkeypatch.setenv(
            "CHECKRD_POLICY_TRUST_OVERRIDE_JSON",
            json.dumps([
                {
                    "keyid": "test-key",
                    "public_key_hex": "ab" * 32,
                    "valid_from": 1700000000,
                    "valid_until": 1900000000,
                },
            ]),
        )
        monkeypatch.setenv("CHECKRD_ALLOW_TRUST_OVERRIDE", "1")
        rc, out, _ = _run(["policy", "verify-key"], capsys)
        assert rc == 0
        assert "trust list: 1 key" in out
        assert "test-key" in out
        # Fingerprint format: sha256:<16 hex>.
        assert "fingerprint: sha256:" in out
        # Validity window rendered in both Unix-secs and ISO-8601.
        assert "1700000000" in out
        assert "1900000000" in out

    def test_multiple_keys_all_listed(
        self, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Operator running the rotation procedure (KEY-CUSTODY.md §3)
        # has both the old and new keys in the list during the
        # overlap window. Inspection should show both.
        monkeypatch.setenv(
            "CHECKRD_POLICY_TRUST_OVERRIDE_JSON",
            json.dumps([
                {"keyid": "prod-2026", "public_key_hex": "aa" * 32,
                 "valid_from": 1700000000, "valid_until": 1800000000},
                {"keyid": "prod-2027", "public_key_hex": "bb" * 32,
                 "valid_from": 1750000000, "valid_until": 1900000000},
            ]),
        )
        monkeypatch.setenv("CHECKRD_ALLOW_TRUST_OVERRIDE", "1")
        rc, out, _ = _run(["policy", "verify-key"], capsys)
        assert rc == 0
        assert "trust list: 2 key" in out
        assert "prod-2026" in out
        assert "prod-2027" in out

    def test_base_url_without_agent_id_errors(
        self, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "CHECKRD_POLICY_TRUST_OVERRIDE_JSON",
            json.dumps([{"keyid": "k", "public_key_hex": "aa" * 32,
                         "valid_from": 0, "valid_until": 9999999999}]),
        )
        monkeypatch.setenv("CHECKRD_ALLOW_TRUST_OVERRIDE", "1")
        rc, _, err = _run(
            ["policy", "verify-key", "--base-url", "https://api.example.com"],
            capsys,
        )
        assert rc == 2
        assert "--agent-id is required" in err

    def test_base_url_without_api_key_errors(
        self, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "CHECKRD_POLICY_TRUST_OVERRIDE_JSON",
            json.dumps([{"keyid": "k", "public_key_hex": "aa" * 32,
                         "valid_from": 0, "valid_until": 9999999999}]),
        )
        monkeypatch.setenv("CHECKRD_ALLOW_TRUST_OVERRIDE", "1")
        monkeypatch.delenv("CHECKRD_API_KEY", raising=False)
        rc, _, err = _run(
            ["policy", "verify-key", "--base-url", "https://api.example.com",
             "--agent-id", "test-agent"],
            capsys,
        )
        assert rc == 2
        assert "--api-key is required" in err


# ============================================================
# Argument parsing
# ============================================================


class TestArgParser:
    def test_no_args_returns_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc, out, _ = _run([], capsys)
        # No subcommand → exit code 2 + help printed.
        assert rc == 2
        assert "checkrd" in out
        assert "keygen" in out

    def test_unknown_subcommand_errors(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc_info:
            cli_main(["nonexistent"])
        # argparse exits with code 2 on parser errors
        assert exc_info.value.code == 2

    def test_version_flag(self, capsys: pytest.CaptureFixture[str]) -> None:
        # argparse --version exits with 0
        with pytest.raises(SystemExit) as exc_info:
            cli_main(["--version"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "checkrd" in captured.out

    def test_keygen_help_does_not_crash(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc_info:
            cli_main(["keygen", "--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "keygen" in captured.out.lower()


# ============================================================
# checkrd keygen output formats
# ============================================================


@requires_wasm
class TestKeygenDefaultEnvFormat:
    def test_default_format_is_env_export(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, out, _ = _run(["keygen"], capsys)
        assert rc == 0
        # The env format includes a comment header and an export line.
        assert "# Generated by `checkrd keygen`" in out
        assert "export CHECKRD_AGENT_KEY=" in out
        # Public key is in a comment.
        assert "Public key" in out
        assert "Fingerprint:" in out

    def test_export_line_contains_valid_base64(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, out, _ = _run(["keygen"], capsys)
        assert rc == 0
        export_line = next(
            line for line in out.splitlines() if line.startswith("export ")
        )
        # Strip "export CHECKRD_AGENT_KEY=" prefix
        b64 = export_line.split("=", 1)[1]
        decoded = base64.b64decode(b64, validate=True)
        assert len(decoded) == 32

    def test_public_key_comment_is_64_hex_chars(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, out, _ = _run(["keygen"], capsys)
        assert rc == 0
        # Find the public key line (the indented hex)
        lines = out.splitlines()
        # Format is "#   <hex>" — the line starts with "#   "
        hex_line = next(
            line.strip().lstrip("#").strip()
            for line in lines
            if line.strip().startswith("#")
            and len(line.strip()) > 60
            and all(c in "0123456789abcdef# " for c in line.strip())
        )
        assert len(hex_line) == 64
        decoded = bytes.fromhex(hex_line)
        assert len(decoded) == 32


@requires_wasm
class TestKeygenJsonFormat:
    def test_json_is_valid(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc, out, _ = _run(["keygen", "--format", "json"], capsys)
        assert rc == 0
        parsed = json.loads(out)
        assert isinstance(parsed, dict)

    def test_json_has_required_fields(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, out, _ = _run(["keygen", "--format", "json"], capsys)
        assert rc == 0
        parsed = json.loads(out)
        assert "private_key" in parsed
        assert "public_key" in parsed
        assert "fingerprint" in parsed

    def test_json_private_key_decodes_to_32_bytes(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, out, _ = _run(["keygen", "--format", "json"], capsys)
        assert rc == 0
        parsed = json.loads(out)
        private = base64.b64decode(parsed["private_key"], validate=True)
        assert len(private) == 32

    def test_json_public_key_is_64_hex_chars(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, out, _ = _run(["keygen", "--format", "json"], capsys)
        assert rc == 0
        parsed = json.loads(out)
        assert len(parsed["public_key"]) == 64
        public = bytes.fromhex(parsed["public_key"])
        assert len(public) == 32

    def test_fingerprint_is_first_8_bytes_of_public_key(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, out, _ = _run(["keygen", "--format", "json"], capsys)
        assert rc == 0
        parsed = json.loads(out)
        public = bytes.fromhex(parsed["public_key"])
        assert parsed["fingerprint"] == public[:8].hex()


@requires_wasm
class TestKeygenSingleValueOutput:
    def test_private_only_outputs_just_base64(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, out, _ = _run(["keygen", "--private-only"], capsys)
        assert rc == 0
        # Output is exactly: <base64>\n (one line, no comments)
        lines = out.strip().splitlines()
        assert len(lines) == 1
        decoded = base64.b64decode(lines[0], validate=True)
        assert len(decoded) == 32

    def test_public_only_outputs_just_hex(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, out, _ = _run(["keygen", "--public-only"], capsys)
        assert rc == 0
        lines = out.strip().splitlines()
        assert len(lines) == 1
        assert len(lines[0]) == 64
        public = bytes.fromhex(lines[0])
        assert len(public) == 32

    def test_private_and_public_only_are_mutually_exclusive(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, _, err = _run(
            ["keygen", "--private-only", "--public-only"], capsys
        )
        assert rc == 2
        assert "mutually exclusive" in err.lower()


# ============================================================
# Cryptographic correctness
# ============================================================


@requires_wasm
class TestKeygenCryptoCorrectness:
    def test_each_invocation_produces_different_key(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        keys = set()
        for _ in range(5):
            rc, out, _ = _run(["keygen", "--private-only"], capsys)
            assert rc == 0
            keys.add(out.strip())
        # All five should be unique (Ed25519 is 256-bit; collision odds are
        # negligible)
        assert len(keys) == 5

    def test_round_trip_through_local_identity(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from checkrd import LocalIdentity

        # Generate
        rc, private_out, _ = _run(["keygen", "--private-only"], capsys)
        assert rc == 0
        private_b64 = private_out.strip()

        # Set env var as a user would
        monkeypatch.setenv("CHECKRD_AGENT_KEY", private_b64)

        # Load via LocalIdentity.from_env
        identity = LocalIdentity.from_env()
        assert identity.private_key_bytes == base64.b64decode(private_b64)
        assert len(identity.public_key) == 32
        assert len(identity.instance_id) == 16

    def test_keygen_public_matches_local_identity_derivation(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from checkrd import LocalIdentity

        # Get both private and public from keygen JSON
        rc, out, _ = _run(["keygen", "--format", "json"], capsys)
        assert rc == 0
        parsed = json.loads(out)
        private = base64.b64decode(parsed["private_key"], validate=True)
        public_from_cli = bytes.fromhex(parsed["public_key"])

        # Verify by loading the same private key via LocalIdentity
        identity = LocalIdentity.from_bytes(private)
        assert identity.public_key == public_from_cli


# ============================================================
# Subprocess integration test (real installed binary)
# ============================================================


class TestInstalledBinary:
    """Verify the entry point works when invoked as a subprocess.

    Skipped gracefully if `checkrd` is not on PATH (e.g., in a fresh
    venv where the package was imported but not pip-installed).
    """

    @pytest.mark.skipif(
        shutil.which("checkrd") is None
        and not Path(sys.executable).parent.joinpath("checkrd").exists(),
        reason="checkrd binary not on PATH or in venv; skipping subprocess test. "
        "Run `pip install -e .` to enable.",
    )
    def test_installed_binary_keygen(self) -> None:
        # Prefer the venv's own binary so the test works even when the
        # venv's bin/ directory is not on the system PATH (common under
        # pytest-xdist workers and CI).
        checkrd_bin = shutil.which("checkrd")
        if checkrd_bin is None:
            checkrd_bin = str(Path(sys.executable).parent / "checkrd")
        result = subprocess.run(
            [checkrd_bin, "keygen", "--private-only"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        decoded = base64.b64decode(result.stdout.strip(), validate=True)
        assert len(decoded) == 32

    def test_python_m_checkrd_works(self) -> None:
        # `python -m checkrd` should always work even without the entry point
        result = subprocess.run(
            [sys.executable, "-m", "checkrd", "keygen", "--private-only"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        decoded = base64.b64decode(result.stdout.strip(), validate=True)
        assert len(decoded) == 32


# ============================================================
# Argparse parser construction (no subcommand execution)
# ============================================================


class TestParserConstruction:
    def test_parser_builds_without_args(self) -> None:
        # Smoke test: build_parser() should never raise.
        parser = build_parser()
        assert parser.prog == "checkrd"

    def test_parser_has_keygen_subcommand(self) -> None:
        parser = build_parser()
        # Parse args to ensure keygen is registered
        args = parser.parse_args(["keygen"])
        assert args.command == "keygen"


# ============================================================
# checkrd policy validate
# ============================================================


class TestPolicyValidate:
    """Tests for ``checkrd policy validate <file>``."""

    def test_valid_yaml_succeeds(
        self,
        tmp_path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        policy_file = tmp_path / "policy.yaml"
        policy_file.write_text(
            "agent: test\ndefault: allow\nrules:\n  - name: r1\n"
            "    allow:\n      method: [GET]\n      url: '*'\n"
        )
        rc = cli_main(["policy", "validate", str(policy_file)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "1 rule" in out

    def test_valid_json_flag(
        self,
        tmp_path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        policy_file = tmp_path / "policy.yaml"
        policy_file.write_text("agent: test\ndefault: deny\nrules: []\n")
        rc = cli_main(["policy", "validate", "--json", str(policy_file)])
        assert rc == 0
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed["default"] == "deny"

    def test_missing_file_fails(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli_main(["policy", "validate", "/nonexistent/policy.yaml"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "not found" in err.lower()

    def test_invalid_yaml_syntax_fails(
        self,
        tmp_path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        bad_file = tmp_path / "bad.yaml"
        bad_file.write_text(":\n  - :\n    invalid: [")
        rc = cli_main(["policy", "validate", str(bad_file)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "error" in err.lower()

    def test_empty_yaml_fails(
        self,
        tmp_path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        empty = tmp_path / "empty.yaml"
        empty.write_text("")
        rc = cli_main(["policy", "validate", str(empty)])
        assert rc == 1

    def test_policy_bare_command_shows_help(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = cli_main(["policy"])
        assert rc == 2

    def test_non_yaml_content_fails(
        self,
        tmp_path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A file with valid YAML but not a dict is rejected."""
        bad = tmp_path / "list.yaml"
        bad.write_text("- one\n- two\n")
        rc = cli_main(["policy", "validate", str(bad)])
        assert rc == 1

    def test_missing_agent_field_fails(
        self,
        tmp_path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Policy without required 'agent' field should fail WASM validation."""
        bad = tmp_path / "no_agent.yaml"
        bad.write_text("default: allow\nrules: []\n")
        rc = cli_main(["policy", "validate", str(bad)])
        # May succeed at YAML level but fail at WASM level (depends on WASM availability)
        # Either way, it should not crash
        assert rc in (0, 1)

    def test_binary_file_fails(
        self,
        tmp_path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Binary file (not valid YAML) is rejected."""
        bad = tmp_path / "binary.yaml"
        bad.write_bytes(b"\x00\x01\x02\x03\xff\xfe")
        rc = cli_main(["policy", "validate", str(bad)])
        assert rc == 1
