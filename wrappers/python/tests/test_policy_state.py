"""Tests for ``checkrd._policy_state``: persistent policy state.

Covers the cross-restart half of the rollback defense + the OPA-style
hot reload at startup:

- ``persist_state`` writes ``(version, hash, envelope)`` atomically
  (the same write-fsync-rename-fsync sequence used by Git's HEAD,
  SQLite's WAL, etcd's Bolt backend).
- ``load_persisted_state`` returns the persisted triple, or
  ``(0, None, None)`` on any read / parse / validation failure.
  Returning all-zeros is the safe fallback — the next signed bundle
  installs into a clean engine, the persisted file is rewritten, and
  the SDK never refuses to start because of a bad state file.
- A simulated disk crash during ``persist_state`` (patched
  ``os.replace``) must not raise — persistence is best-effort.

The end-to-end "rollback survives restart" property is exercised in
``test_control.py`` via the receiver's ``start()`` path; this file
hits the persistence primitive in isolation so failures point at the
right module.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

from checkrd._policy_state import (
    POLICY_STATE_SCHEMA_VERSION,
    load_persisted_state,
    persist_state,
)

# Sentinel envelope JSON used as the third field on every persist_state
# call. The value must be a syntactically-valid JSON object — the loader
# parses it to fail fast on garbled data — but the wrapper never asks
# the WASM core to verify it during these tests.
_FAKE_ENVELOPE = json.dumps({
    "payloadType": "application/vnd.checkrd.policy-bundle+yaml",
    "payload": "ZHVtbXk=",
    "signatures": [{"keyid": "test", "sig": "AA"}],
})
_FAKE_HASH = "a" * 64


# ============================================================
# Round-trip
# ============================================================


def test_round_trip_preserves_state(tmp_path: Path) -> None:
    state = tmp_path / "policy_state.json"
    persist_state(42, _FAKE_HASH, _FAKE_ENVELOPE, path=state)
    assert load_persisted_state(state) == (42, _FAKE_HASH, _FAKE_ENVELOPE)


def test_round_trip_with_zero_version(tmp_path: Path) -> None:
    """Version 0 is allowed (the engine treats it as "no prior install")."""
    state = tmp_path / "policy_state.json"
    persist_state(0, _FAKE_HASH, _FAKE_ENVELOPE, path=state)
    assert load_persisted_state(state) == (0, _FAKE_HASH, _FAKE_ENVELOPE)


def test_round_trip_with_large_version(tmp_path: Path) -> None:
    """The schema uses u64 on the WASM side; large versions must round-trip."""
    state = tmp_path / "policy_state.json"
    persist_state(2**32 + 7, _FAKE_HASH, _FAKE_ENVELOPE, path=state)
    assert load_persisted_state(state) == (2**32 + 7, _FAKE_HASH, _FAKE_ENVELOPE)


def test_overwrite_replaces_old_value(tmp_path: Path) -> None:
    """Sequential writes reflect the most recent value."""
    state = tmp_path / "policy_state.json"
    persist_state(10, _FAKE_HASH, _FAKE_ENVELOPE, path=state)
    persist_state(11, _FAKE_HASH, _FAKE_ENVELOPE, path=state)
    persist_state(15, _FAKE_HASH, _FAKE_ENVELOPE, path=state)
    assert load_persisted_state(state) == (15, _FAKE_HASH, _FAKE_ENVELOPE)


# ============================================================
# load_persisted_state: degraded inputs
# ============================================================


def test_load_returns_empty_when_file_missing(tmp_path: Path) -> None:
    """The expected case on a freshly-installed SDK."""
    state = tmp_path / "policy_state.json"
    assert load_persisted_state(state) == (0, None, None)


def test_load_returns_empty_for_malformed_json(tmp_path: Path) -> None:
    state = tmp_path / "policy_state.json"
    state.write_text("{this is not valid json")
    assert load_persisted_state(state) == (0, None, None)


def test_load_returns_empty_for_non_dict_root(tmp_path: Path) -> None:
    state = tmp_path / "policy_state.json"
    state.write_text("[1, 2, 3]")
    assert load_persisted_state(state) == (0, None, None)


def test_load_returns_empty_for_unknown_schema(tmp_path: Path) -> None:
    """Forward-compat: a file from a newer SDK with a bumped schema is
    treated as missing, not crashed on. The next install rewrites it
    at the schema this SDK understands."""
    state = tmp_path / "policy_state.json"
    state.write_text(
        json.dumps({
            "schema_version": 999,
            "last_policy_version": 50,
            "last_policy_hash": _FAKE_HASH,
            "bundle_envelope_json": _FAKE_ENVELOPE,
            "updated_at": 0,
        })
    )
    assert load_persisted_state(state) == (0, None, None)


def test_load_returns_empty_for_missing_envelope(tmp_path: Path) -> None:
    """A file without the envelope can't safely seed the engine — the
    cache hit on init would short-circuit FFI and leave the engine
    empty. Treat as missing."""
    state = tmp_path / "policy_state.json"
    state.write_text(
        json.dumps({
            "schema_version": POLICY_STATE_SCHEMA_VERSION,
            "last_policy_version": 50,
            "last_policy_hash": _FAKE_HASH,
            "updated_at": 0,
        })
    )
    assert load_persisted_state(state) == (0, None, None)


def test_load_returns_empty_for_missing_hash(tmp_path: Path) -> None:
    state = tmp_path / "policy_state.json"
    state.write_text(
        json.dumps({
            "schema_version": POLICY_STATE_SCHEMA_VERSION,
            "last_policy_version": 50,
            "bundle_envelope_json": _FAKE_ENVELOPE,
            "updated_at": 0,
        })
    )
    assert load_persisted_state(state) == (0, None, None)


def test_load_returns_empty_for_invalid_hash_format(tmp_path: Path) -> None:
    """``last_policy_hash`` must be 64 lowercase hex chars; anything
    else (uppercase, wrong length, non-hex) is treated as corruption."""
    state = tmp_path / "policy_state.json"
    state.write_text(
        json.dumps({
            "schema_version": POLICY_STATE_SCHEMA_VERSION,
            "last_policy_version": 50,
            "last_policy_hash": "NOT-A-HEX-DIGEST",
            "bundle_envelope_json": _FAKE_ENVELOPE,
            "updated_at": 0,
        })
    )
    assert load_persisted_state(state) == (0, None, None)


def test_load_returns_empty_for_envelope_not_string(tmp_path: Path) -> None:
    state = tmp_path / "policy_state.json"
    state.write_text(
        json.dumps({
            "schema_version": POLICY_STATE_SCHEMA_VERSION,
            "last_policy_version": 50,
            "last_policy_hash": _FAKE_HASH,
            "bundle_envelope_json": {"not": "a string"},  # wrong type
            "updated_at": 0,
        })
    )
    assert load_persisted_state(state) == (0, None, None)


def test_load_returns_empty_for_envelope_not_json(tmp_path: Path) -> None:
    state = tmp_path / "policy_state.json"
    state.write_text(
        json.dumps({
            "schema_version": POLICY_STATE_SCHEMA_VERSION,
            "last_policy_version": 50,
            "last_policy_hash": _FAKE_HASH,
            "bundle_envelope_json": "this is not JSON",
            "updated_at": 0,
        })
    )
    assert load_persisted_state(state) == (0, None, None)


def test_load_returns_empty_for_negative_version(tmp_path: Path) -> None:
    state = tmp_path / "policy_state.json"
    state.write_text(
        json.dumps({
            "schema_version": POLICY_STATE_SCHEMA_VERSION,
            "last_policy_version": -1,
            "last_policy_hash": _FAKE_HASH,
            "bundle_envelope_json": _FAKE_ENVELOPE,
            "updated_at": 0,
        })
    )
    assert load_persisted_state(state) == (0, None, None)


def test_load_returns_empty_for_non_int_version(tmp_path: Path) -> None:
    state = tmp_path / "policy_state.json"
    state.write_text(
        json.dumps({
            "schema_version": POLICY_STATE_SCHEMA_VERSION,
            "last_policy_version": "not-an-int",
            "last_policy_hash": _FAKE_HASH,
            "bundle_envelope_json": _FAKE_ENVELOPE,
            "updated_at": 0,
        })
    )
    assert load_persisted_state(state) == (0, None, None)


def test_load_returns_empty_for_bool_version(tmp_path: Path) -> None:
    """Python ``bool`` is a subclass of ``int`` — explicitly reject it
    so a malformed file with ``"last_policy_version": true`` cannot
    install a "version 1" floor."""
    state = tmp_path / "policy_state.json"
    state.write_text(
        json.dumps({
            "schema_version": POLICY_STATE_SCHEMA_VERSION,
            "last_policy_version": True,
            "last_policy_hash": _FAKE_HASH,
            "bundle_envelope_json": _FAKE_ENVELOPE,
            "updated_at": 0,
        })
    )
    assert load_persisted_state(state) == (0, None, None)


def test_load_returns_empty_for_u64_overflow(tmp_path: Path) -> None:
    state = tmp_path / "policy_state.json"
    state.write_text(
        json.dumps({
            "schema_version": POLICY_STATE_SCHEMA_VERSION,
            "last_policy_version": 2**64,  # one past u64 max
            "last_policy_hash": _FAKE_HASH,
            "bundle_envelope_json": _FAKE_ENVELOPE,
            "updated_at": 0,
        })
    )
    assert load_persisted_state(state) == (0, None, None)


def test_load_accepts_u64_max(tmp_path: Path) -> None:
    """The largest valid u64 value must round-trip exactly."""
    state = tmp_path / "policy_state.json"
    state.write_text(
        json.dumps({
            "schema_version": POLICY_STATE_SCHEMA_VERSION,
            "last_policy_version": 2**64 - 1,
            "last_policy_hash": _FAKE_HASH,
            "bundle_envelope_json": _FAKE_ENVELOPE,
            "updated_at": 0,
        })
    )
    version, h, env = load_persisted_state(state)
    assert version == 2**64 - 1
    assert h == _FAKE_HASH
    assert env == _FAKE_ENVELOPE


def test_load_returns_empty_when_oversized(tmp_path: Path) -> None:
    """Files exceeding the envelope cap + metadata headroom are treated
    as corrupt before the JSON parser runs."""
    state = tmp_path / "policy_state.json"
    # Write a real-looking JSON file padded out past the cap.
    huge_envelope = json.dumps({"payload": "A" * (5 * 1024 * 1024)})
    state.write_text(
        json.dumps({
            "schema_version": POLICY_STATE_SCHEMA_VERSION,
            "last_policy_version": 1,
            "last_policy_hash": _FAKE_HASH,
            "bundle_envelope_json": huge_envelope,
            "updated_at": 0,
        })
    )
    assert load_persisted_state(state) == (0, None, None)


# ============================================================
# persist_state: atomicity and resilience
# ============================================================


def test_persist_creates_parent_directory(tmp_path: Path) -> None:
    """The state directory is auto-created on first write so a fresh SDK
    install doesn't fail because ~/.checkrd doesn't exist yet."""
    state = tmp_path / "nested" / "subdir" / "policy_state.json"
    assert not state.parent.exists()
    persist_state(7, _FAKE_HASH, _FAKE_ENVELOPE, path=state)
    assert state.exists()
    assert load_persisted_state(state) == (7, _FAKE_HASH, _FAKE_ENVELOPE)


def test_persist_writes_real_schema_version(tmp_path: Path) -> None:
    """Pin the on-disk schema_version field to the constant so a
    refactor can't silently desync the loader and the writer."""
    state = tmp_path / "policy_state.json"
    persist_state(123, _FAKE_HASH, _FAKE_ENVELOPE, path=state)
    parsed = json.loads(state.read_text())
    assert parsed["schema_version"] == POLICY_STATE_SCHEMA_VERSION
    assert parsed["last_policy_version"] == 123
    assert parsed["last_policy_hash"] == _FAKE_HASH
    assert parsed["bundle_envelope_json"] == _FAKE_ENVELOPE
    assert "updated_at" in parsed


