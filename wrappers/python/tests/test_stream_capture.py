"""Streaming terminal-frame usage tap — golden-fixture + property tests.

The fixtures in ``schemas/genai-fixtures/streaming/*.json`` are the
shared parity contract: the JS tap (``_stream_capture.ts``) tests against
the identical cases, so a field that drifts between runtimes fails here or
there. See ``schemas/genai-fixtures/streaming/README.md``.

The property tests are the mutation-resistance bar: the inclusion-rule
invariant, the Anthropic normalization identity, the no-estimate-on-
abandonment guarantee, and never-throws are asserted against generated
input rather than only the fixed cases.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
from hypothesis import given, settings, strategies as st

from checkrd._stream_capture import capture_stream_usage

# ---------------------------------------------------------------------------
# Fixture loading (mirrors tests/test_genai_fixtures.py)
# ---------------------------------------------------------------------------


def _find_streaming_fixtures_dir() -> Path:
    """Locate ``<repo>/schemas/genai-fixtures/streaming`` regardless of CWD."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "schemas" / "genai-fixtures" / "streaming"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        "Could not locate schemas/genai-fixtures/streaming from " + str(here)
    )


_FIXTURES_DIR = _find_streaming_fixtures_dir()


def _load_cases() -> List[Tuple[str, Dict[str, Any]]]:
    cases: List[Tuple[str, Dict[str, Any]]] = []
    for path in sorted(_FIXTURES_DIR.glob("*.json")):
        for case in json.loads(path.read_text(encoding="utf-8")):
            cases.append((f"{path.stem}::{case['name']}", case))
    return cases


_CASES = _load_cases()


def test_fixtures_were_discovered() -> None:
    # Guard against a silently-empty parametrization (the parity contract
    # is worthless if the loader finds nothing).
    assert len(_CASES) >= 6, f"expected the streaming fixtures, found {len(_CASES)}"


@pytest.mark.parametrize("case_id,case", _CASES, ids=[c[0] for c in _CASES])
def test_streaming_fixture_case(case_id: str, case: Dict[str, Any]) -> None:
    result = capture_stream_usage(
        case["provider"], case["sse_frames"], case.get("complete", True)
    )
    assert result.usage_attrs == case.get("expected_usage_attrs", {}), case_id
    if "expected_pricing_status" in case:
        assert result.pricing_status == case["expected_pricing_status"], case_id


# ---------------------------------------------------------------------------
# Frame builders for the property tests
# ---------------------------------------------------------------------------

_count_st = st.integers(min_value=0, max_value=10_000_000)


def _openai_usage_frame(usage: Dict[str, Any]) -> str:
    return "data: " + json.dumps({"choices": [], "usage": usage}) + "\n\n"


def _anthropic_start_frame(usage: Dict[str, Any]) -> str:
    payload = {"type": "message_start", "message": {"usage": usage}}
    return "event: message_start\ndata: " + json.dumps(payload) + "\n\n"


def _anthropic_delta_frame(output_tokens: int) -> str:
    payload = {"type": "message_delta", "usage": {"output_tokens": output_tokens}}
    return "event: message_delta\ndata: " + json.dumps(payload) + "\n\n"


def _assert_inclusion_safe(attrs: Dict[str, Any]) -> None:
    inp = attrs.get("gen_ai.usage.input_tokens")
    out = attrs.get("gen_ai.usage.output_tokens")
    cr = attrs.get("gen_ai.usage.cache_read.input_tokens")
    cc = attrs.get("gen_ai.usage.cache_creation.input_tokens")
    rz = attrs.get("gen_ai.usage.reasoning.output_tokens")
    if cr is not None or cc is not None:
        assert inp is not None, "cache emitted without an input total"
        assert (cr or 0) + (cc or 0) <= inp
    if rz is not None:
        assert out is not None, "reasoning emitted without an output total"
        assert rz <= out


# --- (a) never throws -------------------------------------------------------


@given(
    frames=st.lists(st.text(max_size=200), max_size=20),
    provider=st.sampled_from(["openai", "anthropic", "google.gemini", "cohere", "x"]),
    complete=st.booleans(),
)
@settings(max_examples=300, deadline=None)
def test_never_raises_on_arbitrary_frames(
    frames: List[str], provider: str, complete: bool
) -> None:
    # A hostile / truncated stream must never crash the telemetry path.
    result = capture_stream_usage(provider, frames, complete)
    assert isinstance(result.usage_attrs, dict)


