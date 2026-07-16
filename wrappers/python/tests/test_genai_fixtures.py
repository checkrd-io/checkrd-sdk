"""Golden-fixture parity tests for body-derived GenAI extraction.

The fixtures in ``schemas/genai-fixtures/*.json`` are the
language-neutral contract: the Python extractor (this module) and the
JS extractor (``wrappers/javascript``) both test against the *same*
files, so a field that drifts between runtimes fails here in at least
one of them. Each fixture case pins the exact attribute dict the
extractor must emit — same keys, same values, no extras.

We also assert the inclusion-rule invariant on every fixture case that
emits detail counters, so a fixture that violated billing-critical
normalization (cache > input, reasoning > output) would fail loudly
rather than silently feed bad numbers to ``settle_usage``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import pytest
from hypothesis import given, settings, strategies as st

from checkrd._genai_body import extract_request_attrs, extract_response_attrs


# ---------------------------------------------------------------------------
# Fixture discovery
# ---------------------------------------------------------------------------


def _find_fixtures_dir() -> Path:
    """Locate ``<repo>/schemas/genai-fixtures`` regardless of CWD.

    Tests run from ``wrappers/python``; the fixtures live at the repo
    root. Walk up from this file until ``schemas/genai-fixtures`` is
    found so the test is robust to where pytest is invoked.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "schemas" / "genai-fixtures"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        "Could not locate schemas/genai-fixtures from " + str(here)
    )


_FIXTURES_DIR = _find_fixtures_dir()


def _load_cases() -> List[Tuple[str, Dict[str, Any]]]:
    """Flatten every ``<provider>.json`` into ``(id, case)`` pairs.

    The id is ``<file-stem>::<case-name>`` so a failure names the exact
    fixture case.
    """
    cases: List[Tuple[str, Dict[str, Any]]] = []
    for path in sorted(_FIXTURES_DIR.glob("*.json")):
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        assert isinstance(data, list), f"{path.name} must be a JSON array of cases"
        for case in data:
            assert isinstance(case, dict)
            name = case.get("name", "<unnamed>")
            cases.append((f"{path.stem}::{name}", case))
    return cases


_CASES = _load_cases()


def _encode_body(body: Any) -> Optional[bytes]:
    """Encode a fixture body the way the extractor receives it.

    Fixtures store the raw provider JSON as a parsed object; the
    extractor consumes UTF-8 JSON *bytes*. ``None`` (absent body)
    passes through unchanged.
    """
    if body is None:
        return None
    return json.dumps(body).encode("utf-8")


def _headers(case_io: Mapping[str, Any]) -> Optional[Mapping[str, str]]:
    headers = case_io.get("headers")
    if headers is None:
        return None
    assert isinstance(headers, dict)
    return headers


# ---------------------------------------------------------------------------
# Sanity: fixtures exist and were discovered
# ---------------------------------------------------------------------------


def test_fixture_dir_discovered() -> None:
    assert _FIXTURES_DIR.is_dir()
    # The six provider files this stream targets must all be present.
    stems = {p.stem for p in _FIXTURES_DIR.glob("*.json")}
    assert {"openai", "anthropic", "gemini", "cohere", "bedrock"} <= stems


def test_some_cases_loaded() -> None:
    assert len(_CASES) >= 20, "expected the full fixture corpus to load"


# ---------------------------------------------------------------------------
# The contract: extractor output == expected_*_attrs, exactly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case_id,case", _CASES, ids=[c[0] for c in _CASES])
def test_fixture_case(case_id: str, case: Dict[str, Any]) -> None:
    provider = case["provider"]
    assert isinstance(provider, str)

    # Request side (when the case exercises a request body).
    if "expected_request_attrs" in case:
        req = case.get("request", {})
        body = _encode_body(req.get("body"))
        got = extract_request_attrs(provider, body)
        assert got == case["expected_request_attrs"], (
            f"{case_id}: request attrs mismatch"
        )

    # Response side (when the case exercises a response body/headers).
    if "expected_response_attrs" in case:
        resp = case.get("response", {})
        body = _encode_body(resp.get("body"))
        headers = _headers(resp)
        got = extract_response_attrs(provider, body, headers)
        assert got == case["expected_response_attrs"], (
            f"{case_id}: response attrs mismatch"
        )

    # Every case must assert at least one side, else it's dead weight.
    assert (
        "expected_request_attrs" in case or "expected_response_attrs" in case
    ), f"{case_id}: case asserts neither request nor response attrs"


