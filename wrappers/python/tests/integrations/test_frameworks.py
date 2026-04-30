"""Smoke tests for the four agent-framework adapters.

Each test is gated on the framework actually being importable —
``pytest.importorskip`` skips the test cleanly when the optional peer
is missing. CI runs the full extras matrix so all four blocks execute;
local dev runs only the ones the contributor has installed.

The tests use :class:`checkrd.testing.MockEngine` instead of the real
WASM engine. ``MockEngine`` is the supported test seam: it implements
the same ``evaluate()`` signature and returns a duck-typed
:class:`EvalResult` so adapters can't tell the difference. This keeps
tests fast (no WASM compilation) and hermetic (no policy file, no
identity key).

The tests focus on the contract of each adapter:

1. **Pre-call gating** — when the engine returns ``allowed=False`` and
   ``enforce=True``, the adapter must surface the deny in the
   framework-native way (raise, tripwire, or block decision).
2. **Allow path** — when allowed, the adapter must delegate to the
   framework's normal flow.
3. **Telemetry** — for each gated event, the sink must receive an
   event with the expected shape (event_type, agent_id, request_id).
4. **Observation mode** — when ``enforce=False``, denies are logged but
   do not abort the call.

Tests do NOT verify framework behavior beyond the integration boundary
— LangChain's chain semantics, the Agents SDK's run loop, etc., are
the framework's responsibility. We verify that Checkrd hooks into them
correctly.
"""

from __future__ import annotations

import asyncio
from typing import Any, List
from uuid import uuid4

import pytest

from checkrd.exceptions import CheckrdPolicyDenied
from checkrd.testing import MockEngine


