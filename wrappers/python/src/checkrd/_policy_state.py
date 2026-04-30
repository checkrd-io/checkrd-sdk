"""Persistent policy bundle for cross-restart enforcement (OPA pattern).

The Checkrd WASM core enforces a strict-greater monotonic version check
on every signed policy bundle install: the new bundle's ``version`` must
be strictly greater than the highest version installed in this engine
instance. This defends against an attacker who captures an older,
signed-but-stale bundle and replays it as a control-plane update — the
older bundle is rejected as a rollback attempt.

The check is in-memory only inside the WASM core, which means a process
restart resets the high water mark to 0. To close that hole *and* let
the SDK enforce policy from the very first request after restart (no
"empty engine" window before SSE init lands), this module persists the
**entire signed bundle envelope** to disk and re-installs it on startup.
The same OPA Bundle Services + TUF client pattern: hot-reloadable cache
with strict freshness + signature checks on load.

# Wire format

The state file is a small JSON document at
``$CHECKRD_CONFIG_DIR/policy_state.json`` (default
``~/.checkrd/policy_state.json``):

```json
{
    "schema_version": 1,
    "last_policy_version": 42,
    "last_policy_hash": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "bundle_envelope_json": "{\"payload\":\"…\",\"payloadType\":\"…\",\"signatures\":[…]}",
    "updated_at": 1712345678
}
```

``schema_version`` is the on-disk format version. Files with a schema
this loader doesn't recognize are treated as missing — the next install
rewrites at the current schema. Future incompatible changes bump this
field.

``last_policy_hash`` is the SHA-256 hex digest of the bundle's verified
yaml payload (matches the server's ``active_policy_hash``).

``bundle_envelope_json`` is the verbatim DSSE envelope JSON the engine
verified at last install. Stored as a string (not nested JSON) so the
wrapper hands the same bytes back to ``reload_policy_signed`` on the
next start — any deserialize/reserialize round-trip risks corrupting
the canonicalization the DSSE signature was computed over. The signature
is re-verified against the current trust list on every load, so a
tampered file is caught immediately.

# Atomic write

The file is written via the canonical POSIX atomic-rename pattern:

1. Write the new contents to a sibling temp file in the same directory.
2. ``fsync`` the temp file so the bytes hit stable storage
   (`fsync(2)`, ext4 journaling docs).
3. ``os.replace`` the temp file over the target. POSIX `rename(2)`
   guarantees this is atomic on the same filesystem.
4. ``fsync`` the parent directory so the new directory entry hits
   stable storage. Without step 4, after a power failure the rename can
   be present in cache but absent on disk; the file would atomically
   roll back to the old contents but the directory entry could be
   either old or new (`man 2 rename`, Theodore Ts'o's "Don't fear the
   fsync" — LWN.net 2009).

This is the same write-fsync-rename-fsync sequence used by Git
(``refs.c::commit_ref``), SQLite's WAL checkpoint, LMDB's MDB_txn, and
etcd's Bolt backend. Crash-consistent: a reader after any crash sees
either the complete old file or the complete new file, never a
half-written or absent one.

# Cross-process correctness

A single SDK process is the typical case. Multiple SDK processes sharing
the same config directory is supported but rare; the file holds the
maximum version any of them has seen. If two processes race to update
the file, the later writer wins — but the in-memory check inside each
process's WASM core continues to enforce monotonicity from whatever
value that process loaded at startup, so neither process can be tricked
into installing an older bundle.
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

POLICY_STATE_SCHEMA_VERSION = 1

# Maximum value the WASM core's `last_policy_version` can hold (u64::MAX).
# A persisted file with a value larger than this would overflow when fed
# back into the FFI, so we treat it as corruption and ignore it.
_U64_MAX = 2**64 - 1

# Hard cap on the envelope JSON we'll persist or load. Real bundles are
# < 100 KB; 4 MB is generous enough to cover any sane future growth in
# rule count while bounding the disk + memory blast radius if the file
# is corrupted or maliciously enlarged. Mirrors the WASM core's own
# `_MAX_SSE_EVENT_BYTES` (10 MB) on the conservative side — we don't
# need to accept anything larger than the SSE wire would carry.
_MAX_PERSISTED_ENVELOPE_BYTES = 4 * 1024 * 1024


def _default_state_path() -> Path:
    """Path to the policy state file.

    Mirrors the override pattern used by ``LocalIdentity``: respects
    ``CHECKRD_CONFIG_DIR`` so tests and dev environments can sandbox
    state into a tmp dir without touching the user's home directory.
    """
    override = os.environ.get("CHECKRD_CONFIG_DIR")
    if override:
        return Path(override) / "policy_state.json"
    return Path.home() / ".checkrd" / "policy_state.json"


def load_persisted_state(
    path: Optional[Path] = None,
) -> tuple[int, Optional[str], Optional[str]]:
    """Read the persisted ``(version, hash, envelope_json)`` from a previous run.

    Returns ``(0, None, None)`` when the file is absent, unreadable,
    corrupt, or carries a schema this loader doesn't recognize. That
    result is the same shape as a brand-new install — the SDK's first
    signed bundle goes through the FFI normally and the rollback
    defense rebuilds from there.

    Per-field semantics:

      - ``version`` — last engine ``last_policy_version`` written.
      - ``hash`` — SHA-256 of the bundle's yaml payload (server-computed
        ``active_policy_hash``). Match against incoming hash to short-
        circuit the FFI install on idempotent re-delivery (OPA bundle /
        TUF "don't re-apply unchanged" pattern).
      - ``envelope_json`` — verbatim DSSE envelope JSON of the last
        verified bundle. Hand this back to ``reload_policy_signed`` on
        startup so the engine has rules from the first request, not
        only after SSE init lands. Re-verified against the current
        trust list on load — a tampered file fails signature check and
        is discarded.

    The function never raises — persistence is best-effort.
    """
    state_path = path or _default_state_path()
    try:
        if not state_path.exists():
            return (0, None, None)
        # Cap the read at our envelope budget plus enough headroom for
        # the metadata keys. Reading without a cap means a corrupt or
        # malicious file could OOM the process before the JSON parser
        # ever runs.
        size = state_path.stat().st_size
        if size > _MAX_PERSISTED_ENVELOPE_BYTES + 65_536:
            logger.warning(
                "checkrd: policy_state.json is %d bytes (cap %d); "
                "treating as corrupt and starting fresh",
                size, _MAX_PERSISTED_ENVELOPE_BYTES + 65_536,
            )
            return (0, None, None)
        contents = state_path.read_text(encoding="utf-8")
        data = json.loads(contents)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "checkrd: could not read policy_state.json (%s); "
            "starting fresh",
            exc,
        )
        return (0, None, None)

    if not isinstance(data, dict):
        logger.warning(
            "checkrd: policy_state.json is not a JSON object; starting fresh"
        )
        return (0, None, None)
    schema = data.get("schema_version")
    if schema != POLICY_STATE_SCHEMA_VERSION:
        # Future-format file written by a newer SDK (or a corrupt /
        # unrecognized schema). Treat as missing; the next install
        # rewrites at the current schema.
        logger.info(
            "checkrd: policy_state.json schema_version=%r — current "
            "version is %d; will reinitialize from server",
            schema, POLICY_STATE_SCHEMA_VERSION,
        )
        return (0, None, None)

    version = data.get("last_policy_version")
    if not isinstance(version, int) or isinstance(version, bool):
        logger.warning(
            "checkrd: policy_state.json last_policy_version=%r is not an "
            "integer; starting fresh",
            version,
        )
        return (0, None, None)
    if version < 0 or version > _U64_MAX:
        logger.warning(
            "checkrd: policy_state.json last_policy_version=%r is out of "
            "the u64 range [0, 2^64); starting fresh",
            version,
        )
        return (0, None, None)

    raw_hash = data.get("last_policy_hash")
    if (
        not isinstance(raw_hash, str)
        or len(raw_hash) != 64
        or any(c not in "0123456789abcdef" for c in raw_hash)
    ):
        logger.warning(
            "checkrd: policy_state.json last_policy_hash is not a 64-char "
            "lowercase hex string; starting fresh"
        )
        return (0, None, None)

    raw_envelope = data.get("bundle_envelope_json")
    if not isinstance(raw_envelope, str):
        logger.warning(
            "checkrd: policy_state.json bundle_envelope_json is not a string; "
            "starting fresh"
        )
        return (0, None, None)
    if len(raw_envelope.encode("utf-8")) > _MAX_PERSISTED_ENVELOPE_BYTES:
        logger.warning(
            "checkrd: policy_state.json bundle_envelope_json exceeds %d byte "
            "cap; starting fresh",
            _MAX_PERSISTED_ENVELOPE_BYTES,
        )
        return (0, None, None)
    # Light validation that the envelope is a JSON object — full
    # signature + freshness + monotonicity checks happen in the WASM
    # core when the wrapper hands it to ``reload_policy_signed``. We
    # don't need to repeat those here; we just want to fail fast on
    # syntactically-broken JSON before the FFI call.
    try:
        envelope_obj = json.loads(raw_envelope)
    except json.JSONDecodeError as exc:
        logger.warning(
            "checkrd: policy_state.json bundle_envelope_json is not valid "
            "JSON (%s); starting fresh",
            exc,
        )
        return (0, None, None)
    if not isinstance(envelope_obj, dict):
        logger.warning(
            "checkrd: policy_state.json bundle_envelope_json is not a JSON "
            "object; starting fresh"
        )
        return (0, None, None)

    return (version, raw_hash, raw_envelope)


def persist_state(
    version: int,
    bundle_hash: str,
    bundle_envelope_json: str,
    path: Optional[Path] = None,
) -> None:
    """Atomically write the policy version + hash + envelope to disk.

    Implements the canonical POSIX write-fsync-rename-fsync pattern:

        1. Write the new contents to a sibling temp file.
        2. ``fsync`` the temp file (durable bytes).
        3. ``os.replace`` the temp file over the target (atomic rename).
        4. ``fsync`` the parent directory (durable directory entry).

    Step 4 is the one most homegrown atomic-write helpers miss; without
    it, the rename can be cached but the directory entry isn't, so a
    power loss between rename and the next directory writeback leaves
    the file pointing at the old inode. Theodore Ts'o documented this
    in the ext4 delayed allocation discussion (LWN.net 2009), and
    `man 2 fsync` calls it out explicitly.

    This is the same sequence Git uses in ``refs.c::commit_ref``,
    SQLite's WAL checkpoint, LMDB's MDB_txn commit, and etcd's Bolt
    backend. Crash-consistent: a reader after any crash sees either the
    complete old file or the complete new file.

    File mode: ``tempfile.mkstemp`` creates the temp file as ``0600``
    (owner read/write only). ``os.replace`` preserves that mode on the
    destination, so the on-disk artifact is owner-private — matching
    OPA's ``/var/opa/`` and TUF clients' ``trusted_root.json``.

    Persistence failures are logged as warnings but never raised: if
    the disk is full or the filesystem is read-only, we lose
    cross-restart rollback protection but the in-process protection
    still works. This is the same fail-open posture used for telemetry
    delivery.
    """
    if len(bundle_envelope_json.encode("utf-8")) > _MAX_PERSISTED_ENVELOPE_BYTES:
        # Refuse to write a pathologically large envelope: the loader
        # would reject it on next read anyway, and writing it just
        # wastes disk + risks log noise on every restart. The
        # in-process rollback defense is unaffected.
        logger.warning(
            "checkrd: bundle envelope is %d bytes (cap %d); skipping "
            "persistence — restore will fall through to fresh fetch",
            len(bundle_envelope_json.encode("utf-8")),
            _MAX_PERSISTED_ENVELOPE_BYTES,
        )
        return

    state_path = path or _default_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps({
        "schema_version": POLICY_STATE_SCHEMA_VERSION,
        "last_policy_version": version,
        "last_policy_hash": bundle_hash,
        "bundle_envelope_json": bundle_envelope_json,
        "updated_at": int(time.time()),
    })

    temp_fd, temp_path = tempfile.mkstemp(
        dir=str(state_path.parent),
        prefix=".policy_state_",
        suffix=".json.tmp",
    )
    try:
        os.write(temp_fd, payload.encode("utf-8"))
        os.fsync(temp_fd)
        os.close(temp_fd)
        # Atomic on POSIX: target is either the old contents or the
        # new contents, never a partial mix. On Windows, os.replace is
        # atomic since Python 3.3.
        os.replace(temp_path, state_path)
        _fsync_parent_dir(state_path)
    except OSError as exc:
        logger.warning(
            "checkrd: could not persist policy_state.json (%s); "
            "rollback defense will not survive restart",
            exc,
        )
        # Best-effort cleanup of the temp file.
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def _fsync_parent_dir(state_path: Path) -> None:
    """Fsync the parent directory so the rename's directory entry is durable.

    POSIX `rename(2)` is atomic but the new directory entry can sit in
    cache for an arbitrary amount of time before being written back. A
    power failure between the rename and the next directory writeback
    can leave the directory pointing at the old inode even though the
    new file's contents are durable. This is the standard fix called
    out in Theodore Ts'o's "Don't fear the fsync" (LWN.net 2009) and in
    `man 2 fsync`.

    Best-effort: not all platforms support fsync on a directory fd
    (Windows raises ``PermissionError``; some FUSE filesystems return
    ``EINVAL``). On those platforms we silently skip the dir-fsync
    step. The atomic rename itself still provides crash safety for the
    file contents; the only thing the dir-fsync adds is durability of
    the directory entry across power loss.
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
