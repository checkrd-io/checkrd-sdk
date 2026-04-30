"""Pluggable telemetry sinks for the Checkrd Python SDK.

Telemetry events flow from the WASM core through the HTTP transport into a
:class:`TelemetrySink`. The sink is the customer-controlled destination —
the Checkrd control plane (the default cloud sink, :class:`TelemetryBatcher`),
a local file (:class:`JsonFileSink`), Python ``logging`` (:class:`LoggingSink`),
or any custom implementation that satisfies the protocol.

This module exists to give Tier 3 (offline / air-gapped) deployments a clean
local telemetry story without requiring the Checkrd control plane. It also
documents the contract that custom sinks must satisfy so customers can plug
in their own destinations (Datadog Agent, Vector, OTLP collector, syslog,
fluent-bit, Loki, etc.).

Example::

    from checkrd import wrap, LocalIdentity
    from checkrd.sinks import JsonFileSink
    import httpx

    # Offline / Tier 3: append every telemetry event as a JSON line.
    # Use logrotate or your log shipper to handle rotation.
    client = wrap(
        httpx.Client(),
        agent_id="sales-agent",
        policy="/etc/checkrd/policy.yaml",
        telemetry_sink=JsonFileSink("/var/log/checkrd/sales-agent.jsonl"),
    )
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional, Protocol, Union, runtime_checkable

logger = logging.getLogger("checkrd")


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class TelemetrySink(Protocol):
    """Where enriched telemetry events go after policy evaluation.

    Implementors must be thread-safe (the SDK calls ``enqueue`` from any
    thread) and must NEVER block the calling thread for I/O — buffer
    internally and flush asynchronously, or write non-blockingly.

    The SDK calls :meth:`stop` on ``client.close()`` and via ``atexit``.
    Implementations should make ``stop`` idempotent.
    """

    def enqueue(self, event: dict[str, Any]) -> None:
        """Add an event to the sink. Must be thread-safe and non-blocking.

        Implementations should buffer events and flush asynchronously rather
        than block the request thread. The HTTP transport calls this from
        whichever thread (or coroutine) made the wrapped request.
        """
        ...

    def stop(self) -> None:
        """Flush any buffered events and release resources.

        Idempotent — safe to call multiple times. Called automatically by
        ``atexit`` and by ``client.close()``.
        """
        ...


# ---------------------------------------------------------------------------
# JsonFileSink
# ---------------------------------------------------------------------------


class JsonFileSink:
    """Append enriched telemetry events as JSON lines to a file.

    One event per line, no trailing comma, UTF-8. Designed to be consumed by
    log shippers like Vector, Fluent Bit, Promtail, Filebeat, or fluentd.
    Customers handle rotation via ``logrotate`` or their log shipper —
    ``JsonFileSink`` does not rotate or truncate.

    Thread safety: writes are protected by a ``threading.Lock``. Multiple
    SDK instances writing to the same file concurrently are also safe IF
    each event is smaller than ``PIPE_BUF`` (typically 4096 bytes on Linux),
    because POSIX guarantees atomic appends below that size. Events larger
    than ``PIPE_BUF`` should use one ``JsonFileSink`` per process or rely on
    a shared lock.

    Args:
        path: File path. Parent directory is created if missing.
        fsync: If True, ``os.fsync`` after every write. Slow but durable
            against power loss. Default False (let the kernel buffer).

    Example:
        >>> from checkrd.sinks import JsonFileSink
        >>> import tempfile, json, os
        >>> with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.jsonl') as f:
        ...     path = f.name
        >>> sink = JsonFileSink(path)
        >>> sink.enqueue({"event": "allowed", "url": "api.stripe.com"})
        >>> sink.stop()
        >>> with open(path) as f:
        ...     line = f.readline()
        >>> parsed = json.loads(line)
        >>> parsed["event"]
        'allowed'
        >>> os.unlink(path)
    """

    def __init__(
        self,
        path: Union[str, Path],
        *,
        fsync: bool = False,
    ) -> None:
        self._path = Path(path)
        self._fsync = fsync
        self._lock = threading.Lock()
        self._file: Optional[Any] = None
        self._stopped = False

        # Create parent directory if missing.
        self._path.parent.mkdir(parents=True, exist_ok=True)

        # Open in append mode. Line buffering keeps individual writes flushed.
        self._file = open(self._path, "a", encoding="utf-8", buffering=1)
        atexit.register(self.stop)

    def enqueue(self, event: dict[str, Any]) -> None:
        """Append a single event as a JSON line. Thread-safe."""
        if self._stopped or self._file is None:
            return
        try:
            line = json.dumps(event, separators=(",", ":"), default=str) + "\n"
        except Exception as exc:  # noqa: BLE001 - serialization must NEVER crash the request thread
            # Catches every failure mode of json.dumps + the default=str
            # fallback (TypeError, ValueError, RuntimeError from a custom
            # __str__, anything). The contract is "telemetry is best-effort":
            # a serialization bug must never propagate to the wrapped HTTP
            # call. Drop the event with a warning and move on.
            logger.warning(
                "checkrd.sinks: failed to serialize event for JsonFileSink (%s); "
                "dropping event",
                exc,
            )
            return

        with self._lock:
            if self._stopped or self._file is None:
                return
            try:
                self._file.write(line)
                if self._fsync:
                    self._file.flush()
                    os.fsync(self._file.fileno())
            except OSError as exc:
                logger.warning(
                    "checkrd.sinks: write to %s failed (%s); dropping event",
                    self._path,
                    exc,
                )

    def stop(self) -> None:
        """Flush and close the file. Idempotent."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            if self._file is not None:
                try:
                    self._file.flush()
                    if self._fsync:
                        try:
                            os.fsync(self._file.fileno())
                        except OSError:
                            pass
                    self._file.close()
                except OSError as exc:
                    logger.debug(
                        "checkrd.sinks: error closing %s (%s)", self._path, exc
                    )
                self._file = None


