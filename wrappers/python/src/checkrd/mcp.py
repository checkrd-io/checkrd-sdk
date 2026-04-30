"""Model Context Protocol (MCP) integration for Checkrd.

This module mirrors the JavaScript ``checkrd/mcp`` subpath. Two
helpers ship:

- :func:`wrap_mcp_client` — wrap an MCP client (``mcp.ClientSession``
  or compatible) so every ``call_tool`` / ``read_resource`` /
  ``get_prompt`` invocation is policy-evaluated before reaching the
  server. Telemetry is emitted to the configured sink, and denied
  requests raise :class:`~checkrd.exceptions.CheckrdPolicyDenied` when
  ``enforce=True`` (or are logged-only when ``enforce=False``).

- :func:`wrap_mcp_server` — wrap an MCP server's request-handler
  registration so every tool / resource / prompt request runs through
  Checkrd before the user's handler executes. Server-side enforcement
  for MCP server operators.

Why MCP specifically: the ``mcp`` PyPI package sees ~97M monthly
downloads across Python + TS combined; the Linux Foundation hosts the
spec; 30+ CVEs were disclosed in MCP servers in the protocol's first
year. An audited, policy-enforcing middleware is the missing piece
nobody in the Python ecosystem has shipped.

Example::

    from mcp import ClientSession
    from checkrd import init
    from checkrd.mcp import wrap_mcp_client

    init(api_key="ck_live_...", policy="policy.yaml")

    raw = ClientSession(...)
    await raw.connect()
    client = wrap_mcp_client(raw, agent_id="my-agent", server_name="github-mcp")

    # Every call through the wrapped client is policy-evaluated:
    result = await client.call_tool("create_issue", arguments={...})

The integration is intentionally **structurally typed** against the
``mcp`` SDK rather than importing concrete classes. MCP's surface is
iterating quickly (the Streamable-HTTP transport spec was finalised
in 2026); structural typing means an SDK minor bump doesn't force a
Checkrd release. We verify the runtime shape in tests.
"""

from __future__ import annotations

import functools
import json
import logging
import urllib.parse
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional, TypeVar

from checkrd._state import _GlobalContext, get_context
from checkrd.engine import WasmEngine
from checkrd.exceptions import CheckrdPolicyDenied
from checkrd.sinks import TelemetrySink

logger = logging.getLogger("checkrd")

T = TypeVar("T")

__all__ = [
    "wrap_mcp_client",
    "wrap_mcp_server",
    "McpPolicyOptions",
]


class McpPolicyOptions:
    """Configuration for MCP wrapping helpers.

    Most callers won't construct this directly — :func:`wrap_mcp_client`
    and :func:`wrap_mcp_server` accept the same kwargs and build the
    options for you. Exposed as a class so advanced integrations can
    cache the configuration once and pass it to many wrappers.

    Attributes:
        engine: The WASM engine used to evaluate policy.
        enforce: When ``True``, deny decisions raise
            :class:`CheckrdPolicyDenied`; when ``False``, the call is
            logged and forwarded (observe-only).
        agent_id: Correlation ID emitted with every telemetry event.
        sink: Optional telemetry sink for per-call events.
        server_name: Friendly authority for synthetic MCP URLs. Set to
            something like ``"github-mcp"`` so policy URL matchers can
            distinguish between MCP servers. Default: ``"mcp"``.
        dashboard_url: Base URL embedded in deny-error deep links.
    """

    def __init__(
        self,
        engine: WasmEngine,
        enforce: bool,
        agent_id: str,
        *,
        sink: Optional[TelemetrySink] = None,
        server_name: str = "mcp",
        dashboard_url: Optional[str] = None,
    ) -> None:
        self.engine = engine
        self.enforce = enforce
        self.agent_id = agent_id
        self.sink = sink
        self.server_name = server_name
        self.dashboard_url = dashboard_url


