"""Tests for :class:`checkrd.OTelSpanSink`.

The sink's job is "emit Checkrd telemetry on the caller's OTel tracer".
These tests use OTel's in-memory span exporter so we can inspect the
exact span shapes the sink produces — the user of the sink never sees
us; they see their own OTel pipeline. A regression here silently
changes the shape of data landing in Datadog / Honeycomb / Grafana,
which is exactly the kind of thing operators set up paged alerts
against.

The attribute names asserted here are a **public contract**. If they
change, dashboards and saved queries break. Names follow OpenTelemetry
semantic conventions where those exist (HTTP, GenAI) and use the
``checkrd.*`` namespace for SDK-specific fields.
"""

from __future__ import annotations

from typing import Any

import pytest


otel_api = pytest.importorskip("opentelemetry.trace")
otel_sdk = pytest.importorskip("opentelemetry.sdk.trace")
otel_inmem = pytest.importorskip(
    "opentelemetry.sdk.trace.export.in_memory_span_exporter",
)

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from checkrd import OTelSpanSink


@pytest.fixture
def span_exporter() -> InMemorySpanExporter:
    """Fresh ``InMemorySpanExporter`` + tracer per test.

    OTel's global ``set_tracer_provider`` may only be called once per
    process — subsequent calls emit a warning and silently keep the
    first provider. We therefore do NOT touch the global; we hand the
    sink an explicit tracer from a test-local provider via
    ``OTelSpanSink(tracer=...)``. The fixture yields the exporter so
    tests can inspect the finished spans.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    # Stash the tracer on the exporter for tests to pull out. The
    # InMemorySpanExporter class accepts arbitrary attribute assignment.
    exporter._test_tracer = tracer  # type: ignore[attr-defined]
    yield exporter
    exporter.clear()


def _sink_with(
    exporter: InMemorySpanExporter,
) -> "OTelSpanSink":
    """Build a sink whose tracer lands on the test's InMemorySpanExporter."""
    return OTelSpanSink(tracer=exporter._test_tracer)  # type: ignore[attr-defined]


def _make_event(**overrides: Any) -> dict[str, Any]:
    """Minimal-shape Checkrd telemetry event for sink tests."""
    base: dict[str, Any] = {
        "request_id": "req-001",
        "agent_id": "test-agent",
        "method": "POST",
        "url_host": "api.openai.com",
        "url_path": "/v1/chat/completions",
        "status_code": 200,
        "latency_ms": 142.5,
        "policy_result": "allowed",
        "span_name": "POST api.openai.com",
        "span_status_code": "OK",
    }
    base.update(overrides)
    return base


class TestOTelSpanSinkBasics:
    def test_enqueue_creates_one_span(
        self, span_exporter: InMemorySpanExporter,
    ) -> None:
        sink = _sink_with(span_exporter)
        sink.enqueue(_make_event())
        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1

    def test_span_name_matches_event(
        self, span_exporter: InMemorySpanExporter,
    ) -> None:
        sink = _sink_with(span_exporter)
        sink.enqueue(_make_event(span_name="POST custom.example.com"))
        spans = span_exporter.get_finished_spans()
        assert spans[0].name == "POST custom.example.com"

    def test_span_name_falls_back_to_method_host(
        self, span_exporter: InMemorySpanExporter,
    ) -> None:
        """Without an explicit ``span_name``, derive ``{METHOD} {host}``.

        This matches the OTel HTTP semconv default — dashboards that
        group by span name get sensible buckets without the SDK doing
        anything special.
        """
        sink = _sink_with(span_exporter)
        event = _make_event()
        del event["span_name"]
        sink.enqueue(event)
        spans = span_exporter.get_finished_spans()
        assert spans[0].name == "POST api.openai.com"

    def test_span_kind_is_client(
        self, span_exporter: InMemorySpanExporter,
    ) -> None:
        from opentelemetry.trace import SpanKind

        sink = _sink_with(span_exporter)
        sink.enqueue(_make_event())
        spans = span_exporter.get_finished_spans()
        assert spans[0].kind == SpanKind.CLIENT


