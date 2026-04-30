"""Checkrd command-line interface.

Operator utilities for production deployment of the Checkrd Python SDK.

Subcommands:

- ``checkrd keygen`` — generate an Ed25519 keypair for an agent identity.
  The private key is what you put in your secrets manager; the public key is
  what you register with the Checkrd control plane (or omit entirely if you're
  running fully offline).

The CLI is registered as a console script in ``pyproject.toml``::

    [project.scripts]
    checkrd = "checkrd.cli:main"

so ``pip install checkrd`` makes ``checkrd`` available on PATH. The
``__main__`` module also lets you invoke it via ``python -m checkrd`` for
environments where the script directory isn't on PATH (containers, etc.).
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from typing import Optional, Sequence

from checkrd._version import __version__


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for the ``checkrd`` console script.

    Returns the process exit code (0 on success, non-zero on error). When
    invoked via the ``[project.scripts]`` entry point, the return value is
    passed to :func:`sys.exit`. The CLI is split into subcommands so future
    operator utilities can be added without breaking the keygen interface.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    rc: int = args.func(args)
    return rc


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser. Exposed for testing."""
    parser = argparse.ArgumentParser(
        prog="checkrd",
        description="Checkrd CLI: operator utilities for the Python SDK.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"checkrd {__version__}",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # keygen
    keygen = sub.add_parser(
        "keygen",
        help="Generate an Ed25519 keypair for an agent identity.",
        description=(
            "Generate a new Ed25519 keypair for use as a Checkrd agent identity. "
            "The private key is base64-encoded for placement in a secrets manager "
            "and consumption via LocalIdentity.from_env(). The public key is hex-"
            "encoded for registration with the control plane."
        ),
    )
    keygen.add_argument(
        "--format",
        choices=["env", "json"],
        default="env",
        help="Output format. 'env' (default): shell export with comments. "
        "'json': machine-readable.",
    )
    keygen.add_argument(
        "--private-only",
        action="store_true",
        help="Print only the base64-encoded private key (no comments, "
        "no newline tricks). Useful for: export "
        "CHECKRD_AGENT_KEY=$(checkrd keygen --private-only)",
    )
    keygen.add_argument(
        "--public-only",
        action="store_true",
        help="Print only the hex-encoded public key. Useful for "
        "registration scripts.",
    )
    keygen.set_defaults(func=cmd_keygen)

    # init
    init_cmd = sub.add_parser(
        "init",
        help="Set up Checkrd for this project (interactive wizard).",
        description=(
            "Interactive bootstrap wizard. Walks you through authentication, "
            "agent creation, keypair generation, and .env file setup. "
            "After running this, add `import checkrd; checkrd.init(); "
            "checkrd.instrument()` to your code."
        ),
    )
    init_cmd.add_argument("--api-key", help="API key (skip the authentication prompt).")
    init_cmd.add_argument("--agent-id", help="Agent name (skip the prompt).")
    init_cmd.add_argument(
        "--base-url",
        default="https://api.checkrd.io",
        help="Control plane base URL (default: https://api.checkrd.io).",
    )
    init_cmd.add_argument(
        "--env-file", default=".env", help="Path to write the .env file (default: .env).",
    )
    init_cmd.add_argument(
        "--non-interactive", action="store_true", help="Skip all prompts.",
    )
    init_cmd.set_defaults(func=cmd_init)

    # policy (subcommand group)
    policy_cmd = sub.add_parser("policy", help="Policy management commands.")
    policy_sub = policy_cmd.add_subparsers(dest="policy_command", metavar="<subcommand>")

    validate_cmd = policy_sub.add_parser(
        "validate",
        help="Validate a policy file.",
        description=(
            "Load a policy YAML or JSON file through the same path the SDK "
            "uses at runtime. Reports syntax errors, schema errors, and on "
            "success prints a summary. Exit code 0 on success, 1 on failure."
        ),
    )
    validate_cmd.add_argument("file", help="Path to the policy YAML or JSON file.")
    validate_cmd.add_argument(
        "--json", action="store_true", dest="output_json",
        help="Output the parsed policy as pretty-printed JSON.",
    )
    validate_cmd.set_defaults(func=cmd_policy_validate)

    # policy trust-status — operator + CI guard. Inspects the current
    # production trust list against the supplied base URL and exits 0
    # when the SDK is shippable, 1 when it's not. Wire into pre-publish
    # CI so an empty _PRODUCTION_TRUSTED_KEYS blocks the release tag.
    trust_status_cmd = policy_sub.add_parser(
        "trust-status",
        help="Report the trust-list state and exit non-zero on misconfiguration.",
        description=(
            "Diagnose checkrd._trust._PRODUCTION_TRUSTED_KEYS for the "
            "supplied base URL. Exit 0 when the SDK is shippable for "
            "that environment (production keys present, or running "
            "against dev). Exit 1 when production keys are missing AND "
            "the URL targets a production endpoint — used as a CI guard "
            "before tag-triggered PyPI publishes."
        ),
    )
    trust_status_cmd.add_argument(
        "--base-url",
        default=None,
        help=(
            "Control plane URL the trust list is being evaluated against. "
            "Production-shaped URLs (containing 'checkrd.io') treat an "
            "empty trust list as a release blocker; other URLs treat it "
            "as a dev-mode warning."
        ),
    )
    trust_status_cmd.add_argument(
        "--json",
        action="store_true",
        dest="output_json",
        help="Emit a machine-readable JSON object instead of text.",
    )
    trust_status_cmd.set_defaults(func=cmd_policy_trust_status)

    # policy verify-key — end-to-end proof that the AWS Secrets
    # Manager private key + the SDK's pinned public key match.
    # Operator runs this after the bootstrap ceremony documented in
    # KEY-CUSTODY.md §2 to confirm the round trip.
    verify_key_cmd = policy_sub.add_parser(
        "verify-key",
        help="Inspect the trust list and (optionally) verify against a live control plane.",
        description=(
            "Two modes:\n"
            "\n"
            "  1. Without ``--base-url`` — print the active trust list "
            "(keyids, fingerprints, validity windows) and exit. Useful "
            "for confirming what the SDK considers ground truth.\n"
            "\n"
            "  2. With ``--base-url`` and ``--agent-id`` — connect to "
            "the control plane's ``GET /v1/agents/{id}/control/state`` "
            "endpoint, fetch any signed policy bundle present, and "
            "verify the signature locally against the pinned trust "
            "list. End-to-end proof that the bootstrap ceremony "
            "(private key in AWS, public key in SDK) lined up.\n"
            "\n"
            "Exit 0 on success; non-zero on any failure with a "
            "specific reason code on stderr."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    verify_key_cmd.add_argument(
        "--base-url",
        default=None,
        help=(
            "Control plane URL. When omitted, the command runs in "
            "trust-list-inspection mode (no network call)."
        ),
    )
    verify_key_cmd.add_argument(
        "--agent-id",
        default=None,
        help="Agent ID to fetch state for. Required with --base-url.",
    )
    verify_key_cmd.add_argument(
        "--api-key",
        default=None,
        help=(
            "Control-plane API key. Falls back to ``CHECKRD_API_KEY``. "
            "Required with --base-url."
        ),
    )
    verify_key_cmd.set_defaults(func=cmd_policy_verify_key)

    # Wire the bare `checkrd policy` (no subcommand) to print help.
    def _policy_help(args: argparse.Namespace) -> int:
        policy_cmd.print_help()
        return 2

    policy_cmd.set_defaults(func=_policy_help)

    return parser


def cmd_keygen(args: argparse.Namespace) -> int:
    """Generate a keypair and print it according to the requested format."""
    # Lazy import: keygen is the only command today, but importing the WASM
    # engine for `checkrd --version` would be wasteful.
    from checkrd.engine import WasmEngine

    if args.private_only and args.public_only:
        print(
            "error: --private-only and --public-only are mutually exclusive",
            file=sys.stderr,
        )
        return 2

    private_bytes, public_bytes = WasmEngine.generate_keypair()
    private_b64 = base64.b64encode(private_bytes).decode("ascii")
    public_hex = public_bytes.hex()
    fingerprint = public_bytes[:8].hex()

    if args.private_only:
        print(private_b64)
        return 0

    if args.public_only:
        print(public_hex)
        return 0

    if args.format == "json":
        print(
            json.dumps(
                {
                    "private_key": private_b64,
                    "public_key": public_hex,
                    "fingerprint": fingerprint,
                },
                indent=2,
            )
        )
        return 0

    # Default: env format. Shell-pasteable export with comments.
    print("# Generated by `checkrd keygen`")
    print("# Public key (register via dashboard or POST /v1/agents/<id>/public-key):")
    print(f"#   {public_hex}")
    print(f"# Fingerprint: {fingerprint}")
    print(f"export CHECKRD_AGENT_KEY={private_b64}")
    return 0


def cmd_policy_validate(args: argparse.Namespace) -> int:
    """Validate a policy file and print the result."""
    import json as json_mod
    from pathlib import Path

    from checkrd.config import load_config
    from checkrd.exceptions import CheckrdInitError

    path = Path(args.file)
    if not path.exists():
        print(f"error: file not found: {path}", file=sys.stderr)
        return 1

    # Step 1: Parse YAML/JSON via the same path the SDK uses.
    try:
        policy_json = load_config(policy=path)
    except CheckrdInitError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    parsed = json_mod.loads(policy_json)

    # Step 2: Optionally validate through the WASM engine.
    wasm_ok = False
    try:
        from checkrd.engine import WasmEngine

        WasmEngine(policy_json, parsed.get("agent", "validate-check"))
        wasm_ok = True
    except CheckrdInitError as e:
        # WASM binary missing — fall back to YAML-only validation.
        if "WASM module not found" in str(e) or "Failed to instantiate" in str(e):
            print(
                "warning: WASM binary not available; YAML syntax is valid "
                "but full schema validation was skipped.",
                file=sys.stderr,
            )
        else:
            print(f"error: policy schema invalid: {e}", file=sys.stderr)
            return 1
    except Exception as e:
        print(f"error: unexpected validation failure: {e}", file=sys.stderr)
        return 1

    # Step 3: Output.
    if args.output_json:
        print(json_mod.dumps(parsed, indent=2))
    else:
        agent = parsed.get("agent", "?")
        default = parsed.get("default", "?")
        rules = parsed.get("rules", [])
        status = "valid" if wasm_ok else "valid (YAML only)"
        print(f"Policy {status}: agent={agent}, default={default}, {len(rules)} rule(s)")

    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Run the interactive ``checkrd init`` wizard."""
    from checkrd._init_wizard import run_wizard

    return run_wizard(
        api_key=args.api_key,
        agent_id=args.agent_id,
        base_url=args.base_url,
        env_file=args.env_file,
        non_interactive=args.non_interactive,
    )


def cmd_policy_verify_key(args: argparse.Namespace) -> int:
    """Inspect the trust list and (optionally) verify against a live
    control plane. See :func:`build_parser` for the help text and
    exit-code contract.

    Two modes:

    - **Inspection only** — when ``--base-url`` is omitted. Prints
      a tabular view of the current trust list and exits 0 if it's
      non-empty, 1 if it's empty (the SDK in this state will reject
      every signed bundle).
    - **End-to-end** — when ``--base-url`` is provided. Hits the
      control plane's ``GET /v1/agents/{id}/control/state`` endpoint,
      fetches any signed policy bundle, and runs it through the WASM
      core's ``reload_policy_signed`` against the pinned trust list.
      Success means the AWS Secrets Manager private key and the
      pinned public key actually match. Failure surfaces the
      specific :class:`PolicySignatureError` reason
      (``signature_invalid`` / ``unknown_or_no_signer`` / etc.).
    """
    import datetime as _dt
    import hashlib as _hashlib
    import json as _json

    from checkrd._trust import trusted_policy_keys

    keys = trusted_policy_keys()
    if not keys:
        print(
            "error: no trusted keys configured.\n"
            "       _PRODUCTION_TRUSTED_KEYS is empty AND no override is "
            "active. Every signed policy update will be rejected. Run "
            "the bootstrap ceremony in KEY-CUSTODY.md §2.",
            file=sys.stderr,
        )
        return 1

    # Inspection-only mode: print the trust list and exit.
    if not args.base_url:
        print(f"trust list: {len(keys)} key(s)")
        for k in keys:
            pk_hex = str(k.get("public_key_hex", ""))
            fp = (
                _hashlib.sha256(bytes.fromhex(pk_hex)).hexdigest()[:16]
                if pk_hex
                else "<no public key>"
            )
            valid_from = int(k.get("valid_from", 0))
            valid_until = int(k.get("valid_until", 0))
            print(f"  - keyid:       {k.get('keyid', '<unset>')}")
            print(f"    fingerprint: sha256:{fp}")
            print(
                f"    valid_from:  {valid_from} "
                f"({_dt.datetime.fromtimestamp(valid_from, _dt.timezone.utc).isoformat()})"
            )
            print(
                f"    valid_until: {valid_until} "
                f"({_dt.datetime.fromtimestamp(valid_until, _dt.timezone.utc).isoformat()})"
            )
        return 0

    # End-to-end mode: actually verify against the control plane.
    import os as _os
    import time as _time

    import httpx as _httpx

    from checkrd.engine import WasmEngine
    from checkrd.exceptions import PolicySignatureError

    if args.agent_id is None:
        print(
            "error: --agent-id is required when --base-url is provided.",
            file=sys.stderr,
        )
        return 2
    api_key = args.api_key or _os.environ.get("CHECKRD_API_KEY")
    if not api_key:
        print(
            "error: --api-key is required when --base-url is provided "
            "(or set CHECKRD_API_KEY).",
            file=sys.stderr,
        )
        return 2

    url = (
        f"{args.base_url.rstrip('/')}/v1/agents/{args.agent_id}/control/state"
    )
    try:
        resp = _httpx.get(
            url, headers={"X-API-Key": api_key}, timeout=10.0,
        )
        resp.raise_for_status()
    except _httpx.HTTPStatusError as exc:
        print(
            f"error: control plane returned HTTP {exc.response.status_code}: "
            f"{exc.response.text[:200]}",
            file=sys.stderr,
        )
        return 3
    except _httpx.HTTPError as exc:
        print(f"error: could not reach {url}: {exc}", file=sys.stderr)
        return 3

    state = resp.json()
    envelope = state.get("policy_envelope")
    if envelope is None:
        print(
            "error: control plane response has no `policy_envelope` field.\n"
            "       The agent has no signed policy yet — push one from "
            "the dashboard or via the policy-publish API and re-run.",
            file=sys.stderr,
        )
        return 4

    # Construct a transient engine to drive the FFI verifier. Any
    # default policy works — the verifier exercises only the DSSE
    # bundle path, not the active policy.
    engine = WasmEngine(
        policy_json=_json.dumps({"agent": args.agent_id, "default": "deny", "rules": []}),
        agent_id=args.agent_id,
    )
    try:
        engine.reload_policy_signed(
            _json.dumps(envelope),
            _json.dumps(keys),
            int(_time.time()),
            86_400,  # max age — match the production default
        )
    except PolicySignatureError as exc:
        print(
            f"error: signature verification failed: {exc.reason} "
            f"(ffi_code={exc.ffi_code}).\n"
            f"       The control plane signed with a key the SDK does "
            f"not trust. Either re-run the bootstrap ceremony or "
            f"verify the AWS Secrets Manager value matches the pinned "
            f"public key fingerprint(s) printed above.",
            file=sys.stderr,
        )
        return 5

    keyid = (envelope.get("signatures", [{}])[0] or {}).get("keyid", "<unknown>")
    print(f"ok: signature verified against keyid {keyid}")
    print("    bootstrap is correct: AWS Secrets Manager private key")
    print("    matches the SDK's pinned public key.")
    return 0


def cmd_policy_trust_status(args: argparse.Namespace) -> int:
    """Report trust-list state; exit 1 only when an empty list ships against prod.

    Maps the four states from :func:`checkrd._trust.production_trust_status`
    onto exit codes:

    - ``ok``, ``override``, ``empty_dev`` → exit 0
    - ``empty_production``                → exit 1

    The override case prints a warning so the CI logs surface that the
    workflow is using a non-production trust set; only empty-against-prod
    fails the build.
    """
    from checkrd._trust import production_trust_status

    level, message = production_trust_status(base_url=args.base_url)

    if args.output_json:
        print(
            json.dumps(
                {"level": level, "message": message, "base_url": args.base_url},
                indent=2,
            )
        )
    else:
        prefix = "ok:" if level == "ok" else f"{level}:"
        print(f"{prefix} {message}")

    return 1 if level == "empty_production" else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