# ---------------------------------------------------------------------------
# LoggingSink
# ---------------------------------------------------------------------------


class LoggingSink:
    """Route enriched telemetry events through Python ``logging``.

    Useful when the customer's observability stack already consumes Python
    logging output: Datadog Agent (via the python.log.format integration),
    Sentry breadcrumbs, journald via systemd, or any structured-logging
    library that hooks into the standard ``logging`` module.

    Each event becomes one log record with the event dict attached as the
    ``extra={"event": ...}`` keyword. The message is the literal string
    ``"checkrd telemetry"`` so log filters can target it.

    Args:
        logger: Custom ``logging.Logger`` instance. Defaults to
            ``logging.getLogger("checkrd.telemetry")``.
        level: Log level for events. Defaults to ``logging.INFO``.

    Example:
        >>> import logging
        >>> from checkrd.sinks import LoggingSink
        >>> sink = LoggingSink()
        >>> sink.enqueue({"event": "allowed", "url": "api.stripe.com"})
        >>> sink.stop()  # no-op (no buffering)
    """

    def __init__(
        self,
        logger: Optional[logging.Logger] = None,
        *,
        level: int = logging.INFO,
    ) -> None:
        self._logger = logger or logging.getLogger("checkrd.telemetry")
        self._level = level

    def enqueue(self, event: dict[str, Any]) -> None:
        """Emit one log record per event. Thread-safe (Python logging is)."""
        # Python's logging is thread-safe by default.
        self._logger.log(self._level, "checkrd telemetry", extra={"event": event})

    def stop(self) -> None:
        """No-op — logging has no buffer to flush."""
        # Python's logging library handles its own flushing via handlers.


# ---------------------------------------------------------------------------
# Re-export TelemetryBatcher as ControlPlaneSink for clarity
# ---------------------------------------------------------------------------

# The existing TelemetryBatcher is the cloud / control-plane sink. It already
# satisfies the TelemetrySink protocol structurally (it has enqueue and stop
# methods with compatible signatures). We re-export it here under a clearer
# name so customers configuring sinks have an obvious place to find it.
from checkrd.batcher import TelemetryBatcher as ControlPlaneSink  # noqa: E402


# ---------------------------------------------------------------------------
# OtlpSink — dual-export to any OTLP endpoint (Datadog, Honeycomb, Grafana)
# ---------------------------------------------------------------------------