class TestSemConvAttributes:
    """Attribute names are a public contract — dashboards hang off them."""

    def test_http_attributes(
        self, span_exporter: InMemorySpanExporter,
    ) -> None:
        sink = _sink_with(span_exporter)
        sink.enqueue(_make_event())
        attrs = span_exporter.get_finished_spans()[0].attributes
        assert attrs is not None
        assert attrs["http.request.method"] == "POST"
        assert attrs["url.full"] == "https://api.openai.com/v1/chat/completions"
        assert attrs["http.response.status_code"] == 200
        assert attrs["checkrd.latency_ms"] == 142.5

    def test_gen_ai_attributes(
        self, span_exporter: InMemorySpanExporter,
    ) -> None:
        """Telemetry events now use the OTel-spec field names directly
        (``gen_ai.provider.name``, ``gen_ai.request.model``, etc.) —
        the URL-derived enrichment in the transport produces the
        provider/operation pair, and the body-derived extractor
        (opt-in via CHECKRD_EXTRACT_GENAI_BODY) produces
        model/usage. The sink iterates a fixed allowlist of known
        OTel keys so dashboards have a single source of truth."""
        sink = _sink_with(span_exporter)
        sink.enqueue(_make_event(**{
            "gen_ai.provider.name": "openai",
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.response.model": "gpt-4o-2024-07-18",
            "gen_ai.usage.input_tokens": 120,
            "gen_ai.usage.output_tokens": 80,
            "gen_ai.request.stream": True,
        }))
        attrs = span_exporter.get_finished_spans()[0].attributes
        assert attrs is not None
        assert attrs["gen_ai.provider.name"] == "openai"
        assert attrs["gen_ai.operation.name"] == "chat"
        assert attrs["gen_ai.request.model"] == "gpt-4o"
        assert attrs["gen_ai.response.model"] == "gpt-4o-2024-07-18"
        assert attrs["gen_ai.usage.input_tokens"] == 120
        assert attrs["gen_ai.usage.output_tokens"] == 80
        assert attrs["gen_ai.request.stream"] is True

    def test_checkrd_namespace_attributes(
        self, span_exporter: InMemorySpanExporter,
    ) -> None:
        sink = _sink_with(span_exporter)
        sink.enqueue(_make_event(
            agent_id="prod-agent-42",
            policy_result="denied",
            deny_reason="rate-limit-exceeded",
            matched_rule="block-new-models",
            matched_rule_kind="deny",
        ))
        attrs = span_exporter.get_finished_spans()[0].attributes
        assert attrs is not None
        assert attrs["checkrd.agent_id"] == "prod-agent-42"
        assert attrs["checkrd.policy_result"] == "denied"
        assert attrs["checkrd.deny_reason"] == "rate-limit-exceeded"
        assert attrs["checkrd.matched_rule"] == "block-new-models"
        assert attrs["checkrd.matched_rule_kind"] == "deny"

    def test_missing_fields_dont_stamp_keys(
        self, span_exporter: InMemorySpanExporter,
    ) -> None:
        """Attributes with a ``None`` / missing source value must not appear.

        Sending ``gen_ai.system = ""`` when the event is not an
        LLM call would corrupt downstream queries like
        ``WHERE gen_ai.system = 'openai'``. Absence is meaningful.
        """
        sink = _sink_with(span_exporter)
        sink.enqueue(_make_event())
        attrs = span_exporter.get_finished_spans()[0].attributes
        assert attrs is not None
        assert "gen_ai.system" not in attrs
        assert "gen_ai.request.model" not in attrs
        assert "checkrd.deny_reason" not in attrs
        assert "checkrd.matched_rule" not in attrs


