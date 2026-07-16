"""Tests for OtlpSink — the OTLP/HTTP trace exporter sink.

Tests the sink's event-to-span translation and lifecycle management.
Uses a custom in-memory exporter since OTel 1.40 removed InMemorySpanExporter.
"""

import pytest

otel_available = True
try:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.trace import StatusCode
except ImportError:
    otel_available = False
    # Stub the base class so the module-level class definition doesn't crash.
    # The pytestmark below ensures no tests actually run.
    SpanExporter = object  # type: ignore[misc,assignment]
    SpanExportResult = None  # type: ignore[misc,assignment]

pytestmark = pytest.mark.skipif(
    not otel_available,
    reason="opentelemetry not installed (pip install checkrd[otlp])",
)


class CapturingExporter(SpanExporter):  # type: ignore[misc]
    """Minimal in-memory exporter for tests."""

    def __init__(self):
        self.spans = []

    def export(self, spans):
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


def make_test_sink():
    """Create an OtlpSink wired to a CapturingExporter."""
    from checkrd.sinks import OtlpSink

    sink = OtlpSink.__new__(OtlpSink)
    exporter = CapturingExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    sink._provider = provider
    sink._tracer = provider.get_tracer("test")
    sink._stopped = False
    return sink, exporter


def sample_event(**overrides):
    # Telemetry events carry the GenAI semconv attributes under their
    # OTel-spec dotted names (the transport stamps ``gen_ai.provider.name``
    # / ``gen_ai.operation.name``; the opt-in body extractor adds
    # ``gen_ai.request.model`` / ``gen_ai.usage.*``). Framework adapters
    # (LangChain, OpenAI Agents) instead write the flat ``gen_ai_system``
    # wire-schema family and may enqueue straight to a span sink — that
    # path is covered by ``test_enqueue_flat_wire_keys_from_adapter``.
    event = {
        "request_id": "req-001",
        "agent_id": "550e8400-e29b-41d4-a716-446655440000",
        "timestamp": "2026-04-08T10:00:00Z",
        "url_host": "api.anthropic.com",
        "url_path": "/v1/messages",
        "method": "POST",
        "status_code": 200,
        "latency_ms": 1250,
        "policy_result": "allowed",
        "span_name": "POST api.anthropic.com",
        "span_kind": "CLIENT",
        "span_status_code": "OK",
        "gen_ai.provider.name": "anthropic",
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": "claude-sonnet-4-20250514",
        "gen_ai.usage.input_tokens": 1500,
        "gen_ai.usage.output_tokens": 350,
    }
    event.update(overrides)
    return event


class TestOtlpSink:
    def test_enqueue_creates_span_with_correct_attributes(self):
        sink, exporter = make_test_sink()
        sink.enqueue(sample_event())

        assert len(exporter.spans) == 1
        span = exporter.spans[0]
        assert span.name == "POST api.anthropic.com"
        attrs = dict(span.attributes)
        assert attrs["http.request.method"] == "POST"
        # Emits the current OTel GenAI provider attribute, NOT the
        # deprecated ``gen_ai.system``.
        assert attrs["gen_ai.provider.name"] == "anthropic"
        assert "gen_ai.system" not in attrs
        assert attrs["gen_ai.operation.name"] == "chat"
        assert attrs["gen_ai.request.model"] == "claude-sonnet-4-20250514"
        assert attrs["gen_ai.usage.input_tokens"] == 1500
        assert attrs["gen_ai.usage.output_tokens"] == 350
        assert attrs["checkrd.policy_result"] == "allowed"
        assert attrs["checkrd.agent_id"] == "550e8400-e29b-41d4-a716-446655440000"

    def test_enqueue_minimal_event(self):
        sink, exporter = make_test_sink()
        sink.enqueue({"method": "GET", "url_host": "api.stripe.com", "url_path": "/v1/charges"})

        assert len(exporter.spans) == 1
        assert exporter.spans[0].name == "GET api.stripe.com"

    def test_stop_is_idempotent(self):
        sink, _ = make_test_sink()
        sink.stop()
        sink.stop()  # no crash

    def test_enqueue_after_stop_is_noop(self):
        sink, exporter = make_test_sink()
        sink.stop()
        sink.enqueue(sample_event())
        assert len(exporter.spans) == 0

    def test_error_status_mapped(self):
        sink, exporter = make_test_sink()
        sink.enqueue(
            sample_event(
                span_status_code="ERROR",
                span_status_message="upstream timeout",
                status_code=504,
            )
        )

        span = exporter.spans[0]
        assert span.status.status_code == StatusCode.ERROR
        assert span.status.description == "upstream timeout"

    def test_ok_status_mapped(self):
        sink, exporter = make_test_sink()
        sink.enqueue(sample_event(span_status_code="OK"))

        span = exporter.spans[0]
        assert span.status.status_code == StatusCode.OK

    def test_satisfies_telemetry_sink_protocol(self):
        from checkrd.sinks import TelemetrySink

        sink, _ = make_test_sink()
        assert isinstance(sink, TelemetrySink)

    def test_gen_ai_fields_optional(self):
        """Events without GenAI fields don't set gen_ai.* attributes."""
        sink, exporter = make_test_sink()
        sink.enqueue(
            {
                "method": "GET",
                "url_host": "api.stripe.com",
                "url_path": "/v1/charges",
                "status_code": 200,
                "span_status_code": "OK",
            }
        )

        attrs = dict(exporter.spans[0].attributes)
        assert "gen_ai.system" not in attrs
        assert "gen_ai.provider.name" not in attrs
        assert "gen_ai.request.model" not in attrs

    def test_enqueue_flat_wire_keys_from_adapter(self):
        """Flat wire-schema GenAI keys (the LangChain / OpenAI Agents
        adapters write ``gen_ai_system`` / ``gen_ai_model`` / token keys)
        still map onto the modern OTel attribute names when an adapter
        handler is constructed with ``sink=OtlpSink(...)``. The deprecated
        ``gen_ai_system`` alias resolves to ``gen_ai.provider.name``; the
        deprecated attribute name itself is never emitted."""
        sink, exporter = make_test_sink()
        sink.enqueue(
            {
                "method": "POST",
                "url_host": "llm.langchain",
                "url_path": "/chat_model/gpt-4o",
                "status_code": 200,
                "span_status_code": "OK",
                # Flat keys, exactly as the LangChain adapter writes them.
                "gen_ai_system": "openai",
                "gen_ai_model": "gpt-4o",
                "gen_ai_input_tokens": 1200,
                "gen_ai_output_tokens": 300,
            }
        )

        attrs = dict(exporter.spans[0].attributes)
        assert attrs["gen_ai.provider.name"] == "openai"
        assert "gen_ai.system" not in attrs
        assert attrs["gen_ai.request.model"] == "gpt-4o"
        assert attrs["gen_ai.usage.input_tokens"] == 1200
        assert attrs["gen_ai.usage.output_tokens"] == 300

    def test_dotted_key_takes_precedence_over_flat_alias(self):
        """When both the dotted OTel-spec key and the flat alias are
        present, the dotted key wins — one emitted value, never dual."""
        sink, exporter = make_test_sink()
        # sample_event already carries gen_ai.provider.name = "anthropic".
        sink.enqueue(sample_event(gen_ai_system="openai"))

        attrs = dict(exporter.spans[0].attributes)
        assert attrs["gen_ai.provider.name"] == "anthropic"
        assert "gen_ai.system" not in attrs