class OtlpSink:
    """Export telemetry events as OTLP/HTTP traces to an external collector.

    This is the industry-standard way to get Checkrd data into existing
    observability stacks: Datadog, Honeycomb, Grafana, Axiom, New Relic,
    or any OpenTelemetry Collector. The customer's existing OTLP endpoint
    receives traces directly from the SDK without routing through the
    Checkrd control plane.

    **Requires** ``pip install checkrd[otlp]`` which pulls in
    ``opentelemetry-exporter-otlp-proto-http``. If the dependency is
    missing, ``__init__`` raises ``ImportError`` with an actionable message.

    Events are batched by the OTel SDK's ``BatchSpanProcessor`` with
    configurable schedule (default: 5s or 512 spans, whichever first).

    Args:
        endpoint: OTLP/HTTP endpoint URL (e.g., ``https://otlp.datadoghq.com``).
        headers: Auth headers as a dict (e.g., ``{"DD-API-KEY": "..."}``).
        service_name: The ``service.name`` resource attribute. Defaults to
            ``"checkrd-agent"``.

    Example::

        from checkrd import wrap
        from checkrd.sinks import OtlpSink

        # Dual-export: Checkrd control plane + Datadog
        client = wrap(
            httpx.Client(),
            agent_id="sales-agent",
            api_key="ck_live_...",
            control_plane_url="https://api.checkrd.io",
            telemetry_sink=OtlpSink(
                endpoint="https://otlp.datadoghq.com:4318",
                headers={"DD-API-KEY": os.environ["DD_API_KEY"]},
            ),
        )
    """

    def __init__(
        self,
        endpoint: str,
        *,
        headers: dict[str, str] | None = None,
        service_name: str = "checkrd-agent",
    ) -> None:
        try:
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
        except ImportError:
            raise ImportError(
                "OtlpSink requires the OpenTelemetry OTLP exporter. "
                "Install with: pip install checkrd[otlp]"
            ) from None

        resource = Resource.create({"service.name": service_name})
        exporter = OTLPSpanExporter(
            endpoint=f"{endpoint.rstrip('/')}/v1/traces",
            headers=headers or {},
        )
        self._provider = TracerProvider(resource=resource)
        self._provider.add_span_processor(BatchSpanProcessor(exporter))
        self._tracer = self._provider.get_tracer("checkrd.otlp_sink")
        self._stopped = False

    def enqueue(self, event: dict[str, Any]) -> None:
        """Translate a Checkrd telemetry event to an OTel span and export."""
        if self._stopped:
            return
        try:
            self._emit_span(event)
        except Exception:  # noqa: BLE001
            logger.debug("OtlpSink: failed to emit span, dropping event", exc_info=True)

    def _emit_span(self, event: dict[str, Any]) -> None:
        """Create and immediately end an OTel span from a Checkrd event."""
        from opentelemetry.trace import StatusCode, SpanKind

        span_name = event.get("span_name", f"{event.get('method', '?')} {event.get('url_host', '?')}")
        kind = SpanKind.CLIENT  # Checkrd events represent outbound HTTP calls

        with self._tracer.start_as_current_span(span_name, kind=kind) as span:
            # HTTP attributes
            span.set_attribute("http.request.method", event.get("method", ""))
            span.set_attribute("url.full", f"https://{event.get('url_host', '')}{event.get('url_path', '/')}")
            if event.get("status_code") is not None:
                span.set_attribute("http.response.status_code", event["status_code"])
            if event.get("latency_ms") is not None:
                span.set_attribute("checkrd.latency_ms", event["latency_ms"])

            # GenAI attributes
            if event.get("gen_ai_system"):
                span.set_attribute("gen_ai.system", event["gen_ai_system"])
            if event.get("gen_ai_model"):
                span.set_attribute("gen_ai.request.model", event["gen_ai_model"])
            if event.get("gen_ai_input_tokens") is not None:
                span.set_attribute("gen_ai.usage.input_tokens", event["gen_ai_input_tokens"])
            if event.get("gen_ai_output_tokens") is not None:
                span.set_attribute("gen_ai.usage.output_tokens", event["gen_ai_output_tokens"])

            # Checkrd-specific attributes
            span.set_attribute("checkrd.agent_id", event.get("agent_id", ""))
            if event.get("policy_result"):
                span.set_attribute("checkrd.policy_result", event["policy_result"])
            if event.get("deny_reason"):
                span.set_attribute("checkrd.deny_reason", event["deny_reason"])

            # Status
            status_code = event.get("span_status_code", "UNSET")
            if status_code == "ERROR":
                span.set_status(StatusCode.ERROR, event.get("span_status_message", ""))
            elif status_code == "OK":
                span.set_status(StatusCode.OK)

    def stop(self) -> None:
        """Flush pending spans and shut down the OTLP exporter. Idempotent."""
        if self._stopped:
            return
        self._stopped = True
        try:
            self._provider.shutdown()
        except Exception:  # noqa: BLE001
            logger.debug("OtlpSink: error during shutdown", exc_info=True)