class _ListSink:
    """Minimal sink that captures enqueued events for assertions."""

    def __init__(self) -> None:
        self.events: List[dict[str, Any]] = []

    def enqueue(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def stop(self) -> None:
        pass


# ======================================================================
# LangChain
# ======================================================================


class TestLangChain:
    """LangChain BaseCallbackHandler adapter."""

    def setup_method(self) -> None:
        pytest.importorskip("langchain_core")

    def _make_handler(
        self, *, default: str = "allow", enforce: bool = True
    ) -> tuple[Any, _ListSink, MockEngine]:
        from checkrd.integrations.langchain import CheckrdCallbackHandler

        engine = MockEngine(default=default)
        sink = _ListSink()
        handler = CheckrdCallbackHandler(
            engine=engine,
            agent_id="test-agent",
            sink=sink,
            enforce=enforce,
        )
        return handler, sink, engine

    def test_on_llm_start_allow_emits_no_deny(self) -> None:
        handler, sink, _ = self._make_handler(default="allow")

        run_id = uuid4()
        # When allowed, on_llm_start does NOT raise. on_llm_end then
        # emits a telemetry event with outcome=ok and matching latency.
        handler.on_llm_start(
            serialized={"kwargs": {"model": "gpt-4o"}},
            prompts=["hello"],
            run_id=run_id,
        )

        # Construct a minimal LLMResult-shaped object for on_llm_end.
        from langchain_core.outputs import Generation, LLMResult

        result = LLMResult(
            generations=[[Generation(text="hi")]],
            llm_output={"token_usage": {"prompt_tokens": 5, "completion_tokens": 2}},
        )
        handler.on_llm_end(result, run_id=run_id)

        assert len(sink.events) == 1
        event = sink.events[0]
        assert event["event_type"] == "langchain_llm"
        assert event["agent_id"] == "test-agent"
        assert event["target"] == "gpt-4o"
        assert event["outcome"] == "ok"
        assert event["input_tokens"] == 5
        assert event["output_tokens"] == 2
        assert event["latency_ms"] >= 0

    def test_on_llm_start_deny_raises_when_enforce(self) -> None:
        handler, _, _ = self._make_handler(default="deny", enforce=True)

        with pytest.raises(CheckrdPolicyDenied) as excinfo:
            handler.on_llm_start(
                serialized={"kwargs": {"model": "gpt-4o"}},
                prompts=["hello"],
                run_id=uuid4(),
            )
        assert "denied by default policy" in excinfo.value.reason
        assert "langchain.local/llm/gpt-4o" in (excinfo.value.url or "")

    def test_on_llm_start_observation_mode_does_not_raise(self) -> None:
        handler, _, _ = self._make_handler(default="deny", enforce=False)

        # Should not raise — observation mode logs and proceeds.
        handler.on_llm_start(
            serialized={"kwargs": {"model": "gpt-4o"}},
            prompts=["hello"],
            run_id=uuid4(),
        )

    def test_on_tool_start_uses_tool_target(self) -> None:
        handler, sink, _ = self._make_handler(default="allow")

        run_id = uuid4()
        handler.on_tool_start(
            serialized={"name": "search_database"},
            input_str="select count(*)",
            run_id=run_id,
        )
        handler.on_tool_end("42", run_id=run_id)

        assert len(sink.events) == 1
        assert sink.events[0]["event_type"] == "langchain_tool"
        assert sink.events[0]["target"] == "search_database"

    def test_on_chain_error_emits_error_outcome(self) -> None:
        handler, sink, _ = self._make_handler(default="allow")

        run_id = uuid4()
        handler.on_chain_start(
            serialized={"name": "my-chain"},
            inputs={"q": "x"},
            run_id=run_id,
        )
        handler.on_chain_error(ValueError("boom"), run_id=run_id)

        assert len(sink.events) == 1
        assert sink.events[0]["outcome"] == "error"
        assert sink.events[0]["error"] == "ValueError"


# ======================================================================
# OpenAI Agents SDK
# ======================================================================


class TestOpenAIAgents:
    """OpenAI Agents SDK TracingProcessor + Guardrail adapter."""

    def setup_method(self) -> None:
        pytest.importorskip("agents")

    def test_input_guardrail_tripwires_on_deny(self) -> None:
        from checkrd.integrations.openai_agents import CheckrdInputGuardrail

        engine = MockEngine(default="deny")
        sink = _ListSink()
        guard = CheckrdInputGuardrail(
            engine=engine,
            agent_id="test-agent",
            sink=sink,
            enforce=True,
        )
        ig = guard.as_guardrail()

        # Build a fake agent-like object with .name.
        class _FakeAgent:
            name = "researcher"

        # The guardrail function is an async callable. Run it.
        out = asyncio.run(
            ig.guardrail_function(None, _FakeAgent(), "do something risky"),
        )
        assert out.tripwire_triggered is True
        assert out.output_info["deny_reason"]
        # Sink received the deny event.
        assert any(e["event_type"] == "openai_agents_input_denied" for e in sink.events)

    def test_input_guardrail_allows_when_allowed(self) -> None:
        from checkrd.integrations.openai_agents import CheckrdInputGuardrail

        engine = MockEngine(default="allow")
        sink = _ListSink()
        guard = CheckrdInputGuardrail(
            engine=engine,
            agent_id="test-agent",
            sink=sink,
            enforce=True,
        )
        ig = guard.as_guardrail()

        class _FakeAgent:
            name = "researcher"

        out = asyncio.run(
            ig.guardrail_function(None, _FakeAgent(), "summarize"),
        )
        assert out.tripwire_triggered is False

    def test_input_guardrail_observation_mode(self) -> None:
        from checkrd.integrations.openai_agents import CheckrdInputGuardrail

        engine = MockEngine(default="deny")
        guard = CheckrdInputGuardrail(
            engine=engine,
            agent_id="test-agent",
            sink=None,
            enforce=False,
        )
        ig = guard.as_guardrail()

        class _FakeAgent:
            name = "researcher"

        out = asyncio.run(
            ig.guardrail_function(None, _FakeAgent(), "x"),
        )
        # Observation mode: never tripwire.
        assert out.tripwire_triggered is False
        assert out.output_info.get("checkrd_observation_only") is True

    def test_tracing_processor_emits_span_telemetry(self) -> None:
        from checkrd.integrations.openai_agents import CheckrdTracingProcessor

        engine = MockEngine(default="allow")
        sink = _ListSink()
        proc = CheckrdTracingProcessor(
            engine=engine,
            agent_id="test-agent",
            sink=sink,
        )

        # Build minimal duck-typed Trace and Span objects. The Agents
        # SDK uses dataclasses internally; we only need attribute access.
        class _FakeSpanData:
            model = "gpt-4o"
            usage = {"input_tokens": 10, "output_tokens": 20}

        class _FakeSpan:
            trace_id = "trace-1"
            span_id = "span-1"
            parent_id = None
            started_at = "2026-04-24T00:00:00+00:00"
            ended_at = "2026-04-24T00:00:01+00:00"
            span_data = _FakeSpanData()

        proc.on_span_start(_FakeSpan())
        proc.on_span_end(_FakeSpan())

        starts = [e for e in sink.events if e["event_type"].endswith("_start")]
        ends = [e for e in sink.events if e["event_type"].endswith("_end")]
        assert starts and ends
        assert ends[0]["target"] == "gpt-4o"
        assert ends[0]["input_tokens"] == 10
        assert ends[0]["latency_ms"] is not None


# ======================================================================
# Anthropic Claude Agent SDK
# ======================================================================


class TestClaudeAgentSDK:
    """Claude Agent SDK PreToolUse / PostToolUse hooks."""

    def setup_method(self) -> None:
        pytest.importorskip("claude_agent_sdk")

    def test_pre_tool_use_hook_blocks_on_deny(self) -> None:
        from checkrd.integrations.claude_agent_sdk import (
            make_pre_tool_use_hook,
        )

        engine = MockEngine(default="deny")
        sink = _ListSink()
        hook = make_pre_tool_use_hook(
            engine=engine,
            agent_id="test-agent",
            sink=sink,
            enforce=True,
        )

        out = asyncio.run(
            hook(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                    "tool_input": {"command": "rm -rf /"},
                    "session_id": "sess-1",
                },
                "tool-use-1",
                None,
            )
        )
        assert out.get("decision") == "block"
        assert "denied" in out.get("systemMessage", "").lower()

        # Telemetry recorded with allowed=False.
        assert any(
            e["event_type"] == "claude_agent_pre_tool_use" and e["allowed"] is False
            for e in sink.events
        )

    def test_pre_tool_use_hook_allows(self) -> None:
        from checkrd.integrations.claude_agent_sdk import (
            make_pre_tool_use_hook,
        )

        engine = MockEngine(default="allow")
        sink = _ListSink()
        hook = make_pre_tool_use_hook(
            engine=engine,
            agent_id="test-agent",
            sink=sink,
            enforce=True,
        )

        out = asyncio.run(
            hook(
                {
                    "tool_name": "Read",
                    "tool_input": {"file_path": "/tmp/x"},
                    "session_id": "sess-1",
                },
                "tool-use-2",
                None,
            )
        )
        # Empty dict on allow.
        assert out == {}

    def test_pre_tool_use_observation_mode(self) -> None:
        from checkrd.integrations.claude_agent_sdk import (
            make_pre_tool_use_hook,
        )

        engine = MockEngine(default="deny")
        hook = make_pre_tool_use_hook(
            engine=engine,
            agent_id="test-agent",
            enforce=False,
        )

        out = asyncio.run(
            hook(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "ls"},
                    "session_id": "sess-1",
                },
                "tool-use-3",
                None,
            )
        )
        # Observation mode: never block.
        assert out == {}

    def test_attach_to_options_is_idempotent(self) -> None:
        from claude_agent_sdk import ClaudeAgentOptions

        from checkrd.integrations.claude_agent_sdk import attach_to_options

        engine = MockEngine(default="allow")
        options = ClaudeAgentOptions()
        attach_to_options(options, engine=engine, agent_id="test-agent")
        attach_to_options(options, engine=engine, agent_id="test-agent")

        # Each event should have exactly one HookMatcher with our marker.
        for event in ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop"):
            assert len(options.hooks.get(event, [])) == 1


