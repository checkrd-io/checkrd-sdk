"""Persistent pricing-bundle version for cross-restart rollback defense (M-12).

The cost-metering analogue of :mod:`checkrd._policy_state`. The Checkrd WASM
core enforces a strict-greater monotonic version check on every signed *pricing*
bundle install: the new bundle's ``version`` must be strictly greater than the
highest version installed in this engine instance. This defends against an
attacker who captures an older, signed-but-stale price table and replays it as a
control-plane update — a rollback to a cheaper-looking (or differently-priced)
historical table is rejected.

The check is in-memory only inside the WASM core, so a process restart resets
the high water mark to 0. To close that hole, this module persists the highest
installed ``last_pricing_version`` to disk and feeds it back through
``set_initial_pricing_version`` on the next boot — BEFORE any signed pricing
reload — so the price-table rollback defense survives restarts.

# Why version-only (no envelope)

Unlike :mod:`checkrd._policy_state`, this module persists only the version, not
the signed envelope. The policy path re-installs the persisted envelope so the
engine has *enforcement rules* from the very first request after restart (a
fail-closed concern — an empty policy would deny everything). Cost metering has
no such urgency: a missing price table is fail-OPEN (``pricing_status =
"disabled"``, cost 0), so there is no "must have a table from request one"
requirement. The price table is delivered fresh by the control plane after
connect, and ``set_initial_pricing_version`` only takes a *version* — it cannot
accept an envelope. Persisting the version alone is exactly what the rollback
defense needs and nothing more (smallest blast radius on disk).

# Wire format

A small JSON document at ``$CHECKRD_CONFIG_DIR/pricing_state.json`` (default
``~/.checkrd/pricing_state.json``):

```json
{
    "schema_version": 1,
    "last_pricing_version": 42,
    "updated_at": 1712345678
}
```

``schema_version`` is the on-disk format version. Files with a schema this
loader doesn't recognize are treated as missing — the next install rewrites at
the current schema. Future incompatible changes bump this field.

# Atomic write

Same canonical POSIX write-fsync-rename-fsync sequence as
:mod:`checkrd._policy_state` (and Git's ``refs.c::commit_ref``, SQLite's WAL
checkpoint, LMDB's MDB_txn, etcd's Bolt backend):

1. Write the new contents to a sibling temp file in the same directory.
2. ``fsync`` the temp file (durable bytes).
3. ``os.replace`` the temp file over the target (atomic rename on POSIX).
4. ``fsync`` the parent directory (durable directory entry).

Crash-consistent: a reader after any crash sees either the complete old file or
the complete new file, never a half-written or absent one. A corrupt /
unreadable / unrecognized-schema file is treated as missing, so a bad state file
never prevents the SDK from starting — it just falls back to the in-process
defense from version 0.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("checkrd")

PRICING_STATE_SCHEMA_VERSION = 1

# Maximum value the WASM core's `last_pricing_version` can hold (u64::MAX).
# A persisted file with a value larger than this would overflow when fed back
# into the FFI, so we treat it as corruption and ignore it.
_U64_MAX = 2**64 - 1


def _default_state_path() -> Path:
    """Path to the pricing state file.

    Mirrors :func:`checkrd._policy_state._default_state_path`: respects
    ``CHECKRD_CONFIG_DIR`` so tests and dev environments can sandbox state into
    a tmp dir without touching the user's home directory.
    """
    override = os.environ.get("CHECKRD_CONFIG_DIR")
    if override:
        return Path(override) / "pricing_state.json"
    return Path.home() / ".checkrd" / "pricing_state.json"


def load_persisted_pricing_version(path: Optional[Path] = None) -> int:
    """Read the persisted ``last_pricing_version`` from a previous run.

    Returns ``0`` when the file is absent, unreadable, corrupt, or carries a
    schema this loader doesn't recognize — the same result as a brand-new
    install, where the rollback defense rebuilds from the first signed bundle.

    The function never raises — persistence is best-effort. The caller feeds the
    returned value to ``set_initial_pricing_version`` before any signed reload.
    """
    state_path = path or _default_state_path()
    try:
        if not state_path.exists():
            return 0
        contents = state_path.read_text(encoding="utf-8")
        data = json.loads(contents)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "checkrd: could not read pricing_state.json (%s); starting fresh",
            exc,
        )
        return 0

    if not isinstance(data, dict):
        logger.warning("checkrd: pricing_state.json is not a JSON object; starting fresh")
        return 0
    schema = data.get("schema_version")
    if schema != PRICING_STATE_SCHEMA_VERSION:
        # Future-format file written by a newer SDK (or a corrupt /
        # unrecognized schema). Treat as missing; the next install rewrites at
        # the current schema.
        logger.info(
            "checkrd: pricing_state.json schema_version=%r — current version "
            "is %d; will reinitialize from server",
            schema,
            PRICING_STATE_SCHEMA_VERSION,
        )
        return 0

    version = data.get("last_pricing_version")
    if not isinstance(version, int) or isinstance(version, bool):
        logger.warning(
            "checkrd: pricing_state.json last_pricing_version=%r is not an integer; starting fresh",
            version,
        )
        return 0
    if version < 0 or version > _U64_MAX:
        logger.warning(
            "checkrd: pricing_state.json last_pricing_version=%r is out of the "
            "u64 range [0, 2^64); starting fresh",
            version,
        )
        return 0

    return version


def persist_pricing_version(version: int, path: Optional[Path] = None) -> None:
    """Atomically write the pricing-bundle version high water mark to disk.

    Implements the canonical POSIX write-fsync-rename-fsync pattern (identical
    to :func:`checkrd._policy_state.persist_state`):

        1. Write the new contents to a sibling temp file.
        2. ``fsync`` the temp file (durable bytes).
        3. ``os.replace`` the temp file over the target (atomic rename).
        4. ``fsync`` the parent directory (durable directory entry).

    File mode: ``tempfile.mkstemp`` creates the temp file as ``0600`` (owner
    read/write only); ``os.replace`` preserves that mode on the destination, so
    the on-disk artifact is owner-private.

    Persistence failures are logged as warnings but never raised: if the disk is
    full or the filesystem is read-only, cross-restart rollback protection is
    lost but the in-process protection still works — the same fail-open posture
    used for telemetry delivery.
    """
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version < 0
        or version > _U64_MAX
    ):
        # Refuse to write an out-of-range or non-int version: the loader would
        # reject it on next read anyway. The in-process rollback defense is
        # unaffected.
        logger.warning(
            "checkrd: refusing to persist invalid pricing version %r; "
            "rollback defense will not survive restart",
            version,
        )
        return

    state_path = path or _default_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(
        {
            "schema_version": PRICING_STATE_SCHEMA_VERSION,
            "last_pricing_version": version,
            "updated_at": int(time.time()),
        }
    )

    temp_fd, temp_path = tempfile.mkstemp(
        dir=str(state_path.parent),
        prefix=".pricing_state_",
        suffix=".json.tmp",
    )
    try:
        os.write(temp_fd, payload.encode("utf-8"))
        os.fsync(temp_fd)
        os.close(temp_fd)
        # Atomic on POSIX: target is either the old contents or the new
        # contents, never a partial mix. On Windows, os.replace is atomic since
        # Python 3.3.
        os.replace(temp_path, state_path)
        _fsync_parent_dir(state_path)
    except OSError as exc:
        logger.warning(
            "checkrd: could not persist pricing_state.json (%s); rollback "
            "defense will not survive restart",
            exc,
        )
        # Best-effort cleanup of the temp file.
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def _fsync_parent_dir(state_path: Path) -> None:
    """Fsync the parent directory so the rename's directory entry is durable.

    POSIX ``rename(2)`` is atomic but the new directory entry can sit in cache
    before being written back; a power failure between the rename and the next
    directory writeback can leave the directory pointing at the old inode even
    though the new file's contents are durable. Standard fix (Theodore Ts'o,
    "Don't fear the fsync", LWN.net 2009; ``man 2 fsync``).

    Best-effort: Windows raises ``PermissionError`` and some FUSE filesystems
    return ``EINVAL`` on a directory-fd fsync; on those we silently skip it. The
    atomic rename still provides crash safety for the file contents.
    """
    try:
        dir_fd = os.open(str(state_path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        # PermissionError on Windows, EINVAL on some FUSE mounts.
        pass
    finally:
        os.close(dir_fd)
