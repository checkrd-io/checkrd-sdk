"""Interactive bootstrap wizard for ``checkrd init``.

Guides a first-time user through the full setup:

1. Detect existing configuration (reuse or replace).
2. Authenticate with the control plane (browser flow or ``--api-key``).
3. Create an agent on the control plane.
4. Generate an Ed25519 keypair and register the public key.
5. Write a ``.env`` file with all the resolved credentials.
6. Print a ready-to-paste Python code snippet.
7. Confirm the round-trip by pinging the control plane.

Every step has a non-interactive fallback (``--non-interactive`` flag or
``sys.stdin.isatty() == False``), so the wizard is usable in CI scripts
that pipe flags in. If the control plane is unreachable or returns an
unexpected error, the wizard falls back to local-only setup (keygen +
``.env``, no registration) and prints a warning.

**No new dependencies.** The wizard uses only the stdlib (``urllib``,
``webbrowser``, ``json``, ``input``) and the existing Checkrd SDK
internals (``WasmEngine.generate_keypair``, ``_settings.resolve``).
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from checkrd._settings import (
    DEFAULT_BASE_URL,
    ENV_AGENT_ID,
    ENV_API_KEY,
    ENV_BASE_URL,
    derive_agent_id,
)

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

_USE_COLOR = sys.stdout.isatty()


def _green(text: str) -> str:
    return f"\033[32m{text}\033[0m" if _USE_COLOR else text


def _bold(text: str) -> str:
    return f"\033[1m{text}\033[0m" if _USE_COLOR else text


def _dim(text: str) -> str:
    return f"\033[2m{text}\033[0m" if _USE_COLOR else text


def _prompt(message: str, *, default: str = "") -> str:
    """Interactive prompt with a default value. Returns the stripped answer."""
    if default:
        raw = input(f"{message} [{default}]: ").strip()
        return raw if raw else default
    return input(f"{message}: ").strip()


# ---------------------------------------------------------------------------
# Wizard steps
# ---------------------------------------------------------------------------


def detect_existing_config() -> dict[str, str]:
    """Check for existing Checkrd env vars and .env file.

    Returns a dict of the vars that are already set (empty dict if none).
    """
    found: dict[str, str] = {}
    for key in (ENV_API_KEY, ENV_BASE_URL, ENV_AGENT_ID, "CHECKRD_AGENT_KEY"):
        value = os.environ.get(key)
        if value:
            found[key] = value

    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("CHECKRD_") and key not in found:
                found[key] = value.strip().strip("\"'")
    return found


def resolve_api_key(
    *,
    explicit_key: Optional[str] = None,
    base_url: str = DEFAULT_BASE_URL,
    interactive: bool = True,
) -> Optional[str]:
    """Get an API key: explicit flag > env var > browser auth > manual paste.

    Returns the API key string, or ``None`` if the user chooses to skip.
    """
    if explicit_key:
        return explicit_key

    env_key = os.environ.get(ENV_API_KEY)
    if env_key:
        return env_key

    if not interactive:
        return None

    print()
    print(_bold("Step 1: Authentication"))
    print()
    print("Get an API key from the Checkrd dashboard:")
    dashboard_url = base_url.replace("api.", "app.").rstrip("/")
    print(f"  {_green(dashboard_url + '/settings/api-keys')}")
    print()

    api_key = _prompt("Paste your API key (ck_live_... or ck_test_...)")
    if not api_key:
        print(_dim("  Skipped — you can set CHECKRD_API_KEY later."))
        return None

    if not api_key.startswith("ck_"):
        print(
            f"  Warning: API keys usually start with 'ck_live_' or 'ck_test_'."
            f" Got: {api_key[:12]}..."
        )

    return api_key


def resolve_agent_id(
    *,
    explicit_id: Optional[str] = None,
    interactive: bool = True,
) -> str:
    """Determine the agent_id: explicit flag > env var > prompt > derived.

    The wizard is the one place where a hostname-less environment is
    recoverable — we just prompt for a value. ``derive_agent_id`` is
    fail-closed in production, so we catch its ``CheckrdInitError`` and
    let the operator type one in. In non-interactive mode the error
    propagates: a CI / setup script that cannot derive an id is a real
    misconfiguration that should be surfaced.
    """
    from checkrd.exceptions import CheckrdInitError

    if explicit_id:
        return explicit_id

    env_id = os.environ.get(ENV_AGENT_ID)
    if env_id:
        return env_id

    try:
        derived: Optional[str] = derive_agent_id()
    except CheckrdInitError:
        if not interactive:
            raise
        derived = None

    if not interactive:
        # ``derived`` is non-None here: derive_agent_id either returned
        # a value or raised, and we re-raised non-interactive failures
        # above.
        if derived is None:
            raise RuntimeError(
                "internal: derive_agent_id() returned None in "
                "non-interactive mode (should have raised instead)"
            )
        return derived

    print()
    print(_bold("Step 2: Agent Identity"))
    print()
    if derived is None:
        # The fall-through path when no hostname / PaaS env var is
        # available: prompt with no default.
        return _prompt("Agent name (no default — set explicitly)")
    return _prompt("Agent name", default=derived)


def generate_keypair() -> tuple[str, str]:
    """Generate an Ed25519 keypair using the WASM engine.

    Returns ``(private_key_b64, public_key_hex)``.
    """
    from checkrd.engine import WasmEngine

    private_bytes, public_bytes = WasmEngine.generate_keypair()
    return base64.b64encode(private_bytes).decode("ascii"), public_bytes.hex()


def register_agent(
    *,
    base_url: str,
    api_key: str,
    agent_id: str,
) -> Optional[str]:
    """Create an agent on the control plane. Returns the agent UUID or None."""
    url = f"{base_url.rstrip('/')}/v1/agents"
    body = json.dumps({"name": agent_id}).encode("utf-8")
    req = Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=10) as resp:
            data: dict[str, str] = json.loads(resp.read())
            return data.get("id") or data.get("agent_id")
    except HTTPError as e:
        if e.code == 409:
            # Agent already exists — that's fine for idempotent init.
            try:
                data_err: dict[str, str] = json.loads(e.read())
                return data_err.get("id") or data_err.get("agent_id")
            except Exception:
                return agent_id
        print(f"  Warning: could not create agent (HTTP {e.code}). Continuing locally.")
        return None
    except (URLError, TimeoutError, OSError) as e:
        print(f"  Warning: could not reach control plane ({e}). Continuing locally.")
        return None


def register_public_key(
    *,
    base_url: str,
    api_key: str,
    agent_id: str,
    public_key_hex: str,
) -> bool:
    """Register the agent's public key with the control plane."""
    url = f"{base_url.rstrip('/')}/v1/agents/{agent_id}/public-key"
    body = json.dumps({"public_key": public_key_hex}).encode("utf-8")
    req = Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=10) as resp:
            return bool(resp.status < 300)
    except HTTPError as e:
        if e.code == 409:
            return True  # Already registered — idempotent.
        print(f"  Warning: could not register public key (HTTP {e.code}).")
        return False
    except (URLError, TimeoutError, OSError) as e:
        print(f"  Warning: could not register public key ({e}).")
        return False


