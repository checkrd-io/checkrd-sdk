"""Golden-fixture + property tests for OtlpSink's GenAI metrics (M-15).

``OtlpSink`` emits the two OTel GenAI *client* metric instruments alongside its
spans. This module pins that behaviour against the shared golden fixtures under
``schemas/genai-fixtures/metrics/`` — the SAME fixtures the JS SDK is built
against, so both runtimes export byte-identical histogram data points.

The harness mirrors ``test_otlp_sink.py``: instead of a capturing span exporter
it wires an ``InMemoryMetricReader`` to a ``MeterProvider`` that carries the same
two ``ExplicitBucketHistogramAggregation`` Views the production ``OtlpSink``
installs, then records events through the *shipped* recording helper
(``checkrd.sinks._record_genai_metrics``). Testing the module-level helper — not
a re-implementation — is what makes the parity contract meaningful.

Property tests (Hypothesis) defend the invariants a fixed fixture set can't:
recording never raises, ``sum(bucket_counts) == count`` for every data point, and
a value always lands in the bucket an independent computation predicts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

otel_available = True
try:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.metrics.view import (
        ExplicitBucketHistogramAggregation,
        View,
    )
    from opentelemetry.sdk.resources import Resource
except ImportError:
    otel_available = False

pytestmark = pytest.mark.skipif(
    not otel_available,
    reason="opentelemetry not installed (pip install checkrd[otlp])",
)

from checkrd.sinks import (  # noqa: E402  (import after the skip guard)
    DURATION_BOUNDS,
    OPERATION_DURATION_INSTRUMENT,
    OTEL_SCHEMA_URL,
    TOKEN_BOUNDS,
    TOKEN_USAGE_INSTRUMENT,
    _create_genai_instruments,
    _record_genai_metrics,
)

# Fixtures live in the repo-shared schemas tree, four levels up from this file:
# wrappers/python/tests/ -> wrappers/python -> wrappers -> <repo root>.
FIXTURES_DIR = (
    Path(__file__).resolve().parents[3] / "schemas" / "genai-fixtures" / "metrics"
)


# ---------------------------------------------------------------------------
# Harness — an OtlpSink-shaped meter wired to an in-memory reader
# ---------------------------------------------------------------------------


def make_test_meters() -> tuple[Any, Any, Any]:
    """Build the two GenAI instruments against an ``InMemoryMetricReader``.

    Uses the SAME Views (explicit bucket boundaries) the production
    ``OtlpSink.__init__`` registers and the SAME ``_create_genai_instruments``
    helper, so the exported data points are exactly what the sink would produce
    over OTLP — only the exporter is swapped for an in-memory capture.

    Returns ``(reader, token_usage, operation_duration)``.
    """
    reader = InMemoryMetricReader()
    provider = MeterProvider(
        resource=Resource.create(
            {"service.name": "test"}, schema_url=OTEL_SCHEMA_URL
        ),
        metric_readers=[reader],
        views=[
            View(
                instrument_name=TOKEN_USAGE_INSTRUMENT,
                aggregation=ExplicitBucketHistogramAggregation(
                    boundaries=list(TOKEN_BOUNDS)
                ),
            ),
            View(
                instrument_name=OPERATION_DURATION_INSTRUMENT,
                aggregation=ExplicitBucketHistogramAggregation(
                    boundaries=list(DURATION_BOUNDS)
                ),
            ),
        ],
    )
    token_usage, operation_duration = _create_genai_instruments(provider)
    return reader, token_usage, operation_duration


def collect_metrics(reader: Any) -> dict[str, dict[str, Any]]:
    """Export via the in-memory reader and flatten into a comparable shape.

    Returns ``{instrument_name: {"unit", "bounds", "data_points"}}`` where each
    data point is ``{"attributes": {...}, "count", "sum", "bucket_counts": [...]}``
    — the exact shape the fixtures' ``expected`` block uses.
    """
    data = reader.get_metrics_data()
    out: dict[str, dict[str, Any]] = {}
    if data is None:
        return out
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                points = []
                for dp in metric.data.data_points:
                    points.append(
                        {
                            "attributes": dict(dp.attributes),
                            "count": dp.count,
                            "sum": dp.sum,
                            "bucket_counts": list(dp.bucket_counts),
                            "bounds": list(dp.explicit_bounds),
                        }
                    )
                out[metric.name] = {
                    "unit": metric.unit,
                    "data_points": points,
                }
    return out


def _points_by_attrs(data_points: list[dict[str, Any]]) -> dict[frozenset, dict]:
    """Index data points by their (order-independent) attribute set."""
    indexed: dict[frozenset, dict] = {}
    for dp in data_points:
        key = frozenset(dp["attributes"].items())
        assert key not in indexed, f"duplicate series for attributes {dp['attributes']}"
        indexed[key] = dp
    return indexed


# ---------------------------------------------------------------------------
# Golden-fixture test
# ---------------------------------------------------------------------------


def _load_fixtures() -> list[tuple[str, dict[str, Any]]]:
    cases = []
    for path in sorted(FIXTURES_DIR.glob("*.json")):
        with open(path, encoding="utf-8") as f:
            cases.append((path.stem, json.load(f)))
    return cases


FIXTURE_CASES = _load_fixtures()


def test_fixtures_present() -> None:
    """Guard against a silently-empty fixture directory (path drift)."""
    assert FIXTURE_CASES, f"no metric fixtures found under {FIXTURES_DIR}"
    names = {name for name, _ in FIXTURE_CASES}
    # The four cases the README documents. If a case is renamed/removed this
    # fails loudly rather than the parametrized test silently covering fewer.
    assert {
        "basic_single_call",
        "aggregation_same_series",
        "dimensional_split_by_model",
        "partial_missing_output",
    } <= names


@pytest.mark.parametrize("name,fixture", FIXTURE_CASES, ids=[c[0] for c in FIXTURE_CASES])
def test_metric_fixture(name: str, fixture: dict[str, Any]) -> None:
    """Record every event in a fixture and match each exported instrument."""
    reader, token_usage, operation_duration = make_test_meters()

    for event in fixture["events"]:
        _record_genai_metrics(event, token_usage, operation_duration)

    collected = collect_metrics(reader)
    expected = fixture["expected"]

    for instrument_name, expected_metric in expected.items():
        assert instrument_name in collected, (
            f"[{name}] instrument {instrument_name!r} was not exported"
        )
        got = collected[instrument_name]

        # Unit + bounds are part of the contract.
        assert got["unit"] == expected_metric["unit"], (
            f"[{name}] {instrument_name} unit mismatch"
        )

        exp_points = _points_by_attrs(expected_metric["data_points"])
        got_points = _points_by_attrs(got["data_points"])

        assert set(got_points) == set(exp_points), (
            f"[{name}] {instrument_name} series mismatch:\n"
            f"  expected attr-sets: {[dict(k) for k in exp_points]}\n"
            f"  got attr-sets:      {[dict(k) for k in got_points]}"
        )

        for attr_key, exp_dp in exp_points.items():
            got_dp = got_points[attr_key]
            ctx = f"[{name}] {instrument_name} {dict(attr_key)}"
            assert got_dp["count"] == exp_dp["count"], f"{ctx}: count"
            # sum may be float (duration) — compare with tolerance.
            assert got_dp["sum"] == pytest.approx(exp_dp["sum"]), f"{ctx}: sum"
            assert list(got_dp["bounds"]) == list(expected_metric["bounds"]), (
                f"{ctx}: bounds"
            )
            assert list(got_dp["bucket_counts"]) == list(exp_dp["bucket_counts"]), (
                f"{ctx}: bucket_counts"
            )


# ---------------------------------------------------------------------------
# Property tests (Hypothesis)
# ---------------------------------------------------------------------------

from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402


def _expected_bucket_index(value: float, bounds: tuple[float, ...]) -> int:
    """Independently compute the bucket index for ``value``.

    OTel explicit-bucket histograms are left-open / right-closed: ``value`` lands
    in the smallest index ``i`` with ``value <= bounds[i]``, else the overflow
    bucket ``len(bounds)``. Deliberately a from-scratch reimplementation so the
    test does not trust the same arithmetic the SDK uses.
    """
    for i, upper in enumerate(bounds):
        if value <= upper:
            return i
    return len(bounds)


# Arbitrary event dicts: mix known GenAI keys with random junk keys/values.
_scalar = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**9), max_value=10**12),
    st.floats(allow_nan=True, allow_infinity=True),
    st.text(max_size=40),
)
_known_keys = st.sampled_from(
    [
        "gen_ai.provider.name",
        "gen_ai.operation.name",
        "gen_ai.request.model",
        "gen_ai.response.model",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai_system",
        "gen_ai_model",
        "gen_ai_input_tokens",
        "gen_ai_output_tokens",
        "latency_ms",
    ]
)
_arbitrary_event = st.dictionaries(
    keys=st.one_of(_known_keys, st.text(max_size=20)),
    values=_scalar,
    max_size=12,
)


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(event=_arbitrary_event)
def test_recording_never_raises(event: dict[str, Any]) -> None:
    """Recording must not raise on ANY event dict — a metering glitch must
    never break the wrapped host call."""
    reader, token_usage, operation_duration = make_test_meters()
    _record_genai_metrics(event, token_usage, operation_duration)
    # Exporting the (possibly empty) result must also be safe.
    collect_metrics(reader)


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(
    input_tokens=st.integers(min_value=0, max_value=10**11),
    output_tokens=st.integers(min_value=0, max_value=10**11),
    latency_ms=st.floats(min_value=0, max_value=500_000, allow_nan=False),
)
def test_bucket_counts_sum_to_count(
    input_tokens: int, output_tokens: int, latency_ms: float
) -> None:
    """For every exported data point, ``sum(bucket_counts) == count``."""
    reader, token_usage, operation_duration = make_test_meters()
    event = {
        "gen_ai.provider.name": "openai",
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.usage.input_tokens": input_tokens,
        "gen_ai.usage.output_tokens": output_tokens,
        "latency_ms": latency_ms,
    }
    _record_genai_metrics(event, token_usage, operation_duration)

    collected = collect_metrics(reader)
    assert collected, "expected at least one instrument exported"
    for metric in collected.values():
        for dp in metric["data_points"]:
            assert sum(dp["bucket_counts"]) == dp["count"]


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(tokens=st.integers(min_value=0, max_value=10**10))
def test_token_value_lands_in_correct_bucket(tokens: int) -> None:
    """A recorded token count increments exactly the bucket an independent
    computation predicts from ``TOKEN_BOUNDS``."""
    reader, token_usage, operation_duration = make_test_meters()
    _record_genai_metrics(
        {
            "gen_ai.provider.name": "openai",
            "gen_ai.usage.input_tokens": tokens,
        },
        token_usage,
        operation_duration,
    )

    collected = collect_metrics(reader)
    dps = collected[TOKEN_USAGE_INSTRUMENT]["data_points"]
    assert len(dps) == 1
    counts = dps[0]["bucket_counts"]
    expected_index = _expected_bucket_index(tokens, TOKEN_BOUNDS)
    assert sum(counts) == 1
    assert counts[expected_index] == 1, (
        f"token count {tokens} expected in bucket {expected_index} "
        f"(bounds {TOKEN_BOUNDS}); got counts {counts}"
    )


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(latency_ms=st.floats(min_value=0, max_value=500_000, allow_nan=False))
def test_duration_value_lands_in_correct_bucket(latency_ms: float) -> None:
    """``latency_ms/1000`` increments exactly the bucket predicted from
    ``DURATION_BOUNDS``."""
    reader, token_usage, operation_duration = make_test_meters()
    _record_genai_metrics(
        {
            "gen_ai.provider.name": "openai",
            "latency_ms": latency_ms,
        },
        token_usage,
        operation_duration,
    )

    collected = collect_metrics(reader)
    dps = collected[OPERATION_DURATION_INSTRUMENT]["data_points"]
    assert len(dps) == 1
    counts = dps[0]["bucket_counts"]
    expected_index = _expected_bucket_index(latency_ms / 1000.0, DURATION_BOUNDS)
    assert sum(counts) == 1
    assert counts[expected_index] == 1, (
        f"duration {latency_ms / 1000.0}s expected in bucket {expected_index} "
        f"(bounds {DURATION_BOUNDS}); got counts {counts}"
    )