class TestSpanStatus:
    def test_status_ok_is_set_on_allowed(
        self, span_exporter: InMemorySpanExporter,
    ) -> None:
        from opentelemetry.trace import StatusCode

        sink = _sink_with(span_exporter)
        sink.enqueue(_make_event(span_status_code="OK"))
        span = span_exporter.get_finished_spans()[0]
        assert span.status.status_code == StatusCode.OK

    def test_status_error_carries_message(
        self, span_exporter: InMemorySpanExporter,
    ) -> None:
        from opentelemetry.trace import StatusCode

        sink = _sink_with(span_exporter)
        sink.enqueue(_make_event(
            span_status_code="ERROR",
            span_status_message="rate-limit exceeded",
        ))
        span = span_exporter.get_finished_spans()[0]
        assert span.status.status_code == StatusCode.ERROR
        assert span.status.description == "rate-limit exceeded"

    def test_status_unset_when_not_provided(
        self, span_exporter: InMemorySpanExporter,
    ) -> None:
        from opentelemetry.trace import StatusCode

        sink = _sink_with(span_exporter)
        event = _make_event()
        del event["span_status_code"]
        sink.enqueue(event)
        span = span_exporter.get_finished_spans()[0]
        assert span.status.status_code == StatusCode.UNSET


class TestInjection:
    def test_accepts_explicit_tracer(
        self, span_exporter: InMemorySpanExporter,
    ) -> None:
        """Callers who don't want to touch the global provider can inject.

        Common case: the app uses multiple TracerProviders (one per
        tenant, or one for internal vs customer traffic). The sink must
        accept a specific tracer without fiddling with the global.
        """
        custom_provider = TracerProvider()
        custom_exporter = InMemorySpanExporter()
        custom_provider.add_span_processor(SimpleSpanProcessor(custom_exporter))
        custom_tracer = custom_provider.get_tracer("custom")

        sink = OTelSpanSink(tracer=custom_tracer)
        sink.enqueue(_make_event())

        # Event lands on the custom provider, NOT the global.
        assert len(custom_exporter.get_finished_spans()) == 1
        assert len(span_exporter.get_finished_spans()) == 0


class TestRobustness:
    def test_stop_is_idempotent(
        self, span_exporter: InMemorySpanExporter,
    ) -> None:
        sink = _sink_with(span_exporter)
        sink.stop()
        sink.stop()  # must not raise

    def test_enqueue_after_stop_is_noop(
        self, span_exporter: InMemorySpanExporter,
    ) -> None:
        sink = _sink_with(span_exporter)
        sink.stop()
        sink.enqueue(_make_event())
        assert len(span_exporter.get_finished_spans()) == 0

    def test_malformed_event_does_not_raise(
        self, span_exporter: InMemorySpanExporter,
    ) -> None:
        """A broken event (wrong type, missing critical field) must NOT
        raise out of enqueue(). Telemetry is best-effort; a bug in the
        host app's event shaping cannot crash the request hot path."""
        sink = _sink_with(span_exporter)
        # `method` as int will trip attribute-setting on some OTel
        # exporters — the sink must swallow it and continue.
        sink.enqueue({"method": 42, "url_host": None})
        # Either no span emitted or a degraded one — either way no
        # exception bubbled up. (Span count is implementation-
        # dependent; we just assert no throw.)


class TestDocstringDiscoverability:
    def test_exports_from_package_root(self) -> None:
        import checkrd

        assert hasattr(checkrd, "OTelSpanSink")
        assert "OTelSpanSink" in checkrd.__all__


def test_importerror_when_otel_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """If ``opentelemetry-api`` isn't installed, the constructor must
    raise with an actionable message pointing at both the minimal install
    and the SDK-managed alternative. Simulated via import patching.
    """
    import sys

    # Preserve and wipe. The subsequent re-import of checkrd.sinks
    # re-runs the OTelSpanSink constructor which imports
    # `opentelemetry.trace` at call time.
    saved = {}
    for name in list(sys.modules):
        if name.startswith("opentelemetry"):
            saved[name] = sys.modules.pop(name)

    # Block future imports.
    import builtins

    real_import = builtins.__import__

    def blocking_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("opentelemetry"):
            raise ImportError(f"(simulated) no module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocking_import)

    try:
        # Default path uses `trace.get_tracer(...)` which triggers the import.
        with pytest.raises(ImportError, match="opentelemetry-api"):
            OTelSpanSink()
    finally:
        sys.modules.update(saved)
