"""Deprecation-warning helper for the public API.

Emits a single ``DeprecationWarning`` via the stdlib ``warnings``
module the first time a given deprecated symbol is touched, silent
thereafter. ``CHECKRD_QUIET_DEPRECATIONS=1`` suppresses the warnings
entirely — use in CI environments where deprecation output spam is
undesirable.

Mirrors ``wrappers/javascript/src/_deprecation.ts``.
"""

from __future__ import annotations

import os
import threading
import warnings
from typing import Optional

_seen: set[str] = set()
_lock = threading.Lock()


def _reset_for_tests() -> None:
    """Clear the seen-set. Test-only."""
    with _lock:
        _seen.clear()


def deprecation_warning(
    name: str,
    removed_in_version: str,
    detail: Optional[str] = None,
) -> None:
    """Emit a one-shot ``DeprecationWarning`` for ``name``.

    Subsequent calls with the same ``name`` are no-ops. The warning is
    routed through :func:`warnings.warn` at stacklevel 2 so it points
    at the caller of the deprecated API, not this helper. Suppressed
    entirely under ``CHECKRD_QUIET_DEPRECATIONS=1``.

    Args:
        name: The deprecated symbol name (e.g. ``"wrap(policy_dict=...)"``).
        removed_in_version: The version in which the symbol will be
            removed (e.g. ``"1.0"``).
        detail: Optional extra guidance ("use X instead").
    """
    with _lock:
        if name in _seen:
            return
        _seen.add(name)
    if os.environ.get("CHECKRD_QUIET_DEPRECATIONS") == "1":
        return
    parts = [
        f"checkrd: '{name}' is deprecated and will be removed in "
        f"{removed_in_version}.",
    ]
    if detail:
        parts.append(detail)
    parts.append("Set CHECKRD_QUIET_DEPRECATIONS=1 to silence.")
    warnings.warn(" ".join(parts), DeprecationWarning, stacklevel=2)


__all__ = ["deprecation_warning"]