def _resolve_options(
    *,
    engine: Optional[WasmEngine] = None,
    enforce: Optional[bool] = None,
    agent_id: Optional[str] = None,
    sink: Optional[TelemetrySink] = None,
    server_name: str = "mcp",
    dashboard_url: Optional[str] = None,
    options: Optional[McpPolicyOptions] = None,
) -> McpPolicyOptions:
    """Build an :class:`McpPolicyOptions` from kwargs or context fallback.

    If ``options`` is supplied, it wins. Otherwise we honor explicit
    kwargs first, then fall back to the global Checkrd context (set by
    :func:`checkrd.init`) for whatever's missing.
    """
    if options is not None:
        return options
    ctx: Optional[_GlobalContext] = None
    if engine is None or enforce is None or agent_id is None:
        try:
            ctx = get_context()
        except Exception:  # noqa: BLE001
            ctx = None
        if ctx is not None:
            engine = engine if engine is not None else ctx.engine
            enforce = enforce if enforce is not None else ctx.enforce
            agent_id = (
                agent_id
                if agent_id is not None
                else ctx.settings.agent_id
            )
            sink = sink if sink is not None else ctx.sink
            dashboard_url = (
                dashboard_url
                if dashboard_url is not None
                else ctx.settings.dashboard_url
            )
    if engine is None:
        raise TypeError(
            "wrap_mcp_*() requires engine= or a prior checkrd.init() call"
        )
    if enforce is None:
        raise TypeError(
            "wrap_mcp_*() requires enforce= or a prior checkrd.init() call"
        )
    if agent_id is None:
        raise TypeError(
            "wrap_mcp_*() requires agent_id= or a prior checkrd.init() call"
        )
    return McpPolicyOptions(
        engine=engine,
        enforce=enforce,
        agent_id=agent_id,
        sink=sink,
        server_name=server_name,
        dashboard_url=dashboard_url,
    )


def _evaluate_or_raise(
    options: McpPolicyOptions,
    *,
    method_kind: str,
    name: str,
    arguments: Any,
) -> None:
    """Evaluate one MCP call against the policy engine.

    ``method_kind`` is one of ``"tool"``, ``"resource"``, ``"prompt"``,
    or ``"list"`` — used in telemetry headers + the synthetic URL so
    operators can write `url: "**/tools/**"` style matchers.

    Raises :class:`CheckrdPolicyDenied` when the engine returns deny
    AND ``options.enforce`` is true. Otherwise, denies are logged at
    WARN and the call returns normally so the caller can proceed.
    """
    request_id = str(uuid.uuid4())
    body: Optional[str]
    if arguments is None:
        body = None
    else:
        try:
            body = json.dumps(arguments, default=str)
        except (TypeError, ValueError):
            body = None
    now = datetime.now(timezone.utc)
    url = _synthetic_url(options.server_name, method_kind, name)

    result = options.engine.evaluate(
        request_id=request_id,
        method="POST",
        url=url,
        headers=[
            ("content-type", "application/json"),
            ("x-mcp-method", method_kind),
            ("x-mcp-target", name),
        ],
        body=body,
        timestamp=now.isoformat(),
        timestamp_ms=int(now.timestamp() * 1000),
    )

    _enqueue_telemetry(
        options.sink, getattr(result, "telemetry_json", ""), options.agent_id,
    )

    if result.allowed:
        return

    logger.warning(
        "checkrd: MCP %s '%s' denied: %s (request_id=%s)",
        method_kind, name, result.deny_reason, result.request_id,
    )

    if not options.enforce:
        return

    raise CheckrdPolicyDenied(
        reason=result.deny_reason or "policy denied",
        request_id=result.request_id,
        url=url,
        dashboard_url=options.dashboard_url,
    )


def _synthetic_url(server_name: str, method_kind: str, name: str) -> str:
    """Build the synthetic ``https://{server}/{kind}/{name}`` URL.

    Resource URIs are query-encoded so policy matchers can target the
    URI prefix without confusing the host/path split (resource URIs
    legitimately contain slashes; encoding them avoids ambiguous URL
    parses on the matcher side).
    """
    quoted = urllib.parse.quote(name, safe="")
    if method_kind == "resource":
        return f"https://{server_name}/resources?uri={quoted}"
    if method_kind == "list":
        return f"https://{server_name}/{name}"
    return f"https://{server_name}/{method_kind}s/{quoted}"


def _enqueue_telemetry(
    sink: Optional[TelemetrySink], telemetry_json: str, agent_id: str,
) -> None:
    """Fan an engine telemetry event into the sink (best-effort)."""
    if sink is None or not telemetry_json:
        return
    try:
        event = json.loads(telemetry_json)
        event["agent_id"] = agent_id
        sink.enqueue(event)
    except (ValueError, AttributeError):  # noqa: BLE001
        logger.debug("checkrd: MCP telemetry enqueue failed", exc_info=True)


