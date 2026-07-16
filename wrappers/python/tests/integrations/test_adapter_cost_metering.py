"""Cross-SDK money-coverage gap (Stream M / M-12): adapter cost metering.

The JS SDK settles cost in its common batcher funnel, so it prices framework-
adapter LLM calls (LangChain / OpenAI-Agents). The Python SDK historically
settled ONLY in the httpx transport (``transports/_httpx._settle_cost_into``,
now ``checkrd._genai_body.settle_cost_into``) — which the adapters BYPASS by
calling ``sink.enqueue(event)`` directly. The result was a cross-SDK divergence:
adapter LLM spend was billed at $0 in Python but counted in JS.

These tests prove the gap is closed:

- The adapter settles a flat-gen_ai-key usage event when ``cost_metering=True``
  and a bundle is installed (``get_active_pricing_version() > 0``) — the
  ``cost_usd_micros`` / ``pricing_status`` fields land on the enqueued event.
- Default-off: with ``cost_metering=False`` the adapter does NOT settle.
- Thread safety: the settle runs SYNCHRONOUSLY on the adapter's calling thread
  (the same thread that drives ``engine.evaluate``), never the batcher thread —
  required because each ``WasmEngine`` is a non-thread-safe wasmtime Store.

A fake engine (not the real WASM core) is used so the assertions are hermetic
and fast: ``settle_usage`` returns a known ``SettleResult`` and records the
calling-thread identity; ``get_active_pricing_version`` is configurable.
``tests/test_pricing.py`` covers the real-WASM settle arithmetic end-to-end.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional
from unittest.mock import patch
from uuid import uuid4

import pytest

from checkrd.testing import MockEngine

# Known SettleResult the fake engine returns — the figures don't matter beyond
# being recognizable on the enqueued event (real arithmetic lives in
# test_pricing.py against the WASM core).
_SETTLE_RESULT: Dict[str, Any] = {
    "cost_usd_micros": 10_500,
    "currency": "USD",
    "pricing_bundle_version": 7,
    "pricing_status": "priced",
    "overflow": False,
    "sku_id": "anthropic-claude-sonnet",
}


class _FakeMeteringEngine(MockEngine):
    """A ``MockEngine`` that also implements the pricing FFI seam.

    Inherits ``evaluate`` (so the adapters' ``_gate`` works unchanged) and adds
    the two pricing methods the settle helper calls. Records every
    ``settle_usage`` invocation — the request_id, the parsed usage, and the
    ident of the calling thread — so tests can assert on the wire shape AND the
    thread-safety contract.
    """

    def __init__(
        self,
        *,
        default: str = "allow",
        active_pricing_version: int = 7,
    ) -> None:
        super().__init__(default=default)
        self._active_pricing_version = active_pricing_version
        self.settle_calls: List[tuple[str, Dict[str, Any]]] = []
        self.settle_thread_idents: List[int] = []
        self.version_calls = 0

    def get_active_pricing_version(self) -> int:
        self.version_calls += 1
        return self._active_pricing_version

    def settle_usage(self, request_id: str, usage_json: str) -> Dict[str, Any]:
        import json

        self.settle_thread_idents.append(threading.get_ident())
        self.settle_calls.append((request_id, json.loads(usage_json)))
        return dict(_SETTLE_RESULT)


class _ListSink:
    """Sink that captures enqueued events for assertions."""

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def enqueue(self, event: Dict[str, Any]) -> None:
        self.events.append(event)

    def stop(self) -> None:
        pass


# ======================================================================
# LangChain
# ======================================================================


class TestLangChainCostMetering:
    """LangChain callback handler settles adapter LLM spend (the gap)."""

    def setup_method(self) -> None:
        pytest.importorskip("langchain_core")

    def _make_handler(
        self,
        *,
        cost_metering: bool,
        active_pricing_version: int = 7,
    ) -> tuple[Any, _ListSink, _FakeMeteringEngine]:
        from checkrd.integrations.langchain import CheckrdCallbackHandler

        engine = _FakeMeteringEngine(
            default="allow",
            active_pricing_version=active_pricing_version,
        )
        sink = _ListSink()
        handler = CheckrdCallbackHandler(
            engine=engine,
            agent_id="test-agent",
            sink=sink,
            enforce=True,
            cost_metering=cost_metering,
        )
        return handler, sink, engine

    def _drive_llm_step(self, handler: Any) -> None:
        """Run an llm start→end pair that yields a flat-gen_ai-key usage event."""
        from langchain_core.outputs import Generation, LLMResult

        run_id = uuid4()
        handler.on_llm_start(
            serialized={"kwargs": {"model": "claude-sonnet-4-5"}},
            prompts=["hello"],
            run_id=run_id,
        )
        result = LLMResult(
            generations=[[Generation(text="hi")]],
            llm_output={"token_usage": {"prompt_tokens": 1000, "completion_tokens": 500}},
        )
        handler.on_llm_end(result, run_id=run_id)

    def test_llm_step_stamps_cost_when_metering_on(self) -> None:
        """THE GAP: an adapter LLM step with flat gen_ai keys gets settled and
        the cost fields stamped on the enqueued event."""
        handler, sink, engine = self._make_handler(cost_metering=True)
        self._drive_llm_step(handler)

        assert len(sink.events) == 1
        event = sink.events[0]
        # Cost fields present — proving the adapter settled (it used to bill $0).
        assert event["cost_usd_micros"] == 10_500
        assert event["currency"] == "USD"
        assert event["pricing_bundle_version"] == 7
        assert event["pricing_status"] == "priced"

        # The UsageInput built from the FLAT adapter keys reflects the call.
        assert len(engine.settle_calls) == 1
        request_id, usage = engine.settle_calls[0]
        assert usage["model"] == "claude-sonnet-4-5"
        assert usage["input_tokens"] == 1000
        assert usage["output_tokens"] == 500
        # run_id doubles as the request_id (same convention as _gate / evaluate).
        assert request_id == event["request_id"]

    def test_default_off_does_not_settle(self) -> None:
        """cost_metering=False (the default): NO settle, NO cost fields. Proves
        the money path is opt-in and the prior behavior is preserved."""
        handler, sink, engine = self._make_handler(cost_metering=False)
        self._drive_llm_step(handler)

        assert len(sink.events) == 1
        event = sink.events[0]
        assert "cost_usd_micros" not in event
        assert "pricing_status" not in event
        # The pricing FFI is never touched when metering is off.
        assert engine.settle_calls == []
        assert engine.version_calls == 0

    def test_no_bundle_installed_leaves_fields_unset(self) -> None:
        """Metering on but version 0 (no price table): the cheap version gate
        short-circuits before settle — cost fields stay unset."""
        handler, sink, engine = self._make_handler(
            cost_metering=True, active_pricing_version=0
        )
        self._drive_llm_step(handler)

        event = sink.events[0]
        assert "cost_usd_micros" not in event
        assert engine.settle_calls == []  # version 0 → never settle
        assert engine.version_calls >= 1  # but the gate WAS checked

    def test_settle_runs_on_calling_thread_not_batcher(self) -> None:
        """Thread-safety contract: the settle is invoked SYNCHRONOUSLY inside the
        adapter callback on the calling thread — never deferred to a background
        thread. Each WasmEngine is a non-thread-safe wasmtime Store, so the
        engine must only ever be touched from the thread that owns it."""
        handler, _sink, engine = self._make_handler(cost_metering=True)
        self._drive_llm_step(handler)

        assert len(engine.settle_thread_idents) == 1
        # Settle happened on THIS test's thread — i.e. synchronously within the
        # on_llm_end callback, not on any batcher/worker thread.
        assert engine.settle_thread_idents[0] == threading.get_ident()

    def test_tool_step_settle_is_noop(self) -> None:
        """A tool step carries no token usage, so even with metering on the
        settle is a no-op (build_usage_input returns None) — no cost fields,
        no spurious unpriced_model."""
        handler, sink, engine = self._make_handler(cost_metering=True)
        run_id = uuid4()
        handler.on_tool_start(
            serialized={"name": "search_database"},
            input_str="select 1",
            run_id=run_id,
        )
        handler.on_tool_end("ok", run_id=run_id)

        event = sink.events[0]
        assert "cost_usd_micros" not in event
        assert engine.settle_calls == []  # no tokens → nothing to price


# ======================================================================
# OpenAI Agents SDK
# ======================================================================


class TestOpenAIAgentsCostMetering:
    """OpenAI Agents tracing processor settles generation-span spend (the gap)."""

    def setup_method(self) -> None:
        pytest.importorskip("agents")

    def _make_processor(
        self,
        *,
        cost_metering: bool,
        active_pricing_version: int = 7,
    ) -> tuple[Any, _ListSink, _FakeMeteringEngine]:
        from checkrd.integrations.openai_agents import CheckrdTracingProcessor

        engine = _FakeMeteringEngine(
            default="allow",
            active_pricing_version=active_pricing_version,
        )
        sink = _ListSink()
        proc = CheckrdTracingProcessor(
            engine=engine,
            agent_id="test-agent",
            sink=sink,
            cost_metering=cost_metering,
        )
        return proc, sink, engine

    @staticmethod
    def _generation_span() -> Any:
        class _FakeSpanData:
            model = "claude-sonnet-4-5"
            usage = {"input_tokens": 1000, "output_tokens": 500}

        class _FakeSpan:
            # 32/16 hex IDs so the wire-validator keeps trace_id/span_id.
            trace_id = "0" * 31 + "1"
            span_id = "0" * 15 + "2"
            parent_id = None
            started_at = "2026-04-24T00:00:00+00:00"
            ended_at = "2026-04-24T00:00:01+00:00"
            span_data = _FakeSpanData()

        return _FakeSpan()

    def test_generation_span_stamps_cost_when_metering_on(self) -> None:
        """THE GAP: a generation span's token usage is settled and the cost
        fields are stamped on the enqueued event."""
        proc, sink, engine = self._make_processor(cost_metering=True)
        proc.on_span_end(self._generation_span())

        assert len(sink.events) == 1
        event = sink.events[0]
        assert event["cost_usd_micros"] == 10_500
        assert event["currency"] == "USD"
        assert event["pricing_bundle_version"] == 7
        assert event["pricing_status"] == "priced"

        assert len(engine.settle_calls) == 1
        request_id, usage = engine.settle_calls[0]
        assert usage["model"] == "claude-sonnet-4-5"
        assert usage["input_tokens"] == 1000
        assert usage["output_tokens"] == 500
        # The span's correlation id is reused as the settle request_id.
        assert request_id == event["request_id"]

    def test_default_off_does_not_settle(self) -> None:
        """cost_metering=False: NO settle, NO cost fields. Default-off proof."""
        proc, sink, engine = self._make_processor(cost_metering=False)
        proc.on_span_end(self._generation_span())

        event = sink.events[0]
        assert "cost_usd_micros" not in event
        assert "pricing_status" not in event
        assert engine.settle_calls == []
        assert engine.version_calls == 0

    def test_no_bundle_installed_leaves_fields_unset(self) -> None:
        """Metering on but version 0: gate short-circuits, cost fields unset."""
        proc, sink, engine = self._make_processor(
            cost_metering=True, active_pricing_version=0
        )
        proc.on_span_end(self._generation_span())

        event = sink.events[0]
        assert "cost_usd_micros" not in event
        assert engine.settle_calls == []
        assert engine.version_calls >= 1

    def test_settle_runs_on_calling_thread_not_batcher(self) -> None:
        """Thread-safety: the Agents SDK invokes on_span_end synchronously in the
        run loop, so the settle must run on THAT thread — asserted here as the
        test thread. The wasmtime Store is non-thread-safe."""
        proc, _sink, engine = self._make_processor(cost_metering=True)
        proc.on_span_end(self._generation_span())

        assert len(engine.settle_thread_idents) == 1
        assert engine.settle_thread_idents[0] == threading.get_ident()

    def test_non_generation_span_settle_is_noop(self) -> None:
        """A function/tool span has no token usage — settle is a no-op."""
        proc, sink, engine = self._make_processor(cost_metering=True)

        class _FuncSpanData:
            name = "lookup"

        class _FuncSpan:
            trace_id = "0" * 31 + "1"
            span_id = "0" * 15 + "2"
            parent_id = None
            started_at = "2026-04-24T00:00:00+00:00"
            ended_at = "2026-04-24T00:00:01+00:00"
            span_data = _FuncSpanData()

        proc.on_span_end(_FuncSpan())
        event = sink.events[0]
        assert "cost_usd_micros" not in event
        assert engine.settle_calls == []


# ======================================================================
# from_global threads the cost_metering flag off the settings
# ======================================================================


class TestFromGlobalThreadsFlag:
    """The ``from_global`` / ``from_checkrd`` constructors must propagate
    ``settings.cost_metering`` so a globally-initialized handler meters."""

    def test_langchain_from_global_reads_settings_flag(self) -> None:
        pytest.importorskip("langchain_core")
        from checkrd.integrations.langchain import CheckrdCallbackHandler

        for flag in (True, False):
            ctx = _fake_global_context(cost_metering=flag)
            handler = _from_global_with_ctx(CheckrdCallbackHandler, ctx)
            assert handler._cost_metering is flag

    def test_openai_agents_from_global_reads_settings_flag(self) -> None:
        pytest.importorskip("agents")
        from checkrd.integrations.openai_agents import CheckrdTracingProcessor

        for flag in (True, False):
            ctx = _fake_global_context(cost_metering=flag)
            proc = _from_global_with_ctx(CheckrdTracingProcessor, ctx)
            assert proc._cost_metering is flag


def _fake_global_context(*, cost_metering: bool) -> Any:
    """A duck-typed _GlobalContext carrying just what from_global reads."""

    class _Settings:
        agent_id = "test-agent"
        dashboard_url = ""

        def __init__(self, cm: bool) -> None:
            self.cost_metering = cm

    class _Ctx:
        engine = MockEngine(default="allow")
        sink: Optional[Any] = None
        enforce = True

        def __init__(self, cm: bool) -> None:
            self.settings = _Settings(cm)

    return _Ctx(cost_metering)


def _from_global_with_ctx(cls: Any, ctx: Any) -> Any:
    """Invoke ``cls.from_global()`` with ``get_context`` patched to ``ctx``.

    Each integration module imported ``get_context`` by name, so patch it on the
    module that owns ``cls`` (``cls.__module__``) rather than on ``checkrd._state``.
    """
    with patch(f"{cls.__module__}.get_context", return_value=ctx):
        return cls.from_global()