def verify_connection(
    *,
    base_url: str,
    api_key: str,
) -> bool:
    """Ping the control plane health endpoint to verify the round-trip."""
    url = f"{base_url.rstrip('/')}/health"
    req = Request(url, headers={"X-API-Key": api_key}, method="GET")
    try:
        with urlopen(req, timeout=5) as resp:
            return bool(resp.status == 200)
    except Exception:
        return False


def write_env_file(
    *,
    api_key: Optional[str],
    agent_id: str,
    agent_key_b64: str,
    base_url: str = DEFAULT_BASE_URL,
    path: Path = Path(".env"),
) -> Path:
    """Write or append Checkrd variables to a ``.env`` file.

    Existing non-Checkrd lines are preserved. Existing Checkrd lines are
    replaced with the new values.
    """
    existing_lines: list[str] = []
    checkrd_keys: set[str] = set()

    if path.exists():
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("CHECKRD_") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                checkrd_keys.add(key)
            else:
                existing_lines.append(line)

    new_lines = list(existing_lines)
    if new_lines and new_lines[-1].strip():
        new_lines.append("")  # blank separator

    new_lines.append("# Checkrd SDK configuration (generated by `checkrd init`)")
    if api_key:
        new_lines.append(f"CHECKRD_API_KEY={api_key}")
    new_lines.append(f"CHECKRD_AGENT_ID={agent_id}")
    new_lines.append(f"CHECKRD_AGENT_KEY={agent_key_b64}")
    if base_url != DEFAULT_BASE_URL:
        new_lines.append(f"CHECKRD_BASE_URL={base_url}")
    new_lines.append("")

    path.write_text("\n".join(new_lines))
    # Restrict permissions to owner-only (0o600) since the file contains
    # the API key and the Ed25519 private key. Matches the permission model
    # used for identity key files in identity.py.
    try:
        import os
        os.chmod(str(path), 0o600)
    except OSError:
        pass  # Windows or read-only filesystem; write succeeded, chmod optional
    return path