# ---------------------------------------------------------------------------
# Inclusion-rule invariant holds on the emitted fixture output
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case_id,case", _CASES, ids=[c[0] for c in _CASES])
def test_fixture_emits_inclusion_safe_attrs(
    case_id: str, case: Dict[str, Any]
) -> None:
    """The detail counters the extractor emits must be subsets of the
    totals (the billing-critical invariant ``settle_usage`` relies on).
    Asserted against the *extractor's* live output, not just the
    fixture's hand-written expectation."""
    if "expected_response_attrs" not in case:
        return
    provider = case["provider"]
    resp = case.get("response", {})
    body = _encode_body(resp.get("body"))
    headers = _headers(resp)
    attrs = extract_response_attrs(provider, body, headers)

    input_total = attrs.get("gen_ai.usage.input_tokens")
    cache_read = attrs.get("gen_ai.usage.cache_read.input_tokens")
    cache_creation = attrs.get("gen_ai.usage.cache_creation.input_tokens")
    output_total = attrs.get("gen_ai.usage.output_tokens")
    reasoning = attrs.get("gen_ai.usage.reasoning.output_tokens")

    cache_sum = (cache_read or 0) + (cache_creation or 0)
    if cache_read is not None or cache_creation is not None:
        assert input_total is not None, f"{case_id}: cache emitted without input total"
        assert cache_sum <= input_total, (
            f"{case_id}: cache_read+cache_creation ({cache_sum}) "
            f"> input_tokens ({input_total})"
        )
    if reasoning is not None:
        assert output_total is not None, (
            f"{case_id}: reasoning emitted without output total"
        )
        assert reasoning <= output_total, (
            f"{case_id}: reasoning ({reasoning}) > output_tokens ({output_total})"
        )


# ===========================================================================
# Hypothesis property tests — the bar for this stream
# ===========================================================================
#
# Three properties, asserted against generated input rather than the fixed
# fixture corpus:
#   (a) the extractors NEVER raise on arbitrary bytes / JSON / headers;
#   (b) the inclusion-rule invariant holds for any non-negative usage shape;
#   (c) Anthropic's input normalization equals raw + cache_read + cache_creation.
#
# Scope mirrors tests/test_ffi_properties.py: bounded examples, fast on a
# laptop, defending the wrapper's marshalling/normalization layer.

_PROVIDERS = [
    "openai",
    "azure.openai",
    "anthropic",
    "google.gemini",
    "google.vertex_ai",
    "cohere",
    "aws.bedrock",
    None,
    "perplexity",  # unknown provider — must still never raise
]

# Non-negative token counts within a realistic-but-bounded range.
_count_st = st.integers(min_value=0, max_value=10_000_000)

# Arbitrary JSON values (bounded depth) to stuff into bodies / usage blocks.
_json_value_st = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(10**9), max_value=10**9),
        st.floats(allow_nan=False, allow_infinity=False, width=32),
        st.text(max_size=40),
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(max_size=12), children, max_size=4),
    ),
    max_leaves=20,
)

# Header mappings with arbitrary string keys/values, plus the two Bedrock
# keys in random casing so the case-insensitive path is exercised.
_bedrock_header_key_st = st.sampled_from(
    [
        "x-amzn-bedrock-input-token-count",
        "X-Amzn-Bedrock-Input-Token-Count",
        "x-amzn-bedrock-output-token-count",
        "X-AMZN-BEDROCK-OUTPUT-TOKEN-COUNT",
    ]
)
_header_value_st = st.one_of(
    st.text(max_size=20),
    _count_st.map(str),
    st.sampled_from(["n/a", "", " 42 ", "-1", "3.14", "0x10"]),
)
_headers_st = st.one_of(
    st.none(),
    st.dictionaries(
        st.one_of(_bedrock_header_key_st, st.text(max_size=24)),
        _header_value_st,
        max_size=8,
    ),
)


def _emitted_int(attrs: Dict[str, Any], key: str) -> Optional[int]:
    """Read an emitted attr as int, or None if absent. Asserts the type
    contract: token attrs are always plain ints when present."""
    if key not in attrs:
        return None
    value = attrs[key]
    assert isinstance(value, int) and not isinstance(value, bool)
    return value


def _assert_inclusion_safe(attrs: Dict[str, Any]) -> None:
    """The billing-critical invariant on whatever the extractor emitted."""
    input_total = _emitted_int(attrs, "gen_ai.usage.input_tokens")
    cache_read = _emitted_int(attrs, "gen_ai.usage.cache_read.input_tokens")
    cache_creation = _emitted_int(attrs, "gen_ai.usage.cache_creation.input_tokens")
    output_total = _emitted_int(attrs, "gen_ai.usage.output_tokens")
    reasoning = _emitted_int(attrs, "gen_ai.usage.reasoning.output_tokens")

    if cache_read is not None or cache_creation is not None:
        assert input_total is not None
        assert (cache_read or 0) + (cache_creation or 0) <= input_total
    if reasoning is not None:
        assert output_total is not None
        assert reasoning <= output_total


# --- (a) never raises on arbitrary bytes -----------------------------------


@given(
    provider=st.sampled_from(_PROVIDERS),
    body=st.binary(max_size=2048),
    headers=_headers_st,
)
@settings(max_examples=300, deadline=None)
def test_never_raises_on_arbitrary_bytes(
    provider: Optional[str],
    body: bytes,
    headers: Optional[Mapping[str, str]],
) -> None:
    """Arbitrary (possibly non-UTF-8, non-JSON) bytes must yield a dict,
    never an exception — the telemetry path must not crash on hostile
    response bodies."""
    req = extract_request_attrs(provider, body)
    resp = extract_response_attrs(provider, body, headers)
    assert isinstance(req, dict)
    assert isinstance(resp, dict)
    # Even on garbage, anything emitted must respect the invariant.
    _assert_inclusion_safe(resp)


