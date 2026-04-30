"""Property-based tests for the Python <-> WASM FFI boundary.

Hypothesis-generated inputs exercise invariants that unit tests can't
cover exhaustively:

- UTF-8 round-tripping through WASM linear memory (including multi-byte
  sequences and emoji) — catches encoding bugs at the ``_write_to_wasm`` /
  ``_read_from_wasm`` seam.
- ``evaluate()`` robustness: any well-formed request produces a valid
  ``EvalResult``. Catches regex panics, header-casing bugs, and pointer
  lifecycle issues that trip only on specific input shapes.
- Policy invariants: allow-all always allows, deny-all always denies.
  Anchors the semantics of the engine against drift from core changes.
- Keypair derivation round-trip: ``generate_keypair`` → split →
  ``derive_public_key`` must equal the stored public half.
- Malformed policy JSON raises :class:`CheckrdInitError` — never crashes
  the interpreter with a WASM trap.

Scope is focused, not exhaustive: ~100 examples per property, total
runtime <10s on a laptop. The Rust core has its own Wycheproof + mutation
coverage; these tests defend the *wrapper's* marshalling layer.
"""
from __future__ import annotations

import json

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from checkrd.engine import WasmEngine
from checkrd.exceptions import CheckrdInitError
from tests.conftest import ALLOW_ALL_POLICY, requires_wasm

_TS = "2026-03-28T14:30:00Z"
_TS_MS = 1774708200000

pytestmark = requires_wasm

# ---------------------------------------------------------------------------
# Input strategies
# ---------------------------------------------------------------------------

