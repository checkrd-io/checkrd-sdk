"""Logging filters for the Checkrd SDK.

Two filters are applied to the ``checkrd`` logger:

1. **RateLimitFilter** (Datadog DDLogger pattern): prevents log flooding
   when a subsystem is in a failure loop (control plane down, telemetry
   sends failing). 1 message per ``rate_limit_secs`` per unique call site,
   with ``[N skipped]`` suffix.

2. **SensitiveHeadersFilter** (OpenAI pattern): redacts credential-bearing
   HTTP headers (``Authorization``, ``X-API-Key``, ``Cookie``, etc.) from
   log messages before they reach any handler. Without this, enabling
   ``DEBUG`` logging on the ``httpx`` or ``httpcore`` loggers would leak
   every customer's API keys to log files, stdout, and monitoring systems.

Usage::

    import logging
    from checkrd._logging import RateLimitFilter, SensitiveHeadersFilter

    logger = logging.getLogger("checkrd")
    logger.addFilter(RateLimitFilter(rate_limit_secs=60))

    # Also apply to HTTP libraries that may log raw headers.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).addFilter(SensitiveHeadersFilter())
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import time
from typing import Dict, Tuple


class RateLimitFilter(logging.Filter):
    """Suppress duplicate log messages from the same call site.

    Each unique ``(filename, lineno, levelno)`` gets at most one message
    per ``rate_limit_secs``. Suppressed messages are counted and appended
    as ``[N skipped]`` to the next allowed message from the same site.

    Thread-safe: the internal dict is guarded by a lock.
    """

    def __init__(self, rate_limit_secs: float = 60.0) -> None:
        super().__init__()
        self._rate_limit = rate_limit_secs
        self._lock = threading.Lock()
        # Key: (filename, lineno, levelno) -> (last_allowed_time, skipped_count)
        self._seen: Dict[Tuple[str, int, int], Tuple[float, int]] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        key = (record.pathname, record.lineno, record.levelno)
        now = time.monotonic()

        with self._lock:
            if key in self._seen:
                last_time, skipped = self._seen[key]
                if now - last_time < self._rate_limit:
                    self._seen[key] = (last_time, skipped + 1)
                    return False  # suppress
                # Enough time has passed — allow and append skip count
                if skipped > 0:
                    record.msg = f"{record.msg} [{skipped} skipped]"
                self._seen[key] = (now, 0)
                return True
            else:
                # First occurrence — always allow
                self._seen[key] = (now, 0)
                return True


# Header names whose values must be redacted in log output. Case-insensitive.
# Matches the same set as _SENSITIVE_HEADER_NAMES in transports/_httpx.py.
_REDACT_HEADERS = frozenset({
    "authorization",
    "x-api-key",
    "api-key",
    "cookie",
    "set-cookie",
    "proxy-authorization",
    "x-checkrd-api-key",
})

# Regex that matches header-value patterns in log messages. Catches both
# "Header: value" and "('Header', 'value')" tuple repr formats that
# httpx/httpcore emit at DEBUG level.
_HEADER_NAMES_PATTERN = "|".join(re.escape(h) for h in _REDACT_HEADERS)
_HEADER_VALUE_RE = re.compile(
    r"""
    (?:                          # Match header: value format
        ({names})                # Group 1: header name
        (?:\s*:\s*)              # Separator (colon)
        (.+?)                    # Group 2: value to redact (non-greedy)
        (?=\s*[,\n]|$)          # Stop at comma, newline, or end
    )
    |
    (?:                          # Match ('header', 'value') tuple format
        (['"])({names})\3        # Group 3+4: quoted header name
        \s*,\s*
        (['"])(.+?)\5            # Group 5+6: quoted value to redact
    )
    """.format(names=_HEADER_NAMES_PATTERN),
    re.IGNORECASE | re.VERBOSE,
)


class SensitiveHeadersFilter(logging.Filter):
    """Redact credential-bearing HTTP headers from log messages.

    Follows the OpenAI Python SDK's ``SensitiveHeadersFilter`` pattern:
    intercept log records and replace sensitive header values with
    ``[REDACTED]`` before they reach any handler.

    Applies to both the ``checkrd`` logger and the ``httpx``/``httpcore``
    loggers (which emit raw request/response headers at DEBUG level).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._redact(record.msg)
        if record.args:
            record.args = tuple(
                self._redact(a) if isinstance(a, str) else a
                for a in (record.args if isinstance(record.args, tuple) else (record.args,))
            )
        return True

    @staticmethod
    def _redact(text: str) -> str:
        """Replace sensitive header values with [REDACTED]."""
        def _replacer(m: re.Match) -> str:  # type: ignore[type-arg]
            if m.group(1):
                # "Header: value" format
                return str(m.group(0).replace(m.group(2), "[REDACTED]"))
            else:
                # ('header', 'value') tuple format
                return str(m.group(0).replace(m.group(6), "[REDACTED]"))

        return _HEADER_VALUE_RE.sub(_replacer, text)


# ---------------------------------------------------------------------------
# Debug-mode PII warning
# ---------------------------------------------------------------------------

_debug_warning_emitted = False
_debug_warning_lock = threading.Lock()

_DEBUG_PII_WARNING = (
    "checkrd: DEBUG logging is enabled.\n"
    "  Request/response bodies and prompt payloads MAY appear in logs.\n"
    "  Checkrd's own code redacts credential-bearing headers, but the\n"
    "  `httpx`/`httpcore` libraries do not redact request/response bodies\n"
    "  at DEBUG level. For an LLM agent SDK, that means prompts and\n"
    "  completions — which typically contain customer data — can end up\n"
    "  in stdout/stderr, log files, and any log-shipping pipeline\n"
    "  (journald, CloudWatch, Datadog, Loki, etc.).\n"
    "\n"
    "  DO NOT enable CHECKRD_DEBUG=1 or debug=True in production.\n"
    "  Use it during local development for a single request, then\n"
    "  turn it off. See https://checkrd.io/docs/debug-logging"
)


def warn_debug_pii_risk(*, once: bool = True) -> None:
    """Emit a one-time stderr banner warning about PII risk in debug logs.

    Called from the init/wrap paths when ``CHECKRD_DEBUG=1`` or ``debug=True``
    is observed. Fires once per process by default — a process that
    repeatedly constructs clients should not spam the warning.

    Writes directly to ``sys.stderr`` (not through the checkrd logger)
    because the whole point is that the logger may be routed to a
    destination the operator isn't actively watching, while stderr
    typically lands in the terminal / journald / equivalent where a
    loud banner will actually be seen.

    Args:
        once: When ``True`` (default) emit at most once per process.
            Tests that want to verify the message repeatedly can pass
            ``once=False`` to bypass the guard.
    """
    global _debug_warning_emitted  # noqa: PLW0603
    if once:
        with _debug_warning_lock:
            if _debug_warning_emitted:
                return
            _debug_warning_emitted = True
    # Write direct to stderr — see the docstring for the reason this
    # bypasses the logging framework.
    sys.stderr.write(_DEBUG_PII_WARNING + "\n")
    sys.stderr.flush()


def _reset_debug_warning_for_testing() -> None:
    """Reset the one-shot guard. Testing-only hook, not part of the public API."""
    global _debug_warning_emitted  # noqa: PLW0603
    with _debug_warning_lock:
        _debug_warning_emitted = False