# --- (a) never raises on arbitrary JSON dicts ------------------------------


@given(
    provider=st.sampled_from(_PROVIDERS),
    payload=st.dictionaries(st.text(max_size=16), _json_value_st, max_size=8),
    headers=_headers_st,
)
@settings(max_examples=300, deadline=None)
def test_never_raises_on_arbitrary_json_dict(
    provider: Optional[str],
    payload: Dict[str, Any],
    headers: Optional[Mapping[str, str]],
) -> None:
    """Any well-formed JSON object body must extract cleanly to a dict."""
    body = json.dumps(payload).encode("utf-8")
    req = extract_request_attrs(provider, body)
    resp = extract_response_attrs(provider, body, headers)
    assert isinstance(req, dict)
    assert isinstance(resp, dict)
    _assert_inclusion_safe(resp)


# --- (a) never raises on arbitrary header mappings -------------------------


@given(
    provider=st.sampled_from(_PROVIDERS),
    headers=st.dictionaries(st.text(max_size=24), st.text(max_size=24), max_size=10),
)
@settings(max_examples=200, deadline=None)
def test_never_raises_on_arbitrary_headers(
    provider: Optional[str], headers: Mapping[str, str]
) -> None:
    """Header-only extraction (Bedrock path) must never raise, and what
    it emits must respect the invariant."""
    resp = extract_response_attrs(provider, None, headers)
    assert isinstance(resp, dict)
    _assert_inclusion_safe(resp)


# --- (b) inclusion-rule invariant, per-provider usage shapes ---------------


@given(prompt=_count_st, completion=_count_st, cached=_count_st, reasoning=_count_st)
@settings(max_examples=200, deadline=None)
def test_openai_inclusion_invariant(
    prompt: int, completion: int, cached: int, reasoning: int
) -> None:
    """OpenAI counts are already inclusive — emitted detail counters must
    be subsets of the totals (we only assert when cached/reasoning are
    themselves <= the totals, which is what real OpenAI usage reports)."""
    body = json.dumps(
        {
            "model": "gpt-4o",
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "prompt_tokens_details": {"cached_tokens": min(cached, prompt)},
                "completion_tokens_details": {
                    "reasoning_tokens": min(reasoning, completion)
                },
            },
        }
    ).encode("utf-8")
    _assert_inclusion_safe(extract_response_attrs("openai", body))


@given(prompt=_count_st, candidates=_count_st, cached=_count_st, thoughts=_count_st)
@settings(max_examples=200, deadline=None)
def test_gemini_inclusion_invariant(
    prompt: int, candidates: int, cached: int, thoughts: int
) -> None:
    """Gemini candidatesTokenCount is inclusive of thoughts; cached is a
    subset of prompt. With realistic subset inputs the invariant holds."""
    body = json.dumps(
        {
            "modelVersion": "gemini-2.5-pro",
            "usageMetadata": {
                "promptTokenCount": prompt,
                "candidatesTokenCount": candidates,
                "cachedContentTokenCount": min(cached, prompt),
                "thoughtsTokenCount": min(thoughts, candidates),
            },
        }
    ).encode("utf-8")
    _assert_inclusion_safe(extract_response_attrs("google.gemini", body))


@given(raw_input=_count_st, cache_read=_count_st, cache_creation=_count_st, output=_count_st)
@settings(max_examples=300, deadline=None)
def test_anthropic_inclusion_invariant(
    raw_input: int, cache_read: int, cache_creation: int, output: int
) -> None:
    """Anthropic normalization makes cache a subset of input by
    construction, for ANY non-negative inputs (no subset precondition
    needed — that's the whole point of the normalization)."""
    body = json.dumps(
        {
            "model": "claude-sonnet-4-5",
            "usage": {
                "input_tokens": raw_input,
                "output_tokens": output,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
            },
        }
    ).encode("utf-8")
    _assert_inclusion_safe(extract_response_attrs("anthropic", body))


# --- (c) Anthropic normalization equality ----------------------------------


@given(raw_input=_count_st, cache_read=_count_st, cache_creation=_count_st)
@settings(max_examples=300, deadline=None)
def test_anthropic_normalization_sums_cache_into_input(
    raw_input: int, cache_read: int, cache_creation: int
) -> None:
    """For any non-negative ints, the emitted input_tokens equals
    raw_input + cache_read + cache_creation, and the cache counters are
    echoed verbatim. This is the exact identity the core nets back out."""
    body = json.dumps(
        {
            "model": "claude-sonnet-4-5",
            "usage": {
                "input_tokens": raw_input,
                "output_tokens": 7,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
            },
        }
    ).encode("utf-8")
    attrs = extract_response_attrs("anthropic", body)
    assert attrs["gen_ai.usage.input_tokens"] == raw_input + cache_read + cache_creation
    assert attrs["gen_ai.usage.cache_read.input_tokens"] == cache_read
    assert attrs["gen_ai.usage.cache_creation.input_tokens"] == cache_creation
    assert attrs["gen_ai.usage.output_tokens"] == 7