def test_persist_failure_does_not_raise(tmp_path: Path) -> None:
    """Disk write failures are best-effort — the SDK must not crash if
    persistence fails (e.g. read-only filesystem). The in-process
    monotonic check still applies."""
    state = tmp_path / "policy_state.json"
    with patch("checkrd._policy_state.os.replace", side_effect=OSError("read-only fs")):
        persist_state(99, _FAKE_HASH, _FAKE_ENVELOPE, path=state)  # must not raise
    assert load_persisted_state(state) == (0, None, None)


def test_persist_does_not_leave_temp_files_on_success(tmp_path: Path) -> None:
    """The atomic rename pattern uses a temp file in the same directory.
    On success, only the final file should remain."""
    state = tmp_path / "policy_state.json"
    persist_state(50, _FAKE_HASH, _FAKE_ENVELOPE, path=state)
    files = list(tmp_path.iterdir())
    assert files == [state], f"unexpected leftover files: {files}"


def test_persist_cleans_up_temp_file_on_failure(tmp_path: Path) -> None:
    """A failed atomic rename should not leak temp files in the parent dir."""
    state = tmp_path / "policy_state.json"
    with patch("checkrd._policy_state.os.replace", side_effect=OSError("simulated")):
        persist_state(99, _FAKE_HASH, _FAKE_ENVELOPE, path=state)
    leftover = [f for f in tmp_path.iterdir() if f.name.startswith(".policy_state_")]
    assert leftover == [], f"temp files leaked: {leftover}"