# --- (b) inclusion-rule invariant ------------------------------------------


@given(prompt=_count_st, completion=_count_st, cached=_count_st, reasoning=_count_st)
@settings(max_examples=300, deadline=None)
def test_openai_inclusion_invariant(
    prompt: int, completion: int, cached: int, reasoning: int
) -> None:
    # cached <= prompt and reasoning <= completion model the real OpenAI
    # contract (details are subsets of the totals).
    usage = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "prompt_tokens_details": {"cached_tokens": min(cached, prompt)},
        "completion_tokens_details": {"reasoning_tokens": min(reasoning, completion)},
    }
    frames = [_openai_usage_frame(usage), "data: [DONE]\n\n"]
    result = capture_stream_usage("openai", frames, True)
    _assert_inclusion_safe(result.usage_attrs)


@given(raw_input=_count_st, cache_read=_count_st, cache_creation=_count_st, output=_count_st)
@settings(max_examples=300, deadline=None)
def test_anthropic_inclusion_invariant(
    raw_input: int, cache_read: int, cache_creation: int, output: int
) -> None:
    # The normalization makes cache a subset of input by construction, for
    # ANY non-negative inputs — no subset precondition needed.
    frames = [
        _anthropic_start_frame(
            {
                "input_tokens": raw_input,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
            }
        ),
        _anthropic_delta_frame(output),
        "event: message_stop\ndata: {}\n\n",
    ]
    result = capture_stream_usage("anthropic", frames, True)
    _assert_inclusion_safe(result.usage_attrs)


# --- (c) Anthropic normalization equality ----------------------------------


@given(raw_input=_count_st, cache_read=_count_st, cache_creation=_count_st, output=_count_st)
@settings(max_examples=300, deadline=None)
def test_anthropic_normalization_sums_cache_into_input(
    raw_input: int, cache_read: int, cache_creation: int, output: int
) -> None:
    # The exact identity the core nets back out: emitted input ==
    # raw + cache_read + cache_creation, cache counters echoed verbatim.
    frames = [
        _anthropic_start_frame(
            {
                "input_tokens": raw_input,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
            }
        ),
        _anthropic_delta_frame(output),
        "event: message_stop\ndata: {}\n\n",
    ]
    attrs = capture_stream_usage("anthropic", frames, True).usage_attrs
    assert attrs["gen_ai.usage.input_tokens"] == raw_input + cache_read + cache_creation
    assert attrs["gen_ai.usage.cache_read.input_tokens"] == cache_read
    assert attrs["gen_ai.usage.cache_creation.input_tokens"] == cache_creation
    assert attrs["gen_ai.usage.output_tokens"] == output


# --- (d) abandonment never estimates ---------------------------------------


@given(prompt=_count_st, completion=_count_st)
@settings(max_examples=200, deadline=None)
def test_openai_abandoned_stream_is_always_untallied(
    prompt: int, completion: int
) -> None:
    # A stream whose terminal usage frame never arrives must yield an empty
    # usage dict and the untallied status — no partial estimate, ever, even
    # though we carry content deltas through.
    frames = [
        "data: " + json.dumps({"choices": [{"delta": {"content": "x"}}]}) + "\n\n",
        "data: " + json.dumps({"choices": [{"delta": {"content": "y"}}]}) + "\n\n",
    ]
    result = capture_stream_usage("openai", frames, complete=False)
    assert result.usage_attrs == {}
    assert result.pricing_status == "untallied"


@given(raw_input=_count_st)
@settings(max_examples=200, deadline=None)
def test_anthropic_abandoned_after_start_is_untallied(raw_input: int) -> None:
    # message_start arrived (we know the input) but the terminal message_delta
    # never did — the output is never finalized, so the whole event is
    # untallied rather than billed on a partial.
    frames = [_anthropic_start_frame({"input_tokens": raw_input})]
    result = capture_stream_usage("anthropic", frames, complete=False)
    assert result.usage_attrs == {}
    assert result.pricing_status == "untallied"
