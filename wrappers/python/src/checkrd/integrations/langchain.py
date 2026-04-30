"""LangChain / LangGraph integration for Checkrd.

LangChain's industry-standard extension point is the callback handler
protocol — :class:`langchain_core.callbacks.base.BaseCallbackHandler`.
Every Runnable in the LangChain ecosystem (chains, LLMs, chat models,
tools, retrievers, agents, LangGraph nodes) emits ``on_*_start`` /
``on_*_end`` / ``on_*_error`` events through these handlers.

This module ships :class:`CheckrdCallbackHandler` which subclasses
:class:`BaseCallbackHandler`. The same instance works for both
``.invoke()`` and ``.ainvoke()`` because LangChain's dispatcher
automatically runs sync handlers via :func:`asyncio.to_thread` for
async chains. (Langfuse v3, OpenLLMetry, and Logfire all use this
pattern.) The WASM engine's ``evaluate()`` is a sub-millisecond synchronous
call, so the thread-pool overhead is negligible compared to wrapping
every method as ``async def``.

Usage::

    from checkrd import Checkrd
    from checkrd.integrations.langchain import CheckrdCallbackHandler
    from langchain_openai import ChatOpenAI

    with Checkrd() as client:
        handler = CheckrdCallbackHandler.from_checkrd(client)
        llm = ChatOpenAI(callbacks=[handler])
        # or, per-call:
        chain.invoke(input, config={"callbacks": [handler]})

The handler enforces policy on every LLM call, tool call, retriever
call, and chain invocation:

- **Pre-call**: builds a synthetic HTTP request from the LangChain
  event metadata (model name, prompt, tool input, retriever query) and
  evaluates it through the WASM core. If the engine denies and
  ``enforce=True``, raises :class:`CheckrdPolicyDenied` from the
  callback — LangChain propagates the exception to the caller and
  fires the matching ``on_*_error`` event.
- **Post-call**: emits a structured telemetry event to the sink with
  latency, finish reason, token counts (when available), and
  ``run_id`` / ``parent_run_id`` for span tree reconstruction.

Correlation: every LangChain event carries ``run_id: UUID`` (the span
ID) and ``parent_run_id: UUID | None``. The handler keeps an in-flight
map keyed by ``run_id`` so ``_end`` events can compute latency relative
to the matching ``_start`` event without relying on framework state.
``run_id`` is also used as the request_id for the WASM engine, so a
single LangChain run produces a single Checkrd telemetry event chain.

Why this is the right surface (not patching ``langchain.OpenAI``):

  - LangChain has many LLM backends; patching each is a maintenance
    treadmill.
  - LangGraph adds tool / retriever / sub-agent events that vendor
    instrumentation cannot see.
  - The callback handler is part of LangChain's stable public API
    (``langchain-core >= 0.3``); the internal architecture changes
    quarterly, but the handler contract is the explicit extension point.
  - Anthropic / OpenAI / etc. instrumentation still works alongside
    this handler — they sit at different layers (HTTP vs. semantic),
    and double-recording is filtered by the WASM core's request_id.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional, Sequence
from uuid import UUID

# Hard import: this module requires langchain-core. Users opt in by
# importing this module — if they don't have LangChain installed, they
# never reach this import, so the ImportError surfaces at the right
# layer (their explicit ``from checkrd.integrations.langchain import ...``).
from langchain_core.callbacks.base import BaseCallbackHandler

from checkrd._state import _GlobalContext, get_context
from checkrd.engine import EvalResult, WasmEngine
from checkrd.exceptions import CheckrdPolicyDenied
from checkrd.sinks import TelemetrySink

if TYPE_CHECKING:
    from langchain_core.agents import AgentAction, AgentFinish
    from langchain_core.documents import Document
    from langchain_core.messages import BaseMessage
    from langchain_core.outputs import LLMResult

    from checkrd.client import Checkrd

logger = logging.getLogger("checkrd.integrations.langchain")


# Synthetic URL authority for LangChain events. Policy authors match
# against ``https://langchain.local/llm/...``, ``.../tool/...`` etc. so
# rules can target LangChain semantics without overlap with vendor
# domains (api.openai.com etc.).
_LANGCHAIN_AUTHORITY = "langchain.local"


class CheckrdCallbackHandler(BaseCallbackHandler):
    """LangChain callback handler that enforces Checkrd policy.

    Args:
        engine: A :class:`WasmEngine` instance. Typically obtained from
            :class:`checkrd.Checkrd` or :func:`checkrd.init`.
        agent_id: The agent identifier used for telemetry correlation.
        sink: Optional :class:`TelemetrySink`. When ``None``, no
            telemetry is emitted (denial decisions still apply).
        enforce: When ``True`` (the default), denied calls raise
            :class:`CheckrdPolicyDenied`. When ``False``, denials are
            logged and the call proceeds — observation mode.
        dashboard_url: Base URL for dashboard deep links in the deny
            error message. Optional.
        logger_: Optional :class:`logging.Logger`. Defaults to the
            module logger.

    Thread safety: the in-flight map is protected by a
    :class:`threading.Lock`. The handler is safe to register on a
    process-wide LLM and reuse across threads / asyncio tasks.

    Async chains: LangChain's dispatcher automatically wraps sync
    callbacks for async chains via :func:`asyncio.to_thread`. The
    WASM ``evaluate()`` call is sub-ms so this is the right tradeoff
    over duplicating every method as ``async def``.

    Example::

        from checkrd import Checkrd
        from checkrd.integrations.langchain import CheckrdCallbackHandler
        from langchain_openai import ChatOpenAI

        with Checkrd(policy="policy.yaml") as client:
            handler = CheckrdCallbackHandler.from_checkrd(client)
            llm = ChatOpenAI(callbacks=[handler])
            llm.invoke("Tell me a joke")
    """

    # ``raise_error = True`` tells LangChain to propagate exceptions
    # raised from this handler instead of swallowing them. Without it,
    # ``CheckrdPolicyDenied`` would be silently dropped — the request
    # would proceed despite the deny decision.
    raise_error: bool = True

    # ``run_inline = True`` ensures the handler is invoked in the same
    # thread/coroutine as the chain (rather than dispatched to a
    # background pool), so ``CheckrdPolicyDenied`` is raised on the
    # caller's stack and latency measurements are accurate.
    run_inline: bool = True

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
        super().__init__()
        self._engine = engine
        self._agent_id = agent_id
        self._sink = sink
        self._enforce = enforce
        self._dashboard_url = dashboard_url or ""
        self._logger = logger_ or logger

        # In-flight map: run_id -> (start_time_ns, kind, target).
        # Used to compute latency and emit the post-call telemetry event
        # without re-deriving the synthetic URL.
        self._in_flight: dict[UUID, tuple[int, str, str]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Convenience constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_checkrd(cls, client: "Checkrd") -> "CheckrdCallbackHandler":
        """Build a handler from a :class:`Checkrd` client.

        Ensures the client has set up the global context, then pulls
        the engine, agent_id, sink, and enforce mode from it. The
        handler matches every other Checkrd instrumentor configured
        on the same client.

        Raises :class:`RuntimeError` if the global context is degraded
        (engine failed to load).
        """
        # Lazy import to avoid a cycle.
        client._ensure_global_context()
        return cls.from_global()

    @classmethod
    def from_global(cls) -> "CheckrdCallbackHandler":
        """Build a handler from the global :func:`checkrd.init` context.

        Useful when the application initializes Checkrd once at startup
        with ``checkrd.init(...)`` and wants the handler to inherit that
        configuration. Raises :class:`RuntimeError` if no context exists.
        """
        ctx: _GlobalContext = get_context()
        return cls(
            engine=ctx.engine,
            agent_id=ctx.settings.agent_id,
            sink=ctx.sink,
            enforce=ctx.enforce,
            dashboard_url=ctx.settings.dashboard_url or "",
        )

    # ------------------------------------------------------------------
    # Internal: gate + emit
    # ------------------------------------------------------------------

    def _gate(
        self,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID],
        kind: str,
        target: str,
        body: Any,
    ) -> EvalResult:
        """Evaluate a synthetic request and raise on deny.

        Args:
            run_id: LangChain's per-event UUID (also the Checkrd
                request_id, so a single LangChain run produces a single
                Checkrd telemetry event chain).
            parent_run_id: The parent UUID, when this event is nested.
            kind: One of ``"llm"``, ``"chat_model"``, ``"tool"``,
                ``"chain"``, ``"retriever"``.
            target: The model name, tool name, retriever id, or chain
                identifier — used as the last URL segment so policy
                authors can match on it.
            body: JSON-serializable payload describing the call. Serialized
                to JSON for the policy engine's body matchers.
        """
        request_id = str(run_id)
        url = f"https://{_LANGCHAIN_AUTHORITY}/{kind}/{target}"
        body_json = _json_default(body)
        now = datetime.now(timezone.utc)

        result = self._engine.evaluate(
            request_id=request_id,
            method="POST",
            url=url,
            headers=[
                ("x-langchain-kind", kind),
                ("x-langchain-target", target),
                ("x-langchain-run-id", str(run_id)),
                (
                    "x-langchain-parent-run-id",
                    str(parent_run_id) if parent_run_id else "",
                ),
            ],
            body=body_json,
            timestamp=now.isoformat(),
            timestamp_ms=int(now.timestamp() * 1000),
            trace_id=str(parent_run_id) if parent_run_id else str(run_id),
            span_id=str(run_id),
            parent_span_id=str(parent_run_id) if parent_run_id else None,
        )

        with self._lock:
            self._in_flight[run_id] = (time.perf_counter_ns(), kind, target)

        if not result.allowed and self._enforce:
            raise CheckrdPolicyDenied(
                reason=result.deny_reason or "policy denied",
                request_id=result.request_id,
                url=url,
                dashboard_url=self._build_dashboard_url(result.request_id),
            )
        if not result.allowed:
            self._logger.warning(
                "checkrd: langchain %s %s denied (observation mode): %s",
                kind,
                target,
                result.deny_reason,
            )

        return result

    def _emit(
        self,
        *,
        run_id: UUID,
        outcome: str,
        extra: dict[str, Any],
    ) -> None:
        """Emit a post-call telemetry event to the sink.

        ``outcome`` is ``"ok"`` or ``"error"``. Latency is computed from
        the in-flight map; the entry is removed after emit.
        """
        if self._sink is None:
            return
        with self._lock:
            entry = self._in_flight.pop(run_id, None)
        if entry is None:
            # _gate was never called for this run_id (e.g. user only
            # registered the handler for a subset of events, or the
            # _start handler raised). Nothing we can compute latency
            # against — drop.
            return
        start_ns, kind, target = entry
        latency_ms = (time.perf_counter_ns() - start_ns) / 1_000_000
        event: dict[str, Any] = {
            "event_type": f"langchain_{kind}",
            "request_id": str(run_id),
            "agent_id": self._agent_id,
            "latency_ms": latency_ms,
            "kind": kind,
            "target": target,
            "outcome": outcome,
        }
        event.update(extra)
        try:
            self._sink.enqueue(event)
        except Exception:
            # Sinks are best-effort. A failing sink must not crash the
            # chain — observability/proxy SDKs follow PostHog's @no_throw
            # discipline at the integration boundary.
            self._logger.warning(
                "checkrd: telemetry enqueue failed for langchain %s %s",
                kind,
                target,
                exc_info=True,
            )

    def _build_dashboard_url(self, request_id: str) -> Optional[str]:
        if not self._dashboard_url:
            return None
        return f"{self._dashboard_url.rstrip('/')}/events/{request_id}"

    # ------------------------------------------------------------------
    # LLM events (covers chat models too via on_chat_model_start)
    # ------------------------------------------------------------------

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        target = _resolve_model_name(serialized) or "unknown"
        self._gate(
            run_id=run_id,
            parent_run_id=parent_run_id,
            kind="llm",
            target=target,
            body={"prompts": prompts, "tags": tags, "metadata": metadata},
        )

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list["BaseMessage"]],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        target = _resolve_model_name(serialized) or "unknown"
        # Serialize messages defensively — Pydantic v2 messages have
        # ``.model_dump()`` but third-party message subclasses may not.
        # Fall back to ``str(msg)`` per LangChain's documented contract.
        body_messages = [[_message_to_dict(msg) for msg in run] for run in messages]
        self._gate(
            run_id=run_id,
            parent_run_id=parent_run_id,
            kind="chat_model",
            target=target,
            body={"messages": body_messages, "tags": tags, "metadata": metadata},
        )

    def on_llm_end(
        self,
        response: "LLMResult",
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        usage = _extract_token_usage(response)
        self._emit(
            run_id=run_id,
            outcome="ok",
            extra={
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "total_tokens": usage.get("total_tokens"),
                "finish_reason": _extract_finish_reason(response),
            },
        )

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        self._emit(
            run_id=run_id,
            outcome="error",
            extra={"error": type(error).__name__, "error_message": str(error)},
        )

    # ------------------------------------------------------------------
    # Tool events
    # ------------------------------------------------------------------

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        inputs: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        target = (serialized or {}).get("name") or "unknown"
        self._gate(
            run_id=run_id,
            parent_run_id=parent_run_id,
            kind="tool",
            target=target,
            body={"input_str": input_str, "inputs": inputs, "tags": tags},
        )

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        self._emit(
            run_id=run_id,
            outcome="ok",
            extra={"output_preview": _preview(output)},
        )

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        self._emit(
            run_id=run_id,
            outcome="error",
            extra={"error": type(error).__name__, "error_message": str(error)},
        )

    # ------------------------------------------------------------------
    # Retriever events
    # ------------------------------------------------------------------

    def on_retriever_start(
        self,
        serialized: dict[str, Any],
        query: str,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        target = (serialized or {}).get("name") or "retriever"
        self._gate(
            run_id=run_id,
            parent_run_id=parent_run_id,
            kind="retriever",
            target=target,
            body={"query": query, "tags": tags, "metadata": metadata},
        )

    def on_retriever_end(
        self,
        documents: Sequence["Document"],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        self._emit(
            run_id=run_id,
            outcome="ok",
            extra={"document_count": len(documents)},
        )

    def on_retriever_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        self._emit(
            run_id=run_id,
            outcome="error",
            extra={"error": type(error).__name__, "error_message": str(error)},
        )

    # ------------------------------------------------------------------
    # Chain events (LangChain's most generic primitive)
    #
    # We evaluate them with the chain name as target so operators can
    # write rules like "deny chain:dangerous_db_query".
    # ------------------------------------------------------------------

    def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        target = (serialized or {}).get("name") or kwargs.get("name") or "chain"
        self._gate(
            run_id=run_id,
            parent_run_id=parent_run_id,
            kind="chain",
            target=target,
            body={"inputs": inputs, "tags": tags, "metadata": metadata},
        )

    def on_chain_end(
        self,
        outputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        self._emit(
            run_id=run_id,
            outcome="ok",
            extra={"output_keys": sorted(outputs.keys()) if isinstance(outputs, dict) else []},
        )

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        self._emit(
            run_id=run_id,
            outcome="error",
            extra={"error": type(error).__name__, "error_message": str(error)},
        )

    # ------------------------------------------------------------------
    # Agent events
    #
    # Agent actions/finishes are emitted by LangChain agents (and
    # LangGraph nodes that map to AgentExecutor). They piggyback on
    # the parent chain's run_id, so we emit telemetry without gating
    # (the underlying tool call gets gated separately via on_tool_start).
    # ------------------------------------------------------------------

    def on_agent_action(
        self,
        action: "AgentAction",
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        if self._sink is None:
            return
        try:
            self._sink.enqueue(
                {
                    "event_type": "langchain_agent_action",
                    "request_id": str(run_id),
                    "agent_id": self._agent_id,
                    "tool": getattr(action, "tool", None),
                    "tool_input": _preview(getattr(action, "tool_input", None)),
                    "log": _preview(getattr(action, "log", None)),
                }
            )
        except Exception:
            self._logger.warning(
                "checkrd: telemetry enqueue failed for agent_action",
                exc_info=True,
            )

    def on_agent_finish(
        self,
        finish: "AgentFinish",
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        if self._sink is None:
            return
        try:
            return_values = getattr(finish, "return_values", None)
            self._sink.enqueue(
                {
                    "event_type": "langchain_agent_finish",
                    "request_id": str(run_id),
                    "agent_id": self._agent_id,
                    "return_values_keys": sorted(return_values.keys())
                    if isinstance(return_values, dict)
                    else [],
                }
            )
        except Exception:
            self._logger.warning(
                "checkrd: telemetry enqueue failed for agent_finish",
                exc_info=True,
            )


# ----------------------------------------------------------------------
# Helpers (module-private)
# ----------------------------------------------------------------------


def _resolve_model_name(serialized: Optional[dict[str, Any]]) -> Optional[str]:
    """Pull a model identifier out of LangChain's serialized payload.

    LangChain serializes Runnables with ``serialized["kwargs"]["model"]``
    (or ``"model_name"`` for older chat models). The structure is
    documented but not strictly enforced — we probe several keys and
    return the first hit.
    """
    if not serialized:
        return None
    kwargs = serialized.get("kwargs") or {}
    for key in ("model", "model_name", "deployment_name", "name"):
        v = kwargs.get(key)
        if isinstance(v, str) and v:
            return v
    name = serialized.get("name") or serialized.get("id")
    if isinstance(name, str) and name:
        return name
    return None


def _message_to_dict(msg: Any) -> dict[str, Any]:
    """Convert a :class:`BaseMessage` (or duck) to a JSON-friendly dict.

    Pydantic v2 messages have ``.model_dump()``. Older or custom
    subclasses may not. The fallback uses ``type(msg).__name__`` and
    ``str(msg)`` so we always have something to feed body matchers.
    """
    dump = getattr(msg, "model_dump", None)
    if callable(dump):
        try:
            return dump()  # type: ignore[no-any-return]
        except Exception:
            pass
    return {
        "type": type(msg).__name__,
        "content": getattr(msg, "content", str(msg)),
    }


def _extract_token_usage(response: "LLMResult") -> dict[str, Optional[int]]:
    """Pull token usage from an :class:`LLMResult`.

    LangChain stores usage in ``response.llm_output`` (provider-specific
    keys). We probe both legacy (``prompt_tokens``/``completion_tokens``)
    and new (``input_tokens``/``output_tokens``) shapes. Any field may
    be ``None`` when the provider doesn't report it.
    """
    out: dict[str, Optional[int]] = {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
    }
    llm_output = getattr(response, "llm_output", None) or {}
    if isinstance(llm_output, dict):
        usage = (
            llm_output.get("token_usage")
            or llm_output.get("usage")
            or llm_output.get("usage_metadata")
            or {}
        )
        if isinstance(usage, dict):
            out["input_tokens"] = _coerce_int(
                usage.get("prompt_tokens") or usage.get("input_tokens"),
            )
            out["output_tokens"] = _coerce_int(
                usage.get("completion_tokens") or usage.get("output_tokens"),
            )
            out["total_tokens"] = _coerce_int(usage.get("total_tokens"))
    return out


def _extract_finish_reason(response: "LLMResult") -> Optional[str]:
    """Best-effort extraction of the finish reason from generations."""
    gens = getattr(response, "generations", None) or []
    for gen_list in gens:
        for gen in gen_list:
            info = getattr(gen, "generation_info", None) or {}
            if isinstance(info, dict):
                reason = info.get("finish_reason") or info.get("stop_reason")
                if isinstance(reason, str):
                    return reason
    return None


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_default(obj: Any) -> str:
    """Serialize ``obj`` to JSON, falling back to ``str()`` on un-serializable values."""
    try:
        return json.dumps(
            obj,
            default=lambda o: getattr(o, "model_dump", lambda: str(o))(),
        )
    except (TypeError, ValueError):
        return json.dumps({"_repr": str(obj)})


def _preview(value: Any, *, max_len: int = 256) -> str:
    """Bounded string preview of arbitrary values for telemetry events.

    Telemetry events must stay under the WASM core's body-size budget;
    truncating here prevents one verbose tool from inflating every event.
    """
    s = str(value)
    if len(s) <= max_len:
        return s
    return s[:max_len] + "..."


__all__ = ["CheckrdCallbackHandler"]