# ---------------------------------------------------------------------------
# Client wrapping
# ---------------------------------------------------------------------------


def wrap_mcp_client(
    client: T,
    *,
    engine: Optional[WasmEngine] = None,
    enforce: Optional[bool] = None,
    agent_id: Optional[str] = None,
    sink: Optional[TelemetrySink] = None,
    server_name: str = "mcp",
    dashboard_url: Optional[str] = None,
    options: Optional[McpPolicyOptions] = None,
) -> T:
    """Wrap an MCP client so every tool/resource/prompt call is checked.

    Args:
        client: Any object with the MCP client method shape:
            ``call_tool(name, arguments=...)``,
            ``read_resource(uri=...)``, ``get_prompt(name, arguments=...)``,
            and the matching ``list_*`` methods. Concretely
            ``mcp.ClientSession`` from the official Python SDK.
        engine: Override the engine. Default: from ``checkrd.init()``.
        enforce: Override the enforce flag. Default: from ``checkrd.init()``.
        agent_id: Override agent id. Default: from ``checkrd.init()``.
        sink: Override telemetry sink. Default: from ``checkrd.init()``.
        server_name: Authority used in synthetic MCP URLs.
        dashboard_url: Base URL for deny-error deep links.
        options: Pre-built :class:`McpPolicyOptions`. Wins if supplied.

    Returns:
        An object that behaves identically to ``client`` for every
        method except the wrapped MCP entry points. Unknown attributes
        and methods pass through untouched.

    Example::

        client = wrap_mcp_client(raw_client, agent_id="my-agent")
        result = await client.call_tool("search", arguments={"q": "foo"})
    """
    opts = _resolve_options(
        engine=engine,
        enforce=enforce,
        agent_id=agent_id,
        sink=sink,
        server_name=server_name,
        dashboard_url=dashboard_url,
        options=options,
    )
    return _ClientProxy(client, opts)  # type: ignore[return-value]


class _ClientProxy:
    """Attribute-forwarding proxy for an MCP client.

    Python's analogue to JS's ``Proxy``. Every attribute access checks
    whether the attribute is one of the wrapped MCP method names; if
    so, returns a policy-checking shim. Otherwise falls through to the
    underlying object.
    """

    _WRAPPED_METHODS = frozenset({
        "call_tool",
        "read_resource",
        "get_prompt",
        "list_tools",
        "list_resources",
        "list_prompts",
    })

    def __init__(self, target: Any, options: McpPolicyOptions) -> None:
        # Use object.__setattr__ to bypass __setattr__ delegation
        # below; we want these on the proxy itself, not on the target.
        object.__setattr__(self, "_checkrd_target", target)
        object.__setattr__(self, "_checkrd_options", options)

    def __getattr__(self, name: str) -> Any:
        target = object.__getattribute__(self, "_checkrd_target")
        attr = getattr(target, name)
        if name not in _ClientProxy._WRAPPED_METHODS:
            return attr
        opts: McpPolicyOptions = object.__getattribute__(
            self, "_checkrd_options",
        )
        return _build_client_shim(attr, name, opts)

    def __setattr__(self, name: str, value: Any) -> None:
        target = object.__getattribute__(self, "_checkrd_target")
        setattr(target, name, value)


def _build_client_shim(
    underlying: Callable[..., Any],
    method_name: str,
    options: McpPolicyOptions,
) -> Callable[..., Any]:
    """Build a policy-checking shim for one client method.

    Auto-detects sync vs async: every official MCP client method is
    async, but the duck-typed approach lets us cover both shapes.
    """
    import inspect

    is_async = inspect.iscoroutinefunction(underlying)

    if method_name == "call_tool":
        kind = "tool"
        name_kw = "name"
    elif method_name == "read_resource":
        kind = "resource"
        name_kw = "uri"
    elif method_name == "get_prompt":
        kind = "prompt"
        name_kw = "name"
    else:
        kind = "list"
        name_kw = ""

    if is_async:

        @functools.wraps(underlying)
        async def async_shim(*args: Any, **kwargs: Any) -> Any:
            target_name, arguments = _extract_target(
                method_name, name_kw, args, kwargs,
            )
            _evaluate_or_raise(
                options,
                method_kind=kind,
                name=target_name,
                arguments=arguments,
            )
            return await underlying(*args, **kwargs)

        return async_shim

    @functools.wraps(underlying)
    def sync_shim(*args: Any, **kwargs: Any) -> Any:
        target_name, arguments = _extract_target(
            method_name, name_kw, args, kwargs,
        )
        _evaluate_or_raise(
            options,
            method_kind=kind,
            name=target_name,
            arguments=arguments,
        )
        return underlying(*args, **kwargs)

    return sync_shim