def test_persist_skips_oversized_envelope(tmp_path: Path) -> None:
    """A pathologically large envelope is dropped on the writer side
    so the loader never has to reject a huge file on every restart."""
    state = tmp_path / "policy_state.json"
    huge_envelope = "A" * (5 * 1024 * 1024)  # > 4 MB cap
    persist_state(7, _FAKE_HASH, huge_envelope, path=state)
    assert load_persisted_state(state) == (0, None, None)
    assert not state.exists()


def test_persist_fsyncs_parent_directory(tmp_path: Path) -> None:
    """Industry-standard atomic write must fsync the parent directory after
    rename so the directory entry is durable across power loss. Pin the
    call so a refactor can't silently regress this. Reference: Theodore
    Ts'o, "Don't fear the fsync" (LWN.net 2009), `man 2 fsync`."""
    state = tmp_path / "policy_state.json"
    fsync_calls: list[int] = []
    real_fsync = os.fsync

    def tracking_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        real_fsync(fd)

    with patch("checkrd._policy_state.os.fsync", side_effect=tracking_fsync):
        persist_state(7, _FAKE_HASH, _FAKE_ENVELOPE, path=state)

    # Two fsync calls: one on the temp file (durable bytes), one on the
    # parent directory (durable directory entry).
    assert len(fsync_calls) == 2, (
        f"expected exactly 2 fsync calls (file + dir), got {len(fsync_calls)}"
    )
    assert load_persisted_state(state) == (7, _FAKE_HASH, _FAKE_ENVELOPE)


