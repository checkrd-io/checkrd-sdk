"""Fork-safety primitives for the Checkrd SDK.

Industry pattern, used by asyncpg, psycopg3, and modern observability
SDKs: register an ``os.register_at_fork(after_in_child=...)`` handler
that walks a :class:`weakref.WeakSet` of fork-aware instances and re-
initializes the threading primitives each one owns. Replaces the
earlier per-operation ``_check_fork`` PID check, which added a
``os.getpid()`` syscall to every ``enqueue`` and only caught the
inconsistency once a hot-path operation happened to fire after the
fork.

Why a centralized helper:

- Each module that owns background threads (telemetry batcher, SSE
  control receiver, file watchers) gets the same registration
  boilerplate. Inlining three near-identical copies is a maintenance
  hazard — a fix to one (e.g., logging the right exception class) has
  to be repeated to the others.
- :func:`register_fork_handler` is idempotent at import time. Modules
  call it unconditionally; the underlying ``os.register_at_fork`` is
  Unix-only (Windows uses ``spawn``-based ``multiprocessing`` which
  gets a fresh module load in the child anyway), so the helper
  silently no-ops when the API is missing.

Usage from a module that owns fork-sensitive instances::

    _LIVE: weakref.WeakSet["MyClass"] = weakref.WeakSet()
    register_fork_handler(_LIVE, "_reinit_after_fork", "my-class")

    class MyClass:
        def __init__(self) -> None:
            ...
            _LIVE.add(self)

        def _reinit_after_fork(self) -> None:
            # idempotent: returns early if the PID matches the parent
            ...
"""

from __future__ import annotations

import logging
import os
import weakref
from typing import Any

logger = logging.getLogger("checkrd")


def register_fork_handler(
    registry: "weakref.WeakSet[Any]",
    reset_method: str,
    label: str,
) -> bool:
    """Register an ``os.register_at_fork`` handler that resets every
    instance in ``registry`` in the forked child process.

    Args:
        registry: WeakSet of fork-aware instances. The caller adds
            instances during ``__init__`` and they're removed
            automatically when GC reaps them.
        reset_method: Name of the instance method to call in the child
            after fork. Must accept zero arguments (other than ``self``).
            Typical name: ``_reinit_after_fork``.
        label: Short human-readable string for log messages identifying
            which subsystem failed if a reset raises (``"telemetry
            batcher"``, ``"control receiver"``, etc.).

    Returns:
        ``True`` if the handler was registered (Unix-like platforms).
        ``False`` on platforms without ``os.register_at_fork`` such as
        Windows; spawn-based multiprocessing on those platforms gets a
        fresh module load in the child, so no handler is needed.

    The function is safe to call at module import. Multiple registrations
    against the same ``registry`` would create duplicate handlers — call
    it exactly once per registry, at module top level.
    """
    register_at_fork = getattr(os, "register_at_fork", None)
    if register_at_fork is None:
        return False

    def _after_fork_in_child() -> None:
        # `list(registry)` materializes a stable snapshot before we
        # iterate — even though the child has only one thread at this
        # point, an instance could be GC'd mid-iteration if its only
        # remaining strong reference happens to be a freed local.
        for instance in list(registry):
            reset = getattr(instance, reset_method, None)
            if reset is None:
                logger.error(
                    "checkrd: %s instance is missing %s; cannot reset after fork",
                    label,
                    reset_method,
                )
                continue
            try:
                reset()
            except Exception:
                # Never let a single instance's reset failure block
                # the others. The child process is about to run user
                # code; partial recovery is better than none.
                logger.exception(
                    "checkrd: error re-initializing %s after fork",
                    label,
                )

    register_at_fork(after_in_child=_after_fork_in_child)
    return True