__all__ = [
    "TelemetrySink",
    "JsonFileSink",
    "LoggingSink",
    "ControlPlaneSink",
    "OtlpSink",
    "OTelSpanSink",
]


# ---------------------------------------------------------------------------
# OTelSpanSink — uses the user's ALREADY-CONFIGURED OTel tracer
# ---------------------------------------------------------------------------
#
# `OtlpSink` owns a TracerProvider + BatchSpanProcessor and ships OTLP/HTTP
# to an explicit endpoint. That's right for teams without OTel already in
# place. But teams that already have OTel running (Datadog APM, Honeycomb,
# a custom collector) need a sink that USES the existing tracer — respects
# their sampler, resource attributes, propagator, and exporter choices.
#
# `OTelSpanSink` solves that case. It pulls a tracer from
# `opentelemetry.trace.get_tracer()` by default (which resolves through the
# global TracerProvider the user's app already configured) and creates a
# Checkrd span per telemetry event.
#
# Attributes follow OTel semconv where they exist (http.*, gen_ai.*) and
# use the `checkrd.*` namespace for SDK-specific metadata. Matches the
# attribute set emitted by `OtlpSink` exactly so operators who query their
# observability stack for "checkrd.policy_result = 'denied'" get the same
# result either way.


class OTelSpanSink:
    """Emit Checkrd telemetry as spans on the caller's OpenTelemetry tracer.

    Use when your application already has OpenTelemetry configured
    (Datadog APM, Honeycomb, Grafana Cloud, a custom exporter). Unlike
    :class:`OtlpSink` — which owns its own ``TracerProvider`` and ships
    OTLP/HTTP to a fixed endpoint — ``OTelSpanSink`` defers to the
    global tracer provider so spans flow through the exporter, sampler,
    resource attributes, and propagator the host app already set up.

    Requires ``opentelemetry-api >= 1.20`` at runtime. The API package is
    very small (~100 KB) and is a peer dependency of essentially every
    Python observability stack — if your app can send any OTel data at
    all, the api package is already installed. If it isn't, the
    constructor raises ``ImportError`` with an actionable message.

    Args:
        tracer: Optional explicit tracer. Defaults to
            ``opentelemetry.trace.get_tracer("checkrd.sdk", <version>)``
            — i.e., the global provider the caller has configured.
        service_name: Deprecated and ignored. Resource attributes live
            on the TracerProvider the caller owns; the sink must not
            override them. Kept as a kwarg for API symmetry with
            :class:`OtlpSink` so callers can swap sinks without
            signature changes.

    Example::

        # app setup: OTel SDK is configured however the customer likes
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        trace.set_tracer_provider(TracerProvider())

        # Checkrd side: telemetry goes through the existing OTel pipeline
        from checkrd import wrap
        from checkrd.sinks import OTelSpanSink

        client = wrap(
            httpx.Client(),
            api_key="ck_live_...",
            telemetry_sink=OTelSpanSink(),
        )
    """

    def __init__(
        self,
        *,
        tracer: Any = None,
        service_name: str = "",  # intentionally ignored; see docstring
    ) -> None:
        # ``service_name`` is accepted but not used — see docstring.
        del service_name

        if tracer is None:
            try:
                from opentelemetry import trace
            except ImportError:
                raise ImportError(
                    "OTelSpanSink requires `opentelemetry-api` (>=1.20). "
                    "Install with: pip install opentelemetry-api\n"
                    "Or, if you want Checkrd to install and manage the "
                    "OTel SDK for you (with an exporter), use OtlpSink "
                    "instead: `pip install checkrd[otlp]`.",
                ) from None
            from checkrd._version import __version__

            tracer = trace.get_tracer("checkrd.sdk", __version__)

        self._tracer = tracer
        self._stopped = False

    def enqueue(self, event: dict[str, Any]) -> None:
        """Create and finish a span for the given Checkrd telemetry event.

        Span shape follows OTel HTTP semconv (``http.request.method``,
        ``url.full``, ``http.response.status_code``), OTel GenAI semconv
        (``gen_ai.system``, ``gen_ai.request.model``, ``gen_ai.usage.*``),
        and the ``checkrd.*`` namespace for policy-engine fields
        (``checkrd.agent_id``, ``checkrd.policy_result``,
        ``checkrd.matched_rule``, ``checkrd.deny_reason``).
        """
        if self._stopped:
            return
        try:
            self._emit_span(event)
        except Exception:
            logger.debug(
                "OTelSpanSink: failed to emit span, dropping event",
                exc_info=True,
            )

    def _emit_span(self, event: dict[str, Any]) -> None:
        from opentelemetry.trace import SpanKind, StatusCode

        span_name = event.get(
            "span_name",
            f"{event.get('method', '?')} {event.get('url_host', '?')}",
        )
        # Every Checkrd event is an outbound HTTP client call — fixed
        # SpanKind.CLIENT matches the OTel HTTP semconv choice.
        with self._tracer.start_as_current_span(
            span_name, kind=SpanKind.CLIENT,
        ) as span:
            _apply_semconv_attributes(span, event)

            # Status. OTel StatusCode: UNSET=0, OK=1, ERROR=2.
            status_code = event.get("span_status_code", "UNSET")
            if status_code == "ERROR":
                span.set_status(
                    StatusCode.ERROR,
                    event.get("span_status_message", ""),
                )
            elif status_code == "OK":
                span.set_status(StatusCode.OK)

    def stop(self) -> None:
        """Idempotent. The caller's TracerProvider handles flushing."""
        self._stopped = True


