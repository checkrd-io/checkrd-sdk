"""WASM multi-instance isolation tests.

Verifies that each WasmEngine instance has fully independent state —
rate limiters, kill switches, policies, and identity are
NOT shared across instances. This is the industry-standard verification
for embeddable WASM engines (equivalent to V8 Isolate tests, Lua
luaL_newstate tests, and OPA instance pool tests).

Each test creates two WasmEngine instances in the same thread and verifies
that mutations to one do not affect the other. These tests run through the
actual WASM boundary, not native Rust, so they exercise the production
isolation guarantee provided by wasmtime's per-Store linear memory.

Reference: https://docs.wasmtime.dev/api/wasmtime/struct.Store.html
"Store is a unit of isolation where WebAssembly objects are always entirely
 contained within a Store, and nothing can cross between stores."
"""

from __future__ import annotations

import json

from checkrd.engine import WasmEngine
from tests.conftest import requires_wasm

_TS = "2026-03-28T14:30:00Z"
_TS_MS = 1774708200000

# --- Policies ---

ALLOW_ALL = json.dumps({"agent": "agent-allow", "default": "allow", "rules": []})

DENY_ALL = json.dumps({"agent": "agent-deny", "default": "deny", "rules": []})

RATE_LIMITED = json.dumps(
    {
        "agent": "agent-rl",
        "default": "allow",
        "rules": [
            {
                "name": "limit-5",
                "limit": {"calls_per_minute": 5, "per": "global"},
            }
        ],
    }
)

def _eval(
    engine: WasmEngine,
    method: str = "GET",
    url: str = "https://api.stripe.com/v1/charges",
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


@requires_wasm
class TestMultiInstanceIsolation:
    """Verify that two WasmEngine instances have fully independent state."""

    def test_policy_isolation(self) -> None:
        """Engine A (allow-all) and Engine B (deny-all) evaluate independently."""
        engine_allow = WasmEngine(ALLOW_ALL, "agent-allow")
        engine_deny = WasmEngine(DENY_ALL, "agent-deny")

        # Each should follow its own policy
        assert _eval(engine_allow).allowed
        assert not _eval(engine_deny).allowed

        # Crucially: creating engine_deny did NOT overwrite engine_allow's policy
        assert _eval(engine_allow).allowed, (
            "engine_allow should still allow after engine_deny was created"
        )

    def test_rate_limiter_isolation(self) -> None:
        """Exhausting one engine's rate limit does not affect the other."""
        engine_a = WasmEngine(RATE_LIMITED, "agent-a")
        engine_b = WasmEngine(RATE_LIMITED, "agent-b")

        # Exhaust engine A's 5/min limit
        for i in range(5):
            result = _eval(engine_a)
            assert result.allowed, f"request {i} to engine A should be allowed"
        result_a6 = _eval(engine_a)
        assert not result_a6.allowed, "engine A should be rate limited after 5 requests"

        # Engine B should still have its full quota
        result_b1 = _eval(engine_b)
        assert result_b1.allowed, (
            "engine B should NOT be affected by engine A's rate limit"
        )

    def test_kill_switch_isolation(self) -> None:
        """Activating kill switch on one engine does not affect the other."""
        engine_a = WasmEngine(ALLOW_ALL, "agent-a")
        engine_b = WasmEngine(ALLOW_ALL, "agent-b")

        # Activate kill switch on engine A only
        engine_a.set_kill_switch(True)

        assert not _eval(engine_a).allowed, "engine A should be killed"
        assert _eval(engine_b).allowed, (
            "engine B should NOT be affected by engine A's kill switch"
        )

    def test_identity_isolation(self) -> None:
        """Each engine uses its own signing key."""
        # Generate two distinct keypairs via WASM
        private_a, _public_a = WasmEngine.generate_keypair()
        private_b, _public_b = WasmEngine.generate_keypair()

        engine_a = WasmEngine(ALLOW_ALL, "agent-a", private_a)
        engine_b = WasmEngine(ALLOW_ALL, "agent-b", private_b)

        payload = b"test payload for signing"
        sig_a = engine_a.sign(payload)
        sig_b = engine_b.sign(payload)

        assert sig_a != sig_b, "different keys should produce different signatures"
        assert len(sig_a) == 64, "Ed25519 signature should be 64 bytes"
        assert len(sig_b) == 64

    def test_reinit_does_not_affect_other(self) -> None:
        """Re-initializing one engine's policy leaves the other unchanged."""
        engine_a = WasmEngine(ALLOW_ALL, "agent-a")
        engine_b = WasmEngine(DENY_ALL, "agent-b")

        assert _eval(engine_a).allowed
        assert not _eval(engine_b).allowed

        # Reload engine A with deny-all policy
        engine_a.reload_policy(DENY_ALL)

        # Engine A should now deny
        assert not _eval(engine_a).allowed, "engine A should deny after reload"
        # Engine B should STILL deny (was deny-all from the start, unchanged)
        assert not _eval(engine_b).allowed, "engine B should be unchanged"

    def test_interleaved_rate_limiting(self) -> None:
        """Interleaving requests across engines tracks limits independently."""
        engine_a = WasmEngine(RATE_LIMITED, "agent-a")
        engine_b = WasmEngine(RATE_LIMITED, "agent-b")

        # Alternate: A, B, A, B, A, B, A, B, A, B (5 each)
        for i in range(5):
            assert _eval(engine_a).allowed, f"A request {i} should be allowed"
            assert _eval(engine_b).allowed, f"B request {i} should be allowed"

        # Both should now be at their limit (5/5)
        assert not _eval(engine_a).allowed, "A should be rate limited"
        assert not _eval(engine_b).allowed, "B should be rate limited"
