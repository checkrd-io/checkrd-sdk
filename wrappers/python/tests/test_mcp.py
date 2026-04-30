"""Tests for the MCP middleware adapter (`checkrd.mcp`).

The official `mcp` SDK is intentionally NOT installed in test deps —
the wrapper is structurally typed against the MCP shape, so the tests
use hand-rolled stand-ins that match the SDK's surface (`call_tool`,
`read_resource`, `get_prompt`, `list_tools`) and verify wrapping
behaviour without pulling in the real package.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from checkrd.engine import WasmEngine
from checkrd.exceptions import CheckrdPolicyDenied
from checkrd.mcp import wrap_mcp_client, wrap_mcp_server

ALLOW_ALL = json.dumps({"agent": "t", "default": "allow", "rules": []})
DENY_ALL = json.dumps({"agent": "t", "default": "deny", "rules": []})
DENY_SEARCH = json.dumps(
    {
        "agent": "t",
        "default": "allow",
        "rules": [
            {
                "name": "block-search-tool",
                "deny": {
                    "method": ["POST"],
                    "url": "mcp/tools/search",
                },
            },
        ],
    },
)


class StubClient:
    """Minimal MCP-shaped client. Methods are async, mirroring the
    real SDK; we record every call for inspection."""

    def __init__(self) -> None:
        self.call_tool_calls: list[dict[str, Any]] = []
        self.read_resource_calls: list[dict[str, Any]] = []
        self.get_prompt_calls: list[dict[str, Any]] = []
        self.list_tools_calls = 0

    async def call_tool(
        self, name: str, arguments: Any = None,
    ) -> dict[str, Any]:
        self.call_tool_calls.append({"name": name, "arguments": arguments})
        return {"content": [{"type": "text", "text": "ok"}]}

    async def read_resource(self, uri: str) -> dict[str, Any]:
        self.read_resource_calls.append({"uri": uri})
        return {"contents": [{"uri": uri, "text": "hi"}]}

    async def get_prompt(
        self, name: str, arguments: Any = None,
    ) -> dict[str, Any]:
        self.get_prompt_calls.append({"name": name, "arguments": arguments})
        return {"messages": []}

    async def list_tools(self) -> dict[str, Any]:
        self.list_tools_calls += 1
        return {"tools": []}


class TestWrapMcpClientAllowPath:
    def test_call_tool_forwards_under_allow_all(self) -> None:
        engine = WasmEngine(ALLOW_ALL, "test")
        raw = StubClient()
        client = wrap_mcp_client(
            raw, engine=engine, enforce=True, agent_id="test",
        )
        result = asyncio.run(
            client.call_tool("search", arguments={"q": "x"}),
        )
        assert result == {"content": [{"type": "text", "text": "ok"}]}
        assert raw.call_tool_calls == [{"name": "search", "arguments": {"q": "x"}}]

    def test_read_resource_forwards(self) -> None:
        engine = WasmEngine(ALLOW_ALL, "test")
        raw = StubClient()
        client = wrap_mcp_client(
            raw, engine=engine, enforce=True, agent_id="test",
        )
        asyncio.run(client.read_resource(uri="file://x"))
        assert raw.read_resource_calls == [{"uri": "file://x"}]

    def test_get_prompt_forwards(self) -> None:
        engine = WasmEngine(ALLOW_ALL, "test")
        raw = StubClient()
        client = wrap_mcp_client(
            raw, engine=engine, enforce=True, agent_id="test",
        )
        asyncio.run(client.get_prompt(name="greet", arguments={"who": "world"}))
        assert raw.get_prompt_calls == [
            {"name": "greet", "arguments": {"who": "world"}},
        ]

    def test_list_tools_forwards(self) -> None:
        engine = WasmEngine(ALLOW_ALL, "test")
        raw = StubClient()
        client = wrap_mcp_client(
            raw, engine=engine, enforce=True, agent_id="test",
        )
        asyncio.run(client.list_tools())
        assert raw.list_tools_calls == 1


class TestWrapMcpClientDenyPath:
    def test_default_deny_blocks_in_enforce_mode(self) -> None:
        engine = WasmEngine(DENY_ALL, "test")
        raw = StubClient()
        client = wrap_mcp_client(
            raw, engine=engine, enforce=True, agent_id="test",
        )
        with pytest.raises(CheckrdPolicyDenied):
            asyncio.run(client.call_tool("search"))
        assert raw.call_tool_calls == []

    def test_specific_tool_denied_via_url_matcher(self) -> None:
        engine = WasmEngine(DENY_SEARCH, "test")
        raw = StubClient()
        client = wrap_mcp_client(
            raw, engine=engine, enforce=True, agent_id="test",
        )
        with pytest.raises(CheckrdPolicyDenied):
            asyncio.run(client.call_tool("search"))
        assert raw.call_tool_calls == []
        # Other tools still pass:
        asyncio.run(client.call_tool("fetch"))
        assert raw.call_tool_calls == [{"name": "fetch", "arguments": None}]

    def test_observe_only_forwards_despite_deny(self) -> None:
        engine = WasmEngine(DENY_ALL, "test")
        raw = StubClient()
        client = wrap_mcp_client(
            raw, engine=engine, enforce=False, agent_id="test",
        )
        result = asyncio.run(client.call_tool("search"))
        assert result == {"content": [{"type": "text", "text": "ok"}]}
        assert raw.call_tool_calls == [{"name": "search", "arguments": None}]


class TestWrapMcpClientProxyTransparency:
    def test_unknown_attributes_pass_through(self) -> None:
        engine = WasmEngine(ALLOW_ALL, "test")

        class WithExtras(StubClient):
            custom_field = "preserved"

            async def custom_method(self) -> str:
                return "result"

        raw = WithExtras()
        client = wrap_mcp_client(
            raw, engine=engine, enforce=True, agent_id="test",
        )
        assert client.custom_field == "preserved"
        assert asyncio.run(client.custom_method()) == "result"


class TestWrapMcpServerHandlerWrapping:
    def test_decorator_factory_wraps_async_handler(self) -> None:
        engine = WasmEngine(ALLOW_ALL, "test")
        registered: list[Any] = []
        user_handler_calls: list[Any] = []

        class StubServer:
            def call_tool(self) -> Any:  # decorator factory
                def decorator(fn: Any) -> Any:
                    registered.append(fn)
                    return fn
                return decorator

        srv = wrap_mcp_server(
            StubServer(), engine=engine, enforce=True, agent_id="test",
        )

        async def my_tool(request: Any) -> dict[str, Any]:
            user_handler_calls.append(request)
            return {"ok": True}

        # Apply the decorator explicitly so we keep a reference to the
        # original `my_tool` — the `@deco` sugar would reassign the
        # name to the decorator's return value.
        decorated = srv.call_tool()(my_tool)

        # Register fired exactly once; the registered fn is the
        # policy-wrapped one, not the raw handler.
        assert len(registered) == 1
        wrapped = registered[0]
        assert wrapped is not my_tool
        # Since StubServer's decorator returns fn unchanged, `decorated`
        # is `wrapped` — the user's `my_tool` name (if they'd used the
        # sugar) would point at the policy-wrapped fn too.
        assert decorated is wrapped

        # Calling the wrapped handler runs through policy + delegates
        # to the real user handler with the original args.
        class _Req:
            class params:  # noqa: D106
                name = "my_tool"
                arguments = {"q": "x"}

        req = _Req()
        result = asyncio.run(wrapped(req))
        assert result == {"ok": True}
        assert user_handler_calls == [req]

    def test_decorator_factory_blocks_under_deny(self) -> None:
        engine = WasmEngine(DENY_ALL, "test")
        registered: list[Any] = []
        user_handler_ran = False

        class StubServer:
            def call_tool(self) -> Any:
                def decorator(fn: Any) -> Any:
                    registered.append(fn)
                    return fn
                return decorator

        srv = wrap_mcp_server(
            StubServer(), engine=engine, enforce=True, agent_id="test",
        )

        async def blocked_tool(request: Any) -> dict[str, Any]:  # noqa: ARG001
            nonlocal user_handler_ran
            user_handler_ran = True  # pragma: no cover
            return {"ok": True}

        srv.call_tool()(blocked_tool)
        wrapped = registered[0]

        class _Req:
            class params:  # noqa: D106
                name = "blocked_tool"
                arguments = None

        with pytest.raises(CheckrdPolicyDenied):
            asyncio.run(wrapped(_Req()))
        assert not user_handler_ran

    def test_set_request_handler_wraps_handler(self) -> None:
        engine = WasmEngine(ALLOW_ALL, "test")
        registered: list[Any] = []

        class StubServer:
            def set_request_handler(self, schema: Any, handler: Any) -> None:  # noqa: ARG002
                registered.append(handler)

        srv = wrap_mcp_server(
            StubServer(), engine=engine, enforce=True, agent_id="test",
        )

        async def my_handler(request: Any) -> dict[str, Any]:  # noqa: ARG001
            return {"ok": True}

        srv.set_request_handler({"fake": "schema"}, my_handler)
        wrapped = registered[0]
        assert wrapped is not my_handler

        class _Req:
            class params:  # noqa: D106
                name = "anything"
                arguments = None

        result = asyncio.run(wrapped(_Req()))
        assert result == {"ok": True}