def _extract_target(
    method_name: str,
    name_kw: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> tuple[str, Any]:
    """Pull the target name + arguments out of the user's call.

    The MCP SDK accepts both positional and keyword forms:
    ``call_tool("search", {"q": "x"})`` and
    ``call_tool(name="search", arguments={"q": "x"})``. We need to
    handle both for telemetry/policy URL building.
    """
    if method_name in ("list_tools", "list_resources", "list_prompts"):
        # No specific target — use the method name suffix so policies
        # can write `url: "**/list/tools"` etc.
        return method_name.replace("list_", ""), None
    if name_kw in kwargs:
        target = kwargs[name_kw]
    elif args:
        target = args[0]
    else:
        target = "unknown"
    arguments = kwargs.get("arguments")
    if arguments is None and len(args) > 1:
        arguments = args[1]
    return str(target), arguments


# ---------------------------------------------------------------------------
# Server wrapping (decorator + Server.set_request_handler)
# ---------------------------------------------------------------------------


def wrap_mcp_server(
    server: T,
    *,
    engine: Optional[WasmEngine] = None,
    enforce: Optional[bool] = None,
    agent_id: Optional[str] = None,
    sink: Optional[TelemetrySink] = None,
    server_name: str = "mcp",
    dashboard_url: Optional[str] = None,
    options: Optional[McpPolicyOptions] = None,
) -> T:
    """Wrap an MCP server so registered handlers are policy-checked.

    Patches the server's request-handler registration entry points
    (``set_request_handler`` for the low-level API, decorator-based
    ``call_tool``/``read_resource``/``get_prompt`` for the FastMCP
    convenience layer). Each registered handler is wrapped so the
    policy engine fires *before* the user's code runs.

    Args:
        server: An MCP server instance. ``mcp.Server`` for the
            low-level API or ``mcp.server.fastmcp.FastMCP`` for the
            high-level convenience layer.
        See :func:`wrap_mcp_client` for the rest of the args.

    Returns:
        The same server, with patched registration methods. Existing
        handlers registered before the wrap are NOT retroactively
        wrapped — call ``wrap_mcp_server`` before registering tools.

    Example (low-level Server)::

        from mcp.server import Server
        srv = wrap_mcp_server(Server("my-server"), agent_id="srv")

        @srv.call_tool()
        async def search(args):
            return ...

    Example (FastMCP)::

        from mcp.server.fastmcp import FastMCP
        mcp = wrap_mcp_server(FastMCP("my-server"), agent_id="srv")

        @mcp.tool()
        async def search(q: str) -> str:
            return ...
    """
    opts = _resolve_options(
        engine=engine,
        enforce=enforce,
        agent_id=agent_id,
        sink=sink,
        server_name=server_name,
        dashboard_url=dashboard_url,
        options=options,
    )
    return _patch_server_handlers(server, opts)


def _patch_server_handlers(server: T, options: McpPolicyOptions) -> T:
    """In-place patch the server's handler registration methods.

    Each registration is replaced with a thin wrapper that re-wraps
    the user-supplied handler with policy enforcement before passing
    it to the original registrar. The MCP SDK's internal handler
    dispatch is unchanged.
    """
    # Low-level Server.set_request_handler(schema, handler).
    # ``getattr``/``setattr`` for monkey-patching: ``hasattr`` narrows
    # attribute existence in mypy but not in pyright, and the
    # behavioural invariant we care about is "the attribute is there
    # at runtime" — exactly what ``getattr(...)`` is for.
    original_set = getattr(server, "set_request_handler", None)
    if original_set is not None:

        @functools.wraps(original_set)
        def patched_set(schema: Any, handler: Callable[..., Any]) -> Any:
            return original_set(schema, _wrap_server_handler(handler, options, "tool"))

        setattr(server, "set_request_handler", patched_set)

    # Low-level Server.call_tool() / read_resource() / get_prompt() — these
    # are decorator factories; calling them returns a decorator the user
    # applies to their handler.
    for method_name, kind in (
        ("call_tool", "tool"),
        ("read_resource", "resource"),
        ("get_prompt", "prompt"),
    ):
        if not hasattr(server, method_name):
            continue
        original_factory = getattr(server, method_name)
        if not callable(original_factory):
            continue
        setattr(
            server,
            method_name,
            _wrap_decorator_factory(original_factory, options, kind),
        )

    # FastMCP convenience: tool() / resource() / prompt() decorators.
    for method_name, kind in (
        ("tool", "tool"),
        ("resource", "resource"),
        ("prompt", "prompt"),
    ):
        if not hasattr(server, method_name):
            continue
        original_factory = getattr(server, method_name)
        if not callable(original_factory):
            continue
        setattr(
            server,
            method_name,
            _wrap_decorator_factory(original_factory, options, kind),
        )

    return server


def _wrap_decorator_factory(
    original_factory: Callable[..., Any],
    options: McpPolicyOptions,
    kind: str,
) -> Callable[..., Any]:
    """Wrap a decorator-factory like ``Server.call_tool()``.

    The factory returns a decorator; we intercept the decorator and
    wrap the handler the user applies it to.
    """

    @functools.wraps(original_factory)
    def patched(*factory_args: Any, **factory_kwargs: Any) -> Callable[..., Any]:
        decorator = original_factory(*factory_args, **factory_kwargs)

        @functools.wraps(decorator)
        def patched_decorator(handler: Callable[..., Any]) -> Any:
            wrapped = _wrap_server_handler(handler, options, kind)
            return decorator(wrapped)

        return patched_decorator

    return patched


def _wrap_server_handler(
    handler: Callable[..., Any],
    options: McpPolicyOptions,
    kind: str,
) -> Callable[..., Any]:
    """Wrap a single server-side handler with policy evaluation."""
    import inspect

    is_async = inspect.iscoroutinefunction(handler)

    if is_async:

        @functools.wraps(handler)
        async def async_wrapped(*args: Any, **kwargs: Any) -> Any:
            target_name, arguments = _extract_server_target(args, kwargs)
            _evaluate_or_raise(
                options,
                method_kind=kind,
                name=target_name,
                arguments=arguments,
            )
            return await handler(*args, **kwargs)

        return async_wrapped

    @functools.wraps(handler)
    def sync_wrapped(*args: Any, **kwargs: Any) -> Any:
        target_name, arguments = _extract_server_target(args, kwargs)
        _evaluate_or_raise(
            options,
            method_kind=kind,
            name=target_name,
            arguments=arguments,
        )
        return handler(*args, **kwargs)

    return sync_wrapped


def _extract_server_target(
    args: tuple[Any, ...], kwargs: dict[str, Any],
) -> tuple[str, Any]:
    """Pull the target name + arguments out of a server handler call.

    The MCP server SDK invokes handlers with the request as the first
    positional arg or via kwargs. We support both shapes for telemetry
    and policy URL building. Unknown shapes degrade gracefully to
    ``("unknown", None)``.
    """
    if args:
        first = args[0]
        # Low-level: first arg is a request object with .params.
        params = getattr(first, "params", None)
        if params is not None:
            name = getattr(params, "name", None) or getattr(params, "uri", None)
            arguments = getattr(params, "arguments", None)
            return str(name) if name is not None else "unknown", arguments
        # FastMCP: first arg is the tool input directly. Use the func's
        # __name__ as the "tool name" — set above when wrapping.
        return "unknown", first
    if kwargs:
        return "unknown", kwargs
    return "unknown", None


# Best-effort: warn if the user imports this module without `mcp`
# installed, so they get a clear message instead of an
# AttributeError later.
try:
    import mcp  # noqa: F401
except ImportError:
    logger.debug(
        "checkrd: 'mcp' not installed; checkrd.mcp helpers will still "
        "import, but require the user to provide an MCP-shaped client "
        "or server object. Install with: pip install mcp",
    )