# RFC 7230 methods; the matcher is case-insensitive but we stick to the
# canonical uppercase spelling that real clients send.
_method_st = st.sampled_from(["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])

# URL strategy: canonical https URLs with a bounded path. Avoids percent-
# encoded edge cases that belong in a separate URL-parser test.
_url_st = st.builds(
    lambda host, path: f"https://{host}/{path}",
    host=st.sampled_from(
        ["api.stripe.com", "api.openai.com", "api.anthropic.com", "example.com"]
    ),
    path=st.text(
        alphabet="abcdefghijklmnopqrstuvwxyz0123456789/-_", min_size=0, max_size=50
    ),
)

# RFC 9110 header values: visible ASCII + space + tab. Keeping it narrow
# avoids entangling this suite with header normalization tests.
_header_name_st = st.sampled_from(
    ["Authorization", "Content-Type", "X-Request-Id", "User-Agent", "Accept"]
)
_header_value_st = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ._-/:;=,",
    min_size=0,
    max_size=200,
)
_headers_st = st.lists(
    st.tuples(_header_name_st, _header_value_st), min_size=0, max_size=8
)

# Any valid Python string — UTF-8 only, no unpaired surrogates (those can't
# cross the FFI boundary because Python rejects them at the encode step).
_any_utf8_st = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    min_size=0,
    max_size=500,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def allow_all_engine() -> WasmEngine:
    return WasmEngine(
        json.dumps(ALLOW_ALL_POLICY),
        "test-agent",
        private_key_bytes=b"",
        instance_id="",
    )


@pytest.fixture()
def deny_all_engine() -> WasmEngine:
    policy = {"agent": "test-agent", "default": "deny", "rules": []}
    return WasmEngine(
        json.dumps(policy), "test-agent", private_key_bytes=b"", instance_id=""
    )


# Hypothesis reuses the function-scoped engine fixture across all examples
# of a @given test. Safe because evaluate() is idempotent under a static
# policy — we suppress the health-check warning to document that intent.
_FIXTURE_SETTINGS = settings(
    max_examples=100,
    deadline=None,  # WASM eval variance on CI runners
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


# ---------------------------------------------------------------------------
# UTF-8 round-trip through FFI
# ---------------------------------------------------------------------------


@given(payload=_any_utf8_st)
@_FIXTURE_SETTINGS
def test_utf8_round_trip_via_request_id(
    allow_all_engine: WasmEngine, payload: str
) -> None:
    """Any UTF-8 string survives Python → WASM → Python unchanged.

    The ``request_id`` field is the cleanest probe for this: the WASM core
    echoes it verbatim in the result, so bitwise equality proves both
    encoding legs are lossless.
    """
    result = allow_all_engine.evaluate(
        request_id=payload,
        method="GET",
        url="https://example.com/",
        headers=[],
        body=None,
        timestamp=_TS,
        timestamp_ms=_TS_MS,
    )
    assert result.request_id == payload


# ---------------------------------------------------------------------------
# evaluate() robustness
# ---------------------------------------------------------------------------


@given(
    method=_method_st,
    url=_url_st,
    headers=_headers_st,
    request_id=st.text(
        alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_", min_size=1, max_size=50
    ),
)
@_FIXTURE_SETTINGS
def test_evaluate_produces_valid_result_for_any_request(
    allow_all_engine: WasmEngine,
    method: str,
    url: str,
    headers: list[tuple[str, str]],
    request_id: str,
) -> None:
    """Every well-formed request yields a structurally valid EvalResult.

    Verifies the output contract: ``allowed`` is a bool, ``telemetry_json``
    parses, and ``request_id`` echoes the input. Regressions would surface
    here rather than deep inside a transport or batcher test.
    """
    result = allow_all_engine.evaluate(
        request_id=request_id,
        method=method,
        url=url,
        headers=headers,
        body=None,
        timestamp=_TS,
        timestamp_ms=_TS_MS,
    )
    assert result.allowed is True
    assert result.deny_reason is None
    assert result.request_id == request_id
    assert isinstance(result.telemetry_json, str)
    json.loads(result.telemetry_json)  # must be valid JSON


# ---------------------------------------------------------------------------
# Policy semantic invariants
# ---------------------------------------------------------------------------


@given(method=_method_st, url=_url_st)
@_FIXTURE_SETTINGS
def test_allow_all_policy_allows_every_request(
    allow_all_engine: WasmEngine, method: str, url: str
) -> None:
    """default=allow with no rules is unconditional: every request allowed."""
    result = allow_all_engine.evaluate(
        request_id="req",
        method=method,
        url=url,
        headers=[],
        body=None,
        timestamp=_TS,
        timestamp_ms=_TS_MS,
    )
    assert result.allowed is True
    assert result.deny_reason is None


@given(method=_method_st, url=_url_st)
@_FIXTURE_SETTINGS
def test_deny_all_policy_denies_every_request(
    deny_all_engine: WasmEngine, method: str, url: str
) -> None:
    """default=deny with no rules is unconditional: every request denied."""
    result = deny_all_engine.evaluate(
        request_id="req",
        method=method,
        url=url,
        headers=[],
        body=None,
        timestamp=_TS,
        timestamp_ms=_TS_MS,
    )
    assert result.allowed is False
    assert result.deny_reason is not None


# ---------------------------------------------------------------------------
# Keypair generation round-trip
# ---------------------------------------------------------------------------


@given(st.integers(min_value=0, max_value=20))
@settings(max_examples=10, deadline=None)
def test_generate_keypair_derive_public_matches(_iter: int) -> None:
    """derive_public_key(private) == the public half returned by generate_keypair.

    Guards against RNG-side bugs and FFI byte-ordering issues. Running
    multiple iterations catches cases where a specific random byte value
    would cause divergence (past FFI bugs have been byte-value sensitive).
    """
    private, public = WasmEngine.generate_keypair()
    assert len(private) == 32, "private key must be 32 bytes"
    assert len(public) == 32, "public key must be 32 bytes"
    derived = WasmEngine.derive_public_key(private)
    assert derived == public


# ---------------------------------------------------------------------------
# Error path: malformed input → clean exception, not a WASM trap
# ---------------------------------------------------------------------------


@given(
    malformed=st.one_of(
        st.text(min_size=0, max_size=200),  # random strings
        st.just(""),
        st.just("null"),
        st.just("[]"),
        st.just("{"),  # truncated JSON
        st.just('{"default": "not_a_mode"}'),
        st.just('{"default": "allow", "rules": [null]}'),
        st.just('{"default": "allow", "rules": [{"name": null}]}'),
    )
)
@settings(max_examples=50, deadline=None)
def test_malformed_policy_raises_init_error(malformed: str) -> None:
    """Any malformed policy JSON raises CheckrdInitError, never crashes.

    Operator safety: a typo in a policy file must surface as a readable
    Python exception, not a WASM trap that leaves the host process in an
    undefined state.
    """
    with pytest.raises(CheckrdInitError):
        WasmEngine(
            malformed, "test-agent", private_key_bytes=b"", instance_id=""
        )