def _apply_semconv_attributes(span: Any, event: dict[str, Any]) -> None:
    """Stamp OTel semconv + Checkrd namespace attributes on ``span``.

    Extracted so :class:`OTelSpanSink` and :class:`OtlpSink` both emit
    identical attribute shapes — operators querying their observability
    stack get the same answers regardless of which sink routed the
    event. Drift between the two is a documented regression risk
    (different dashboards would need to query different attr names).
    """
    # --- HTTP semconv (stable v1.x) ------------------------------------
    method = event.get("method")
    if method:
        span.set_attribute("http.request.method", method)
    url_host = event.get("url_host")
    url_path = event.get("url_path", "/")
    if url_host:
        span.set_attribute("url.full", f"https://{url_host}{url_path}")
    status_code = event.get("status_code")
    if status_code is not None:
        span.set_attribute("http.response.status_code", status_code)
    latency_ms = event.get("latency_ms")
    if latency_ms is not None:
        span.set_attribute("checkrd.latency_ms", latency_ms)

    # --- GenAI semconv (1.27+) ----------------------------------------
    # Two attribute-source layers, both stamped here so a span carries
    # the full GenAI picture regardless of which path produced it:
    #
    #   1. URL-derived (always on) — ``gen_ai.provider.name`` and
    #      ``gen_ai.operation.name`` from the request URL
    #      (see ``_genai.attributes_for_url``). Cheap, no body
    #      buffering required.
    #
    #   2. Body-derived (opt-in via ``CHECKRD_EXTRACT_GENAI_BODY``) —
    #      ``gen_ai.request.model``, ``gen_ai.response.model``,
    #      ``gen_ai.usage.input_tokens``, ``gen_ai.usage.output_tokens``,
    #      ``gen_ai.request.stream``. Requires parsing JSON bodies, so
    #      gated by an explicit opt-in to keep PII surface bounded
    #      (see ``_genai_body``).
    #
    # The transport layer writes these keys directly onto the
    # telemetry event using the OTel-spec names, so the sink just
    # passes them through. Iterating over a fixed list (rather than
    # ``for k in event if k.startswith("gen_ai.")``) keeps the
    # contract auditable — a dashboard query for a specific
    # attribute name has a single source-of-truth.
    for attr_name in (
        "gen_ai.provider.name",
        "gen_ai.operation.name",
        "gen_ai.request.model",
        "gen_ai.response.model",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.request.stream",
    ):
        value = event.get(attr_name)
        if value is not None:
            span.set_attribute(attr_name, value)

    # --- Checkrd namespace ---------------------------------------------
    agent_id = event.get("agent_id")
    if agent_id:
        span.set_attribute("checkrd.agent_id", agent_id)
    policy_result = event.get("policy_result")
    if policy_result:
        span.set_attribute("checkrd.policy_result", policy_result)
    deny_reason = event.get("deny_reason")
    if deny_reason:
        span.set_attribute("checkrd.deny_reason", deny_reason)
    matched_rule = event.get("matched_rule")
    if matched_rule:
        span.set_attribute("checkrd.matched_rule", matched_rule)
    matched_rule_kind = event.get("matched_rule_kind")
    if matched_rule_kind:
        span.set_attribute("checkrd.matched_rule_kind", matched_rule_kind)
