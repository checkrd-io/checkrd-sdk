"""Anthropic Claude Agent SDK integration for Checkrd.

The Claude Agent SDK exposes hooks on :class:`ClaudeAgentOptions` —
async functions invoked at well-defined points in the agent's run
loop. The supported hook events on the Python SDK (as of April 2026)
are: ``PreToolUse``, ``PostToolUse``, ``PostToolUseFailure``,
``UserPromptSubmit``, ``Stop``, ``SubagentStop``, ``PreCompact``,
``Notification``, ``SubagentStart``, ``PermissionRequest``.

This module ships factory functions that return ``HookCallback``-typed
async functions wired to the Checkrd WASM core:

- :func:`make_pre_tool_use_hook` — evaluate the tool call before
  Claude executes it. Block on deny.
- :func:`make_post_tool_use_hook` — emit telemetry for the tool result.
- :func:`make_user_prompt_submit_hook` — gate the user's prompt before
  Claude reasons about it.
- :func:`make_stop_hook` — emit a final telemetry event when the
  agent finishes.

For convenience :func:`attach_to_options` mutates an existing
:class:`ClaudeAgentOptions` to add Checkrd hooks on the four events
above. Idempotent — calling it twice on the same options does not
add duplicate hooks.

Usage::

    from claude_agent_sdk import ClaudeAgentOptions, query
    from checkrd import Checkrd
    from checkrd.integrations.claude_agent_sdk import attach_to_options

    with Checkrd() as client:
        options = ClaudeAgentOptions(
            hooks={},  # any user-supplied hooks coexist
        )
        attach_to_options(options, client=client)
        async for msg in query(prompt="...", options=options):
            ...

Deny semantics: on deny the hook returns
``{"decision": "block", "systemMessage": <reason>}`` per the SDK's
documented protocol. The ``claude-code`` CLI subprocess interprets
this as "do not run the tool / do not proceed" and reports the
``systemMessage`` back to the agent's reasoning loop.

Reference: https://code.claude.com/docs/en/agent-sdk/python
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional, cast

# Hard imports: this module requires claude-agent-sdk. We import the
# SDK's ``HookCallback`` and ``HookJSONOutput`` type aliases so adapter
# function signatures match the SDK's contract exactly — registering a
# Checkrd hook on ``ClaudeAgentOptions.hooks`` is then type-safe end to end.
from claude_agent_sdk import (
    ClaudeAgentOptions,
    HookCallback,
    HookContext,
    HookJSONOutput,
    HookMatcher,
)
from claude_agent_sdk.types import (
    PostToolUseHookInput,
    PreToolUseHookInput,
    StopHookInput,
    UserPromptSubmitHookInput,
)

from checkrd._state import get_context
from checkrd.engine import EvalResult, WasmEngine
from checkrd.sinks import TelemetrySink

if TYPE_CHECKING:
    from checkrd.client import Checkrd

logger = logging.getLogger("checkrd.integrations.claude_agent_sdk")


# Synthetic URL authority for Claude Agent SDK events.
_AUTHORITY = "claude-agent.local"

# Marker attribute set on hooks we install, used by :func:`attach_to_options`
# to detect prior installation and avoid duplicate registration.
_CHECKRD_INSTALLED_MARKER = "__checkrd_installed__"


# ----------------------------------------------------------------------
# Public factory functions
# ----------------------------------------------------------------------


def make_pre_tool_use_hook(
    *,
    engine: WasmEngine,
    agent_id: str,
    sink: Optional[TelemetrySink] = None,
    enforce: bool = True,
    dashboard_url: Optional[str] = None,
) -> HookCallback:
    """Build a ``PreToolUse`` hook that policy-evaluates each tool call.

    Returns an async function suitable for registration via
    :class:`HookMatcher`. The hook receives the tool name, tool input,
    and session context; evaluates them through the WASM core; and
    returns ``{"decision": "block", ...}`` on deny when ``enforce=True``.

    The synthetic URL is
    ``https://claude-agent.local/tools/<tool_name>`` so policy authors
    write rules like::

        rules:
          - name: deny-bash-rm
            deny:
              url: "claude-agent.local/tools/Bash"
              body:
                command: "*rm -rf*"
    """
    dashboard_base = (dashboard_url or "").rstrip("/")

    async def hook(
        input_data: PreToolUseHookInput,
        tool_use_id: Optional[str],
        context: HookContext,
    ) -> HookJSONOutput:
        tool_name = str(input_data.get("tool_name", "unknown"))
        tool_input = input_data.get("tool_input", {})
        session_id = str(input_data.get("session_id", "")) or ""
        request_id = tool_use_id or session_id

        result = _evaluate(
            engine=engine,
            request_id=request_id,
            kind="tools",
            target=tool_name,
            body_obj={"tool_input": tool_input, "session_id": session_id},
            extra_headers=[
                ("x-claude-agent-tool", tool_name),
                ("x-claude-agent-tool-use-id", tool_use_id or ""),
                ("x-claude-agent-session-id", session_id),
            ],
        )

        # Telemetry first (regardless of decision), so observation-mode
        # operators see what would have been blocked.
        _enqueue_safe(
            sink,
            {
                "event_type": "claude_agent_pre_tool_use",
                "request_id": result.request_id,
                "agent_id": agent_id,
                "tool_name": tool_name,
                "tool_use_id": tool_use_id,
                "session_id": session_id,
                "allowed": result.allowed,
                "deny_reason": result.deny_reason if not result.allowed else None,
            },
        )

        if result.allowed:
            return {}

        if not enforce:
            logger.warning(
                "checkrd: claude-agent tool %s denied (observation mode): %s",
                tool_name,
                result.deny_reason,
            )
            return {}

        message = result.deny_reason or "policy denied"
        if dashboard_base:
            message = f"{message} (dashboard: {dashboard_base}/events/{result.request_id})"
        return {"decision": "block", "systemMessage": message}

    setattr(hook, _CHECKRD_INSTALLED_MARKER, True)
    # Cast to ``HookCallback`` (the SDK's union over every hook input type)
    # so this PreToolUse-specific hook can register on any matcher slot.
    return cast(HookCallback, hook)


def make_post_tool_use_hook(
    *,
    engine: WasmEngine,
    agent_id: str,
    sink: Optional[TelemetrySink] = None,
) -> HookCallback:
    """Build a ``PostToolUse`` hook that emits a telemetry event per tool result.

    Does not block — purely observational. Tool denials happen at
    ``PreToolUse``; this hook captures successful tool outcomes for
    correlation with the matching ``pre_tool_use`` event.
    """

    async def hook(
        input_data: PostToolUseHookInput,
        tool_use_id: Optional[str],
        context: HookContext,
    ) -> HookJSONOutput:
        tool_name = str(input_data.get("tool_name", "unknown"))
        tool_response = input_data.get("tool_response", None)
        session_id = str(input_data.get("session_id", "")) or ""

        _enqueue_safe(
            sink,
            {
                "event_type": "claude_agent_post_tool_use",
                "request_id": tool_use_id or session_id,
                "agent_id": agent_id,
                "tool_name": tool_name,
                "tool_use_id": tool_use_id,
                "session_id": session_id,
                "response_preview": _preview(tool_response),
            },
        )
        return {}

    setattr(hook, _CHECKRD_INSTALLED_MARKER, True)
    return cast(HookCallback, hook)


def make_user_prompt_submit_hook(
    *,
    engine: WasmEngine,
    agent_id: str,
    sink: Optional[TelemetrySink] = None,
    enforce: bool = True,
    dashboard_url: Optional[str] = None,
) -> HookCallback:
    """Build a ``UserPromptSubmit`` hook that policy-evaluates user prompts.

    Useful for blocking known-bad prompts before Claude reasons about
    them (prompt-injection defenses, sensitive-topic blocks, etc.).
    """
    dashboard_base = (dashboard_url or "").rstrip("/")

    async def hook(
        input_data: UserPromptSubmitHookInput,
        tool_use_id: Optional[str],
        context: HookContext,
    ) -> HookJSONOutput:
        prompt = str(input_data.get("prompt", "") or "")
        session_id = str(input_data.get("session_id", "")) or ""

        result = _evaluate(
            engine=engine,
            request_id=session_id,
            kind="prompts",
            target="user-prompt",
            body_obj={"prompt": prompt, "session_id": session_id},
            extra_headers=[
                ("x-claude-agent-session-id", session_id),
            ],
        )

        _enqueue_safe(
            sink,
            {
                "event_type": "claude_agent_user_prompt_submit",
                "request_id": result.request_id,
                "agent_id": agent_id,
                "session_id": session_id,
                "prompt_preview": _preview(prompt),
                "allowed": result.allowed,
                "deny_reason": result.deny_reason if not result.allowed else None,
            },
        )

        if result.allowed:
            return {}

        if not enforce:
            logger.warning(
                "checkrd: claude-agent user prompt denied (observation mode): %s",
                result.deny_reason,
            )
            return {}

        message = result.deny_reason or "policy denied"
        if dashboard_base:
            message = f"{message} (dashboard: {dashboard_base}/events/{result.request_id})"
        return {"decision": "block", "systemMessage": message}

    setattr(hook, _CHECKRD_INSTALLED_MARKER, True)
    return cast(HookCallback, hook)


def make_stop_hook(
    *,
    agent_id: str,
    sink: Optional[TelemetrySink] = None,
) -> HookCallback:
    """Build a ``Stop`` hook that emits a final telemetry event when the agent finishes."""

    async def hook(
        input_data: StopHookInput,
        tool_use_id: Optional[str],
        context: HookContext,
    ) -> HookJSONOutput:
        session_id = str(input_data.get("session_id", "")) or ""
        _enqueue_safe(
            sink,
            {
                "event_type": "claude_agent_stop",
                "request_id": session_id,
                "agent_id": agent_id,
                "session_id": session_id,
            },
        )
        return {}

    setattr(hook, _CHECKRD_INSTALLED_MARKER, True)
    return cast(HookCallback, hook)


# ----------------------------------------------------------------------
# Convenience: attach all hooks to an existing options object
# ----------------------------------------------------------------------


def attach_to_options(
    options: ClaudeAgentOptions,
    *,
    client: Optional["Checkrd"] = None,
    engine: Optional[WasmEngine] = None,
    agent_id: Optional[str] = None,
    sink: Optional[TelemetrySink] = None,
    enforce: bool = True,
    dashboard_url: Optional[str] = None,
    tool_matcher: Optional[str] = None,
) -> ClaudeAgentOptions:
    """Mutate ``options`` to add Checkrd hooks on the four standard events.

    Idempotent: calling this twice on the same options object does not
    add duplicate hooks. User-supplied hooks remain in place — Checkrd
    hooks are appended to the same matchers.

    Args:
        options: The :class:`ClaudeAgentOptions` to mutate. Returned
            for chainable use.
        client: Optional :class:`Checkrd` client. When provided, engine,
            agent_id, sink, enforce, and dashboard_url default from the
            client's runtime.
        engine, agent_id, sink, enforce, dashboard_url: Explicit
            overrides — required when ``client`` is omitted.
        tool_matcher: Optional regex to scope ``PreToolUse`` /
            ``PostToolUse`` hooks (e.g. ``"Bash|Write|Edit"``). Default
            is ``None`` which matches every tool.

    Returns:
        The same ``options`` object, with hooks added.
    """
    if client is not None:
        client._ensure_global_context()
        ctx = get_context()
        engine = engine or ctx.engine
        agent_id = agent_id or ctx.settings.agent_id
        sink = sink if sink is not None else ctx.sink
        enforce = enforce if client is None else ctx.enforce
        dashboard_url = dashboard_url or ctx.settings.dashboard_url or ""

    if engine is None or agent_id is None:
        raise ValueError(
            "attach_to_options requires either client= or both engine= and agent_id=",
        )

    pre_tool = make_pre_tool_use_hook(
        engine=engine,
        agent_id=agent_id,
        sink=sink,
        enforce=enforce,
        dashboard_url=dashboard_url,
    )
    post_tool = make_post_tool_use_hook(
        engine=engine,
        agent_id=agent_id,
        sink=sink,
    )
    prompt_submit = make_user_prompt_submit_hook(
        engine=engine,
        agent_id=agent_id,
        sink=sink,
        enforce=enforce,
        dashboard_url=dashboard_url,
    )
    stop_hook = make_stop_hook(
        agent_id=agent_id,
        sink=sink,
    )

    # The SDK's ``ClaudeAgentOptions.hooks`` is keyed by the ``HookEvent``
    # ``Literal[...]`` of event names. Using ``Any`` here keeps the
    # mutation generic; we narrow back to the SDK's keyspace via the
    # literal-string append calls below, which the type checker accepts
    # because each is one of the allowed Literal values.
    hooks: dict[Any, list[HookMatcher]] = dict(getattr(options, "hooks", None) or {})

    def _append(event: Any, matcher: HookMatcher) -> None:
        existing = hooks.get(event, [])
        # Skip if a hook with the Checkrd marker is already installed
        # for this event — keeps :func:`attach_to_options` idempotent.
        for hm in existing:
            for fn in getattr(hm, "hooks", None) or []:
                if getattr(fn, _CHECKRD_INSTALLED_MARKER, False):
                    return
        hooks[event] = [*existing, matcher]

    _append(
        "PreToolUse",
        HookMatcher(matcher=tool_matcher, hooks=[pre_tool], timeout=30),
    )
    _append(
        "PostToolUse",
        HookMatcher(matcher=tool_matcher, hooks=[post_tool]),
    )
    _append(
        "UserPromptSubmit",
        HookMatcher(hooks=[prompt_submit]),
    )
    _append(
        "Stop",
        HookMatcher(hooks=[stop_hook]),
    )

    options.hooks = hooks
    return options


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _evaluate(
    *,
    engine: WasmEngine,
    request_id: str,
    kind: str,
    target: str,
    body_obj: Any,
    extra_headers: list[tuple[str, str]],
) -> EvalResult:
    url = f"https://{_AUTHORITY}/{kind}/{target}"
    body_json = _safe_json(body_obj)
    now = datetime.now(timezone.utc)
    return engine.evaluate(
        request_id=request_id or "",
        method="POST",
        url=url,
        headers=[("x-claude-agent-kind", kind), *extra_headers],
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
        logger.warning(
            "checkrd: claude-agent telemetry enqueue failed",
            exc_info=True,
        )


def _safe_json(obj: Any) -> str:
    try:
        return json.dumps(
            obj,
            default=lambda o: getattr(o, "model_dump", lambda: str(o))(),
        )
    except (TypeError, ValueError):
        return json.dumps({"_repr": str(obj)})


def _preview(value: Any, *, max_len: int = 256) -> str:
    s = str(value)
    return s if len(s) <= max_len else s[:max_len] + "..."


__all__ = [
    "make_pre_tool_use_hook",
    "make_post_tool_use_hook",
    "make_user_prompt_submit_hook",
    "make_stop_hook",
    "attach_to_options",
    "HookCallback",
]
