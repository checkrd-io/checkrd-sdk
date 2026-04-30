"""OpenAI Agents SDK integration for Checkrd.

The OpenAI Agents SDK splits its extension points across two layers:

1. **Tracing processors** — observation-only. Receive ``on_trace_*`` and
   ``on_span_*`` events; cannot abort a run.
2. **Guardrails** — enforcement. Run before the model invocation and
   can ``tripwire`` the agent (raising a ``GuardrailTripwireTriggered``
   exception) to abort the run.

Checkrd ships both:

- :class:`CheckrdTracingProcessor` — emits a Checkrd telemetry event
  per span, with span-data-aware enrichment (model name and token
  counts for ``GenerationSpanData``, function name for
  ``FunctionSpanData``, etc.). Registered via
  :func:`agents.add_trace_processor` so the OpenAI traces dashboard
  keeps working alongside Checkrd.
- :class:`CheckrdInputGuardrail` — evaluates the agent input through
  the WASM core before the run starts and tripwires the agent on
  deny. Registered on the agent constructor.
- :class:`CheckrdOutputGuardrail` — same, for the final output.
  Optional; ship :class:`CheckrdInputGuardrail` for input safety and
  add this when output filtering is required.

Usage::

    from agents import Agent, Runner, add_trace_processor
    from checkrd import Checkrd
    from checkrd.integrations.openai_agents import (
        CheckrdInputGuardrail,
        CheckrdTracingProcessor,
    )

    with Checkrd() as client:
        # 1. Wire telemetry (does not enforce)
        add_trace_processor(CheckrdTracingProcessor.from_checkrd(client))

        # 2. Wire enforcement on each agent
        agent = Agent(
            name="research-agent",
            input_guardrails=[CheckrdInputGuardrail.from_checkrd(client)],
        )
        result = Runner.run_sync(agent, "summarize tomorrow's calendar")

Why this split:

- The Agents SDK chose this design intentionally — tracing is an
  observability primitive (must be cheap, must not affect run
  semantics), guardrails are an enforcement primitive (can mutate
  control flow).
- Pretending tracing can enforce would be unsupported and break in
  the next minor version. Better to use the SDK's intended seams.

Span correlation: the Agents SDK uses OpenTelemetry-shaped IDs.
``trace_id`` is the root correlation ID; spans carry ``span_id`` and
``parent_id``. The tracing processor emits these as the Checkrd
``request_id`` / ``span_id`` / ``parent_span_id`` so trace tree
reconstruction works downstream.

Reference: https://openai.github.io/openai-agents-python/tracing/
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

# Hard imports: this module requires the openai-agents package.
from agents import (
    GuardrailFunctionOutput,
    InputGuardrail,
    OutputGuardrail,
)
from agents.tracing import Span, Trace, TracingProcessor

from checkrd._state import _GlobalContext, get_context
from checkrd.engine import EvalResult, WasmEngine
from checkrd.sinks import TelemetrySink

if TYPE_CHECKING:
    from agents import RunContextWrapper

    from checkrd.client import Checkrd

logger = logging.getLogger("checkrd.integrations.openai_agents")


# Synthetic URL authority for OpenAI Agents events. Policy authors
# match against ``https://openai-agents.local/agent/...``,
# ``.../function/...``, etc.
_AUTHORITY = "openai-agents.local"


# ----------------------------------------------------------------------
# Tracing processor (observability)
# ----------------------------------------------------------------------


class CheckrdTracingProcessor(TracingProcessor):
    """Emit a Checkrd telemetry event per Agents SDK span.

    The Agents SDK invokes this processor synchronously in the agent's
    run loop. Methods MUST be fast and non-blocking — heavy work goes
    on the sink's background thread/queue (which is the
    :class:`TelemetryBatcher` or :class:`AsyncTelemetryBatcher` in the
    typical case, both of which buffer asynchronously).

    Args:
        engine: WASM engine instance (used as a hashing/correlation
            anchor only — this processor does NOT call ``evaluate()``).
        agent_id: Agent identifier for telemetry correlation.
        sink: Optional telemetry sink. When ``None``, the processor is
            a no-op (useful for tests).
        logger_: Optional logger.

    Example::

        from agents import add_trace_processor
        proc = CheckrdTracingProcessor.from_checkrd(client)
        add_trace_processor(proc)
    """

    def __init__(
        self,
        *,
        engine: WasmEngine,
        agent_id: str,
        sink: Optional[TelemetrySink] = None,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self._engine = engine
        self._agent_id = agent_id
        self._sink = sink
        self._logger = logger_ or logger

    @classmethod
    def from_checkrd(cls, client: "Checkrd") -> "CheckrdTracingProcessor":
        client._ensure_global_context()
        return cls.from_global()

    @classmethod
    def from_global(cls) -> "CheckrdTracingProcessor":
        ctx: _GlobalContext = get_context()
        return cls(
            engine=ctx.engine,
            agent_id=ctx.settings.agent_id,
            sink=ctx.sink,
        )

    # ------------------------------------------------------------------
    # TracingProcessor protocol
    # ------------------------------------------------------------------

    def on_trace_start(self, trace: Trace) -> None:
        if self._sink is None:
            return
        self._enqueue_safe(
            {
                "event_type": "openai_agents_trace_start",
                "request_id": getattr(trace, "trace_id", "") or "",
                "agent_id": self._agent_id,
                "trace_name": getattr(trace, "name", None),
            }
        )

    def on_trace_end(self, trace: Trace) -> None:
        if self._sink is None:
            return
        self._enqueue_safe(
            {
                "event_type": "openai_agents_trace_end",
                "request_id": getattr(trace, "trace_id", "") or "",
                "agent_id": self._agent_id,
                "trace_name": getattr(trace, "name", None),
            }
        )

    def on_span_start(self, span: Span[Any]) -> None:
        if self._sink is None:
            return
        kind, target, extra = _classify_span(span)
        self._enqueue_safe(
            {
                "event_type": f"openai_agents_{kind}_start",
                "request_id": getattr(span, "trace_id", "") or "",
                "span_id": getattr(span, "span_id", None),
                "parent_span_id": getattr(span, "parent_id", None),
                "agent_id": self._agent_id,
                "kind": kind,
                "target": target,
                **extra,
            }
        )

    def on_span_end(self, span: Span[Any]) -> None:
        if self._sink is None:
            return
        kind, target, extra = _classify_span(span)
        latency_ms = _span_latency_ms(span)
        self._enqueue_safe(
            {
                "event_type": f"openai_agents_{kind}_end",
                "request_id": getattr(span, "trace_id", "") or "",
                "span_id": getattr(span, "span_id", None),
                "parent_span_id": getattr(span, "parent_id", None),
                "agent_id": self._agent_id,
                "kind": kind,
                "target": target,
                "latency_ms": latency_ms,
                **extra,
            }
        )

    def shutdown(self) -> None:
        # The sink owns its own shutdown via the global context teardown
        # in :func:`checkrd.shutdown`. This method is a no-op so the
        # tracing processor's lifecycle does not race with other Checkrd
        # consumers (transports, instrumentors) that share the same sink.
        return None

    def force_flush(self) -> None:
        if self._sink is None:
            return
        flush = getattr(self._sink, "flush", None)
        if callable(flush):
            try:
                flush()
            except Exception:
                self._logger.warning(
                    "checkrd: openai-agents force_flush failed",
                    exc_info=True,
                )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _enqueue_safe(self, event: dict[str, Any]) -> None:
        try:
            self._sink.enqueue(event)  # type: ignore[union-attr]
        except Exception:
            self._logger.warning(
                "checkrd: openai-agents telemetry enqueue failed",
                exc_info=True,
            )


# ----------------------------------------------------------------------
# Guardrails (enforcement)
# ----------------------------------------------------------------------


class _CheckrdGuardrailBase:
    """Shared evaluation logic for input and output guardrails.

    Guardrails in the Agents SDK are coroutine functions that return a
    :class:`GuardrailFunctionOutput`. Setting ``tripwire_triggered=True``
    aborts the agent run with :class:`GuardrailTripwireTriggered`.
    Checkrd surfaces the WASM engine's deny decision as a tripwire,
    with the deny reason on ``output_info`` for the agent's error log.
    """

    def __init__(
        self,
        *,
        engine: WasmEngine,
        agent_id: str,
        sink: Optional[TelemetrySink] = None,
        enforce: bool = True,
        dashboard_url: Optional[str] = None,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self._engine = engine
        self._agent_id = agent_id
        self._sink = sink
        self._enforce = enforce
        self._dashboard_url = dashboard_url or ""
        self._logger = logger_ or logger

    def _evaluate(
        self,
        *,
        kind: str,
        target: str,
        body_obj: Any,
    ) -> EvalResult:
        """Evaluate a synthetic request through the WASM core."""
        url = f"https://{_AUTHORITY}/{kind}/{target}"
        body_json = _safe_json(body_obj)
        now = datetime.now(timezone.utc)
        return self._engine.evaluate(
            request_id="",  # engine mints a uuid when blank
            method="POST",
            url=url,
            headers=[
                ("x-openai-agents-kind", kind),
                ("x-openai-agents-target", target),
            ],
            body=body_json,
            timestamp=now.isoformat(),
            timestamp_ms=int(now.timestamp() * 1000),
        )

    def _build_output(
        self,
        *,
        result: EvalResult,
        kind: str,
        target: str,
    ) -> "GuardrailFunctionOutput":
        if result.allowed:
            return GuardrailFunctionOutput(
                output_info={
                    "checkrd_request_id": result.request_id,
                    "kind": kind,
                    "target": target,
                },
                tripwire_triggered=False,
            )
        # Denied. Emit telemetry and either tripwire (enforce mode) or
        # log + allow (observation mode).
        if self._sink is not None:
            try:
                self._sink.enqueue(
                    {
                        "event_type": f"openai_agents_{kind}_denied",
                        "request_id": result.request_id,
                        "agent_id": self._agent_id,
                        "kind": kind,
                        "target": target,
                        "deny_reason": result.deny_reason,
                    }
                )
            except Exception:
                self._logger.warning(
                    "checkrd: openai-agents deny telemetry enqueue failed",
                    exc_info=True,
                )

        if not self._enforce:
            self._logger.warning(
                "checkrd: openai-agents %s %s denied (observation mode): %s",
                kind,
                target,
                result.deny_reason,
            )
            return GuardrailFunctionOutput(
                output_info={
                    "checkrd_request_id": result.request_id,
                    "checkrd_observation_only": True,
                    "deny_reason": result.deny_reason,
                },
                tripwire_triggered=False,
            )

        return GuardrailFunctionOutput(
            output_info={
                "checkrd_request_id": result.request_id,
                "deny_reason": result.deny_reason,
                "dashboard_url": self._build_dashboard_url(result.request_id),
            },
            tripwire_triggered=True,
        )

    def _build_dashboard_url(self, request_id: str) -> Optional[str]:
        if not self._dashboard_url:
            return None
        return f"{self._dashboard_url.rstrip('/')}/events/{request_id}"


class CheckrdInputGuardrail(_CheckrdGuardrailBase):
    """Input guardrail factory for the OpenAI Agents SDK.

    Use :meth:`as_guardrail` (or the convenience :meth:`__call__`) to
    obtain an :class:`InputGuardrail` instance ready for the
    ``input_guardrails=[...]`` parameter on :class:`agents.Agent`.

    The guardrail evaluates the agent's input string (the prompt or
    initial message list) through the WASM core BEFORE the LLM is
    called. Useful for blocking known-bad prompts, enforcing input
    length, denying sensitive subjects, etc.

    Example::

        from agents import Agent, Runner
        from checkrd.integrations.openai_agents import CheckrdInputGuardrail

        guard = CheckrdInputGuardrail.from_checkrd(client)
        agent = Agent(name="bot", input_guardrails=[guard.as_guardrail()])
    """

    @classmethod
    def from_checkrd(cls, client: "Checkrd") -> "CheckrdInputGuardrail":
        client._ensure_global_context()
        return cls.from_global()

    @classmethod
    def from_global(cls) -> "CheckrdInputGuardrail":
        ctx: _GlobalContext = get_context()
        return cls(
            engine=ctx.engine,
            agent_id=ctx.settings.agent_id,
            sink=ctx.sink,
            enforce=ctx.enforce,
            dashboard_url=ctx.settings.dashboard_url or "",
        )

    def as_guardrail(self) -> InputGuardrail:
        """Return the actual :class:`InputGuardrail` to register on an agent."""

        async def guardrail_fn(
            ctx: "RunContextWrapper[Any]",
            agent: Any,
            input_data: Any,
        ) -> "GuardrailFunctionOutput":
            target = getattr(agent, "name", None) or "agent"
            result = self._evaluate(
                kind="input",
                target=target,
                body_obj={"input": _coerce_input(input_data)},
            )
            return self._build_output(result=result, kind="input", target=target)

        return InputGuardrail(guardrail_function=guardrail_fn)


class CheckrdOutputGuardrail(_CheckrdGuardrailBase):
    """Output guardrail factory for the OpenAI Agents SDK.

    Same shape as :class:`CheckrdInputGuardrail`, but evaluates the
    agent's final output. Use this when output content needs to be
    policy-filtered (e.g., redacting PII, blocking refusal-bypass
    attempts, enforcing schema).

    Example::

        agent = Agent(
            name="bot",
            output_guardrails=[CheckrdOutputGuardrail.from_checkrd(client).as_guardrail()],
        )
    """

    @classmethod
    def from_checkrd(cls, client: "Checkrd") -> "CheckrdOutputGuardrail":
        client._ensure_global_context()
        return cls.from_global()

    @classmethod
    def from_global(cls) -> "CheckrdOutputGuardrail":
        ctx: _GlobalContext = get_context()
        return cls(
            engine=ctx.engine,
            agent_id=ctx.settings.agent_id,
            sink=ctx.sink,
            enforce=ctx.enforce,
            dashboard_url=ctx.settings.dashboard_url or "",
        )

    def as_guardrail(self) -> OutputGuardrail:
        async def guardrail_fn(
            ctx: "RunContextWrapper[Any]",
            agent: Any,
            output: Any,
        ) -> "GuardrailFunctionOutput":
            target = getattr(agent, "name", None) or "agent"
            result = self._evaluate(
                kind="output",
                target=target,
                body_obj={"output": _coerce_input(output)},
            )
            return self._build_output(result=result, kind="output", target=target)

        return OutputGuardrail(guardrail_function=guardrail_fn)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _classify_span(span: Span[Any]) -> tuple[str, str, dict[str, Any]]:
    """Map a span to (kind, target, extra-fields).

    The Agents SDK exposes span-type information via ``span.span_data``.
    Common subclasses include ``AgentSpanData``, ``FunctionSpanData``,
    ``GenerationSpanData``, ``HandoffSpanData``, ``ResponseSpanData``,
    ``GuardrailSpanData``, ``MCPListToolsSpanData``, ``TurnSpanData``.

    We probe duck-typed attributes rather than ``isinstance(span_data,
    GenerationSpanData)`` because the SpanData subclass list evolves
    across minor versions; duck-typing keeps Checkrd portable.
    """
    span_data = getattr(span, "span_data", None)
    type_name = type(span_data).__name__ if span_data is not None else "Span"
    extra: dict[str, Any] = {}

    # Generation: model + token usage
    if hasattr(span_data, "model"):
        target = str(getattr(span_data, "model", "unknown"))
        usage = getattr(span_data, "usage", None)
        if isinstance(usage, dict):
            extra["input_tokens"] = usage.get("input_tokens") or usage.get("prompt_tokens")
            extra["output_tokens"] = usage.get("output_tokens") or usage.get("completion_tokens")
        return "generation", target, extra

    # Function / tool call: name
    if hasattr(span_data, "name"):
        target = str(getattr(span_data, "name", "unknown"))
        if "Function" in type_name or "Tool" in type_name:
            return "function", target, extra
        if "Handoff" in type_name:
            return "handoff", target, extra
        if "Agent" in type_name:
            return "agent", target, extra
        return type_name.replace("SpanData", "").lower() or "span", target, extra

    # Guardrail
    if "Guardrail" in type_name:
        return "guardrail", str(getattr(span_data, "tripwire_triggered", "")), extra

    return type_name.replace("SpanData", "").lower() or "span", "", extra


def _span_latency_ms(span: Span[Any]) -> Optional[float]:
    started = getattr(span, "started_at", None)
    ended = getattr(span, "ended_at", None)
    if not started or not ended:
        return None
    try:
        s = datetime.fromisoformat(started.replace("Z", "+00:00"))
        e = datetime.fromisoformat(ended.replace("Z", "+00:00"))
        return (e - s).total_seconds() * 1000
    except (ValueError, TypeError):
        return None


def _coerce_input(value: Any) -> Any:
    """Coerce guardrail input/output to a JSON-serializable shape."""
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return dump()
        except Exception:
            pass
    if isinstance(value, (str, int, float, bool, type(None), list, dict)):
        return value
    return str(value)


def _safe_json(obj: Any) -> str:
    try:
        return json.dumps(
            obj,
            default=lambda o: getattr(o, "model_dump", lambda: str(o))(),
        )
    except (TypeError, ValueError):
        return json.dumps({"_repr": str(obj)})


__all__ = [
    "CheckrdTracingProcessor",
    "CheckrdInputGuardrail",
    "CheckrdOutputGuardrail",
]
