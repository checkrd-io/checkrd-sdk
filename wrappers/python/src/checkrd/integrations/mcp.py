"""Model Context Protocol (MCP) integration for Checkrd.

The MCP Python SDK has no app-level middleware chain. Three canonical
interception points exist; this module ships an adapter for each:

1. **Server, low-level** — the user passes `on_call_tool`,
   `on_list_tools`, etc. to :class:`mcp.server.lowlevel.server.Server`.
   :func:`wrap_call_tool_handler` wraps a user-supplied handler so
   every tool invocation is policy-evaluated before delegating.

2. **Client** — every outbound call goes through
   :class:`mcp.client.session.ClientSession`. :class:`CheckrdClientSession`
   is a drop-in subclass that overrides ``call_tool``, ``read_resource``,
   and ``get_prompt`` to evaluate before delegating.

3. **Server, transport (Streamable HTTP)** — when the server is
   exposed via Streamable HTTP, the canonical interception is plain
   Starlette / ASGI middleware on the mounted app. Checkrd's
   :class:`checkrd.asgi.CheckrdASGIMiddleware` is the right choice for
   that path; this module focuses on the JSON-RPC layer where tool
   calls are individually addressable.

Why no patching: the MCP SDK is iterating quickly. Patching internals
would break on every minor release. The handler-wrap and subclass
patterns target the documented public API surface.

Usage — server::

    from mcp.server.lowlevel.server import Server
    from checkrd import Checkrd
    from checkrd.integrations.mcp import wrap_call_tool_handler

    with Checkrd() as client:
        async def my_tool_handler(ctx, params):
            ...

        server = Server(
            "my-server",
            on_call_tool=wrap_call_tool_handler(
                my_tool_handler, client=client, server_name="my-server",
            ),
        )

Usage — client::

    from mcp import StdioServerParameters
    from mcp.client.stdio import stdio_client
    from checkrd.integrations.mcp import CheckrdClientSession

    async with stdio_client(server_params) as (read, write):
        async with CheckrdClientSession(
            read, write, checkrd_client=client, server_name="github-mcp",
        ) as session:
            await session.initialize()
            await session.call_tool("create_issue", {"title": "..."})

Reference: https://github.com/modelcontextprotocol/python-sdk
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

# Hard imports: this module requires the mcp package.
from mcp.client.session import ClientSession

from checkrd._state import _GlobalContext, get_context
from checkrd.engine import EvalResult, WasmEngine
from checkrd.exceptions import CheckrdPolicyDenied
from checkrd.sinks import TelemetrySink

if TYPE_CHECKING:
    from checkrd.client import Checkrd

logger = logging.getLogger("checkrd.integrations.mcp")


# ----------------------------------------------------------------------
# Server-side: wrap individual handlers
# ----------------------------------------------------------------------


def wrap_call_tool_handler(
    handler: Callable[..., Awaitable[Any]],
    *,
    client: Optional["Checkrd"] = None,
    engine: Optional[WasmEngine] = None,
    agent_id: Optional[str] = None,
    sink: Optional[TelemetrySink] = None,
    enforce: bool = True,
    server_name: str = "mcp",
    dashboard_url: Optional[str] = None,
) -> Callable[..., Awaitable[Any]]:
    """Wrap a user-supplied ``on_call_tool`` handler with Checkrd enforcement.

    The wrapped handler evaluates each call through the WASM core
    BEFORE delegating to the user's handler. On deny in enforce mode,
    raises :class:`CheckrdPolicyDenied` — the MCP server framework
    converts this into a JSON-RPC error response back to the client.

    Args:
        handler: The user's ``async def handler(ctx, params)`` function.
        client: Optional :class:`Checkrd` client. Provides engine,
            agent_id, sink, enforce, dashboard_url defaults.
        engine, agent_id, sink, enforce, dashboard_url: Explicit
            overrides.
        server_name: Friendly server identifier; appears as the URL
            authority in policy matchers, e.g.
            ``https://my-server/tools/<name>``.

    Returns:
        A new async function with the same signature.
    """
    engine_, agent_id_, sink_, enforce_, dashboard_ = _resolve_options(
        client=client,
        engine=engine,
        agent_id=agent_id,
        sink=sink,
        enforce=enforce,
        dashboard_url=dashboard_url,
    )

    async def wrapped(ctx: Any, params: Any, *args: Any, **kwargs: Any) -> Any:
        tool_name = _extract_tool_name(params)
        tool_arguments = _extract_tool_arguments(params)

        result = _evaluate(
            engine=engine_,
            kind="tools",
            target=tool_name,
            server_name=server_name,
            body_obj={"arguments": tool_arguments, "tool": tool_name},
            extra_headers=[
                ("x-mcp-tool", tool_name),
                ("x-mcp-server", server_name),
            ],
        )

        _enqueue_safe(
            sink_,
            {
                "event_type": "mcp_call_tool",
                "request_id": result.request_id,
                "agent_id": agent_id_,
                "server_name": server_name,
                "tool_name": tool_name,
                "allowed": result.allowed,
                "deny_reason": result.deny_reason if not result.allowed else None,
                "side": "server",
            },
        )

        if not result.allowed:
            if enforce_:
                raise CheckrdPolicyDenied(
                    reason=result.deny_reason or "policy denied",
                    request_id=result.request_id,
                    url=f"https://{server_name}/tools/{tool_name}",
                    dashboard_url=_build_dashboard_url(dashboard_, result.request_id),
                )
            logger.warning(
                "checkrd: mcp tool %s denied (observation mode): %s",
                tool_name,
                result.deny_reason,
            )

        return await handler(ctx, params, *args, **kwargs)

    return wrapped


def wrap_list_tools_handler(
    handler: Callable[..., Awaitable[Any]],
    *,
    client: Optional["Checkrd"] = None,
    engine: Optional[WasmEngine] = None,
    agent_id: Optional[str] = None,
    sink: Optional[TelemetrySink] = None,
    enforce: bool = True,
    server_name: str = "mcp",
    dashboard_url: Optional[str] = None,
) -> Callable[..., Awaitable[Any]]:
    """Wrap an ``on_list_tools`` handler with Checkrd enforcement.

    List operations are policy-evaluated as a single ``"*"`` target so
    operators can write ``deny: { url: "my-server/tools" }`` rules to
    restrict which agents can enumerate available tools. Default
    policies typically allow listing.
    """
    engine_, agent_id_, sink_, enforce_, dashboard_ = _resolve_options(
        client=client,
        engine=engine,
        agent_id=agent_id,
        sink=sink,
        enforce=enforce,
        dashboard_url=dashboard_url,
    )

    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        result = _evaluate(
            engine=engine_,
            kind="tools-list",
            target="*",
            server_name=server_name,
            body_obj={},
            extra_headers=[("x-mcp-server", server_name)],
        )
        _enqueue_safe(
            sink_,
            {
                "event_type": "mcp_list_tools",
                "request_id": result.request_id,
                "agent_id": agent_id_,
                "server_name": server_name,
                "allowed": result.allowed,
                "side": "server",
            },
        )
        if not result.allowed:
            if enforce_:
                raise CheckrdPolicyDenied(
                    reason=result.deny_reason or "policy denied",
                    request_id=result.request_id,
                    url=f"https://{server_name}/tools",
                    dashboard_url=_build_dashboard_url(dashboard_, result.request_id),
                )
            logger.warning(
                "checkrd: mcp list-tools denied (observation mode): %s",
                result.deny_reason,
            )
        return await handler(*args, **kwargs)

    return wrapped


# ----------------------------------------------------------------------
# Client-side: ClientSession subclass
# ----------------------------------------------------------------------


class CheckrdClientSession(ClientSession):
    """:class:`ClientSession` subclass that policy-evaluates outbound calls.

    Overrides ``call_tool``, ``read_resource``, and ``get_prompt`` to
    evaluate each call through the WASM core before sending it to the
    server. On deny in enforce mode, raises
    :class:`CheckrdPolicyDenied` — the request never reaches the wire.

    All other methods (initialize, list_tools, sampling, etc.) pass
    through unchanged. Use it anywhere you'd use ``ClientSession``::

        async with stdio_client(params) as (read, write):
            async with CheckrdClientSession(
                read, write,
                checkrd_client=client,
                server_name="github-mcp",
            ) as session:
                await session.initialize()
                await session.call_tool("create_issue", {"title": "..."})

    Args:
        *args: Forwarded to :class:`ClientSession`.
        checkrd_client: Optional :class:`Checkrd` client. Provides the
            engine and other config from its global context.
        engine, agent_id, sink, enforce, dashboard_url: Explicit
            overrides — required if ``checkrd_client`` is omitted.
        server_name: Friendly server identifier for policy matchers.
            Default ``"mcp"``.
        **kwargs: Forwarded to :class:`ClientSession`.
    """

    def __init__(
        self,
        *args: Any,
        checkrd_client: Optional["Checkrd"] = None,
        engine: Optional[WasmEngine] = None,
        agent_id: Optional[str] = None,
        sink: Optional[TelemetrySink] = None,
        enforce: bool = True,
        server_name: str = "mcp",
        dashboard_url: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        (
            self._checkrd_engine,
            self._checkrd_agent_id,
            self._checkrd_sink,
            self._checkrd_enforce,
            self._checkrd_dashboard,
        ) = _resolve_options(
            client=checkrd_client,
            engine=engine,
            agent_id=agent_id,
            sink=sink,
            enforce=enforce,
            dashboard_url=dashboard_url,
        )
        self._checkrd_server_name = server_name

    async def call_tool(
        self,
        name: str,
        arguments: Optional[dict[str, Any]] = None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        await self._checkrd_gate(
            kind="tools",
            target=name,
            body_obj={"arguments": arguments or {}, "tool": name},
        )
        return await super().call_tool(name, arguments, *args, **kwargs)

    async def read_resource(
        self,
        uri: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        await self._checkrd_gate(
            kind="resources",
            target=str(uri),
            body_obj={"uri": str(uri)},
        )
        return await super().read_resource(uri, *args, **kwargs)

    async def get_prompt(
        self,
        name: str,
        arguments: Optional[dict[str, Any]] = None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        await self._checkrd_gate(
            kind="prompts",
            target=name,
            body_obj={"name": name, "arguments": arguments or {}},
        )
        return await super().get_prompt(name, arguments, *args, **kwargs)

    async def _checkrd_gate(
        self,
        *,
        kind: str,
        target: str,
        body_obj: Any,
    ) -> None:
        result = _evaluate(
            engine=self._checkrd_engine,
            kind=kind,
            target=target,
            server_name=self._checkrd_server_name,
            body_obj=body_obj,
            extra_headers=[
                ("x-mcp-server", self._checkrd_server_name),
                ("x-mcp-method", kind),
                ("x-mcp-target", target),
            ],
        )

        _enqueue_safe(
            self._checkrd_sink,
            {
                "event_type": f"mcp_{kind}_call",
                "request_id": result.request_id,
                "agent_id": self._checkrd_agent_id,
                "server_name": self._checkrd_server_name,
                "kind": kind,
                "target": target,
                "allowed": result.allowed,
                "deny_reason": result.deny_reason if not result.allowed else None,
                "side": "client",
            },
        )

        if not result.allowed:
            if self._checkrd_enforce:
                raise CheckrdPolicyDenied(
                    reason=result.deny_reason or "policy denied",
                    request_id=result.request_id,
                    url=f"https://{self._checkrd_server_name}/{kind}/{target}",
                    dashboard_url=_build_dashboard_url(
                        self._checkrd_dashboard,
                        result.request_id,
                    ),
                )
            logger.warning(
                "checkrd: mcp client %s %s denied (observation mode): %s",
                kind,
                target,
                result.deny_reason,
            )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _resolve_options(
    *,
    client: Optional["Checkrd"],
    engine: Optional[WasmEngine],
    agent_id: Optional[str],
    sink: Optional[TelemetrySink],
    enforce: bool,
    dashboard_url: Optional[str],
) -> tuple[WasmEngine, str, Optional[TelemetrySink], bool, str]:
    if client is not None:
        client._ensure_global_context()
        ctx: _GlobalContext = get_context()
        return (
            engine or ctx.engine,
            agent_id or ctx.settings.agent_id,
            sink if sink is not None else ctx.sink,
            ctx.enforce if client is not None else enforce,
            dashboard_url or ctx.settings.dashboard_url or "",
        )

    if engine is None or agent_id is None:
        raise ValueError(
            "MCP integration requires either checkrd_client= or both engine= and agent_id="
        )
    return engine, agent_id, sink, enforce, dashboard_url or ""


def _extract_tool_name(params: Any) -> str:
    """Pull the tool name from MCP request params (duck-typed across SDK versions)."""
    if hasattr(params, "name"):
        return str(getattr(params, "name", "unknown"))
    if isinstance(params, dict):
        return str(params.get("name", "unknown"))
    return "unknown"


def _extract_tool_arguments(params: Any) -> Any:
    if hasattr(params, "arguments"):
        return getattr(params, "arguments", None)
    if isinstance(params, dict):
        return params.get("arguments")
    return None


def _evaluate(
    *,
    engine: WasmEngine,
    kind: str,
    target: str,
    server_name: str,
    body_obj: Any,
    extra_headers: list[tuple[str, str]],
) -> EvalResult:
    url = f"https://{server_name}/{kind}/{target}"
    body_json = _safe_json(body_obj)
    now = datetime.now(timezone.utc)
    return engine.evaluate(
        request_id="",
        method="POST",
        url=url,
        headers=extra_headers,
        body=body_json,
        timestamp=now.isoformat(),
        timestamp_ms=int(now.timestamp() * 1000),
    )


def _enqueue_safe(
    sink: Optional[TelemetrySink],
    event: dict[str, Any],
) -> None:
    if sink is None:
        return
    try:
        sink.enqueue(event)
    except Exception:
        logger.warning("checkrd: mcp telemetry enqueue failed", exc_info=True)


def _build_dashboard_url(base: str, request_id: str) -> Optional[str]:
    if not base:
        return None
    return f"{base.rstrip('/')}/events/{request_id}"


def _safe_json(obj: Any) -> str:
    try:
        return json.dumps(
            obj,
            default=lambda o: getattr(o, "model_dump", lambda: str(o))(),
        )
    except (TypeError, ValueError):
        return json.dumps({"_repr": str(obj)})


__all__ = [
    "wrap_call_tool_handler",
    "wrap_list_tools_handler",
    "CheckrdClientSession",
]