# ======================================================================
# MCP
# ======================================================================


class TestMCP:
    """MCP server-handler wrap and CheckrdClientSession."""

    def setup_method(self) -> None:
        pytest.importorskip("mcp")

    def test_wrap_call_tool_handler_blocks_on_deny(self) -> None:
        from checkrd.integrations.mcp import wrap_call_tool_handler

        engine = MockEngine(default="deny")
        sink = _ListSink()

        async def real_handler(ctx: Any, params: Any) -> Any:
            return {"content": []}

        wrapped = wrap_call_tool_handler(
            real_handler,
            engine=engine,
            agent_id="test-agent",
            sink=sink,
            enforce=True,
            server_name="test-server",
        )

        # Build a minimal params-like object.
        class _Params:
            name = "delete_file"
            arguments = {"path": "/etc/passwd"}

        with pytest.raises(CheckrdPolicyDenied) as excinfo:
            asyncio.run(wrapped(None, _Params()))
        assert "test-server/tools/delete_file" in (excinfo.value.url or "")
        assert any(
            e["event_type"] == "mcp_call_tool" and e["allowed"] is False for e in sink.events
        )

    def test_wrap_call_tool_handler_allows(self) -> None:
        from checkrd.integrations.mcp import wrap_call_tool_handler

        engine = MockEngine(default="allow")
        sink = _ListSink()
        called = []

        async def real_handler(ctx: Any, params: Any) -> Any:
            called.append(params.name)
            return {"content": [{"type": "text", "text": "ok"}]}

        wrapped = wrap_call_tool_handler(
            real_handler,
            engine=engine,
            agent_id="test-agent",
            sink=sink,
        )

        class _Params:
            name = "search"
            arguments = {"q": "rust"}

        result = asyncio.run(wrapped(None, _Params()))
        assert called == ["search"]
        assert result["content"][0]["text"] == "ok"

    def test_wrap_call_tool_observation_mode(self) -> None:
        from checkrd.integrations.mcp import wrap_call_tool_handler

        engine = MockEngine(default="deny")
        called = []

        async def real_handler(ctx: Any, params: Any) -> Any:
            called.append(params.name)
            return {"content": []}

        wrapped = wrap_call_tool_handler(
            real_handler,
            engine=engine,
            agent_id="test-agent",
            enforce=False,
        )

        class _Params:
            name = "search"
            arguments = {}

        # Should NOT raise; should still call the underlying handler.
        asyncio.run(wrapped(None, _Params()))
        assert called == ["search"]