def print_code_snippet(
    *,
    agent_id: str,
    has_api_key: bool,
) -> None:
    """Print a ready-to-paste Python code snippet."""
    print()
    print(_bold("Ready to go! Add this to your code:"))
    print()
    print(_dim("  # Load .env (or export the vars in your shell)"))
    if has_api_key:
        print("  import checkrd")
        print("  checkrd.init()")
        print("  checkrd.instrument()  # patches openai, anthropic")
        print()
        print("  from openai import OpenAI")
        print("  client = OpenAI()")
        print("  # Every API call now goes through Checkrd")
    else:
        print("  import checkrd")
        print(f'  checkrd.init(agent_id="{agent_id}")')
        print("  checkrd.instrument()")
        print()
        print(
            _dim(
                "  # Set CHECKRD_API_KEY to connect to the control plane"
            )
        )
    print()


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------


def run_wizard(
    *,
    api_key: Optional[str] = None,
    agent_id: Optional[str] = None,
    base_url: str = DEFAULT_BASE_URL,
    env_file: str = ".env",
    non_interactive: bool = False,
) -> int:
    """Run the full ``checkrd init`` wizard. Returns exit code (0=success)."""
    interactive = not non_interactive and sys.stdin.isatty()

    print()
    print(_bold("checkrd init") + " — set up Checkrd for this project")
    print()

    # Step 0: Check for existing config.
    existing = detect_existing_config()
    if existing and interactive:
        print("Found existing Checkrd configuration:")
        for k, v in existing.items():
            display = v[:20] + "..." if len(v) > 20 else v
            print(f"  {k}={display}")
        answer = _prompt("Replace it?", default="y")
        if answer.lower() not in ("y", "yes"):
            print("Keeping existing configuration.")
            return 0

    # Step 1: API key.
    resolved_key = resolve_api_key(
        explicit_key=api_key,
        base_url=base_url,
        interactive=interactive,
    )

    # Step 2: Agent ID.
    resolved_agent_id = resolve_agent_id(
        explicit_id=agent_id,
        interactive=interactive,
    )

    # Step 3: Generate keypair.
    if interactive:
        print()
        print(_bold("Step 3: Generating Ed25519 keypair..."))
    private_b64, public_hex = generate_keypair()
    fingerprint = public_hex[:16]
    if interactive:
        print(f"  Fingerprint: {fingerprint}")

    # Step 4: Register with control plane (if we have an API key).
    if resolved_key:
        if interactive:
            print()
            print(_bold("Step 4: Registering with control plane..."))

        agent_uuid = register_agent(
            base_url=base_url,
            api_key=resolved_key,
            agent_id=resolved_agent_id,
        )
        if agent_uuid:
            key_ok = register_public_key(
                base_url=base_url,
                api_key=resolved_key,
                agent_id=agent_uuid,
                public_key_hex=public_hex,
            )
            if key_ok and interactive:
                print(f"  Agent registered: {resolved_agent_id}")
                print(f"  Public key: {fingerprint}")

    # Step 5: Write .env.
    env_path = write_env_file(
        api_key=resolved_key,
        agent_id=resolved_agent_id,
        agent_key_b64=private_b64,
        base_url=base_url,
        path=Path(env_file),
    )
    if interactive:
        print()
        print(f"  Wrote {env_path}")

    # Step 6: Verify round-trip.
    if resolved_key:
        ok = verify_connection(base_url=base_url, api_key=resolved_key)
        if ok and interactive:
            print(f"  {_green('Connected to control plane.')}")
        elif not ok and interactive:
            print("  Warning: could not verify connection. Check your network.")

    # Step 7: Code snippet.
    print_code_snippet(
        agent_id=resolved_agent_id,
        has_api_key=bool(resolved_key),
    )

    return 0