def test_persist_succeeds_when_dir_fsync_unsupported(tmp_path: Path) -> None:
    """Some platforms (Windows, certain FUSE mounts) don't support fsync
    on a directory file descriptor. The dir-fsync step must degrade
    silently — the atomic rename itself still provides crash safety
    for the file contents."""
    state = tmp_path / "policy_state.json"
    real_fsync = os.fsync
    seen: dict[str, bool] = {"saw_dir_fsync": False}

    def stub_fsync(fd: int) -> None:
        if not seen["saw_dir_fsync"]:
            seen["saw_dir_fsync"] = True
            real_fsync(fd)
            return
        raise OSError(22, "Invalid argument")  # simulate EINVAL on dir fsync

    with patch("checkrd._policy_state.os.fsync", side_effect=stub_fsync):
        persist_state(11, _FAKE_HASH, _FAKE_ENVELOPE, path=state)  # must not raise

    assert load_persisted_state(state) == (11, _FAKE_HASH, _FAKE_ENVELOPE)


def test_default_path_respects_config_dir_env(tmp_path: Path, monkeypatch: Any) -> None:
    """``CHECKRD_CONFIG_DIR`` redirects the default state path,
    matching the same override pattern used by ``LocalIdentity``."""
    from checkrd._policy_state import _default_state_path

    monkeypatch.setenv("CHECKRD_CONFIG_DIR", str(tmp_path))
    assert _default_state_path() == tmp_path / "policy_state.json"


# ============================================================
# Defense-in-depth: a corrupted file never causes the SDK to crash
# ============================================================


def test_load_handles_unreadable_file(tmp_path: Path) -> None:
    """A file with mode 000 (e.g. left behind by a failed install) must
    degrade to ``(0, None, None)``, not crash."""
    state = tmp_path / "policy_state.json"
    persist_state(5, _FAKE_HASH, _FAKE_ENVELOPE, path=state)
    os.chmod(state, 0o000)
    try:
        result = load_persisted_state(state)
        # On macOS/Linux as a non-root user the chmod takes effect and
        # we expect a graceful (0, None, None). On systems where the
        # chmod is ignored (running as root in CI), the load succeeds.
        # Both are acceptable — the contract is "must not raise."
        assert result in ((0, None, None), (5, _FAKE_HASH, _FAKE_ENVELOPE))
    finally:
        # Restore so pytest's tmp_path cleanup can delete the file.
        os.chmod(state, 0o600)
