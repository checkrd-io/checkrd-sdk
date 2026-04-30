"""Tests for P1 security fixes: fork safety, sensitive header log
filtering, and .env file permissions.

1. **Fork safety** (P1-1): background threads (batcher, control receiver,
   watchers) must survive ``os.fork()`` cleanly. The SDK uses
   ``os.register_at_fork(after_in_child=...)`` to walk a WeakSet of
   live instances in the child process and call ``_reinit_after_fork``
   on each. Prevents silent telemetry loss and deadlocks in
   Gunicorn/uWSGI ``preload_app=True`` deployments. Same pattern as
   asyncpg, psycopg3, and modern Sentry.

2. **SensitiveHeadersFilter** (P1-2): log records containing credential-
   bearing headers (Authorization, X-API-Key, Cookie, etc.) must have
   their values replaced with [REDACTED] before reaching any handler.
   Prevents API key leaks when DEBUG logging is enabled on httpx/httpcore.

3. **.env file permissions** (P1-3): write_env_file() must set 0o600
   permissions on the output file since it contains the API key and
   Ed25519 private key.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from checkrd._logging import SensitiveHeadersFilter


# ============================================================
# P1-1: Fork safety
# ============================================================


class TestBatcherForkSafety:
    """Verify TelemetryBatcher detects PID change and restarts."""

    def test_batcher_stores_pid(self) -> None:
        """Batcher must record os.getpid() at construction time."""
        from checkrd.batcher import TelemetryBatcher

        engine = MagicMock()
        engine.sign_telemetry_batch.return_value = {
            "content_digest": "d", "signature_input": "si",
            "signature": "s", "instance_id": "iid", "expires": 0,
        }
        batcher = TelemetryBatcher(
            base_url="http://localhost:8080",
            api_key="ck_test_fake",
            engine=engine,
            signer_agent_id="test-agent",
        )
        try:
            assert batcher._pid == os.getpid()
        finally:
            batcher.stop()

    def test_batcher_detects_simulated_fork(self) -> None:
        """When PID differs from recorded value, ``_reinit_after_fork``
        resets state and spawns a fresh thread. Mirrors the production
        path where ``os.register_at_fork`` invokes the same method in
        the child after a real fork."""
        from checkrd.batcher import TelemetryBatcher

        engine = MagicMock()
        engine.sign_telemetry_batch.return_value = {
            "content_digest": "d", "signature_input": "si",
            "signature": "s", "instance_id": "iid", "expires": 0,
        }
        batcher = TelemetryBatcher(
            base_url="http://localhost:8080",
            api_key="ck_test_fake",
            engine=engine,
            signer_agent_id="test-agent",
        )
        try:
            old_thread = batcher._thread
            assert old_thread is not None

            # Simulate fork by changing the stored PID. The reinit
            # method is what `os.register_at_fork` would call in the
            # forked child.
            batcher._pid = -1
            batcher._reinit_after_fork()

            assert batcher._pid == os.getpid()
            assert batcher._thread is not old_thread
            assert batcher._thread.is_alive()
            assert batcher._buffer == []
            assert batcher._events_sent == 0
        finally:
            batcher.stop()

    def test_reinit_is_idempotent_when_pid_matches(self) -> None:
        """The fork handler walks every live batcher; calling
        ``_reinit_after_fork`` on an instance whose PID still matches
        the current process must be a no-op (don't reset for nothing)."""
        from checkrd.batcher import TelemetryBatcher

        engine = MagicMock()
        engine.sign_telemetry_batch.return_value = {
            "content_digest": "d", "signature_input": "si",
            "signature": "s", "instance_id": "iid", "expires": 0,
        }
        batcher = TelemetryBatcher(
            base_url="http://localhost:8080",
            api_key="ck_test_fake",
            engine=engine,
            signer_agent_id="test-agent",
        )
        try:
            old_thread = batcher._thread
            batcher._reinit_after_fork()
            # PID matches → no reset, same thread reference.
            assert batcher._thread is old_thread
        finally:
            batcher.stop()

    def test_live_batchers_registry_tracks_construction(self) -> None:
        """New batchers register themselves in ``_LIVE_BATCHERS`` so the
        fork handler can find them. The set is held weakly — once a
        batcher is closed and dereferenced, GC reclaims it from the set
        without needing explicit removal."""
        from checkrd.batcher import _LIVE_BATCHERS, TelemetryBatcher

        engine = MagicMock()
        engine.sign_telemetry_batch.return_value = {
            "content_digest": "d", "signature_input": "si",
            "signature": "s", "instance_id": "iid", "expires": 0,
        }
        batcher = TelemetryBatcher(
            base_url="http://localhost:8080",
            api_key="ck_test_fake",
            engine=engine,
            signer_agent_id="test-agent",
        )
        try:
            assert batcher in _LIVE_BATCHERS
        finally:
            batcher.stop()


class TestControlReceiverForkSafety:
    """Verify ControlReceiver detects PID change and resets state."""

    def test_control_stores_pid(self) -> None:
        from checkrd.control import ControlReceiver
        receiver = ControlReceiver(
            base_url="http://localhost:8080",
            agent_id="test-agent",
            api_key="ck_test_fake",
            engine=MagicMock(),
        )
        assert receiver._pid == os.getpid()

    def test_control_detects_simulated_fork(self) -> None:
        from checkrd.control import ControlReceiver
        receiver = ControlReceiver(
            base_url="http://localhost:8080",
            agent_id="test-agent",
            api_key="ck_test_fake",
            engine=MagicMock(),
        )
        # Simulate fork by changing the stored PID. The reinit method
        # is what ``os.register_at_fork`` would call in the child.
        receiver._pid = -1
        receiver._thread = MagicMock()  # stale thread from parent
        receiver._reinit_after_fork()

        assert receiver._pid == os.getpid()
        assert receiver._thread is None  # cleared for fresh start
        assert not receiver._stop.is_set()

    def test_live_receivers_registry_tracks_construction(self) -> None:
        """New receivers self-register so the fork handler can find them."""
        from checkrd.control import _LIVE_RECEIVERS, ControlReceiver
        receiver = ControlReceiver(
            base_url="http://localhost:8080",
            agent_id="test-agent",
            api_key="ck_test_fake",
            engine=MagicMock(),
        )
        assert receiver in _LIVE_RECEIVERS


class TestPolicyFileWatcherForkSafety:
    """Verify PolicyFileWatcher detects PID change."""

    def test_watcher_stores_pid(self, tmp_path: Path) -> None:
        from checkrd.watchers import PolicyFileWatcher
        policy = tmp_path / "p.yaml"
        policy.write_text("agent: a\ndefault: allow\nrules: []\n")
        watcher = PolicyFileWatcher(MagicMock(), policy, interval_secs=60)
        assert watcher._pid == os.getpid()

    def test_watcher_detects_simulated_fork(self, tmp_path: Path) -> None:
        from checkrd.watchers import PolicyFileWatcher
        policy = tmp_path / "p.yaml"
        policy.write_text("agent: a\ndefault: allow\nrules: []\n")
        watcher = PolicyFileWatcher(MagicMock(), policy, interval_secs=60)

        watcher._pid = -1
        watcher._thread = MagicMock()
        watcher._stopped = True
        watcher._reinit_after_fork()

        assert watcher._pid == os.getpid()
        assert watcher._thread is None
        assert watcher._stopped is False

    def test_live_watchers_registry_tracks_construction(self, tmp_path: Path) -> None:
        from checkrd.watchers import _LIVE_POLICY_WATCHERS, PolicyFileWatcher
        policy = tmp_path / "p.yaml"
        policy.write_text("agent: a\ndefault: allow\nrules: []\n")
        watcher = PolicyFileWatcher(MagicMock(), policy, interval_secs=60)
        assert watcher in _LIVE_POLICY_WATCHERS


class TestKillSwitchWatcherForkSafety:
    """Verify KillSwitchFileWatcher detects PID change."""

    def test_watcher_stores_pid(self, tmp_path: Path) -> None:
        from checkrd.watchers import KillSwitchFileWatcher
        watcher = KillSwitchFileWatcher(MagicMock(), tmp_path / "ks", interval_secs=60)
        assert watcher._pid == os.getpid()

    def test_watcher_detects_simulated_fork(self, tmp_path: Path) -> None:
        from checkrd.watchers import KillSwitchFileWatcher
        watcher = KillSwitchFileWatcher(MagicMock(), tmp_path / "ks", interval_secs=60)

        watcher._pid = -1
        watcher._thread = MagicMock()
        watcher._stopped = True
        watcher._reinit_after_fork()

        assert watcher._pid == os.getpid()
        assert watcher._thread is None
        assert watcher._stopped is False

    def test_live_watchers_registry_tracks_construction(self, tmp_path: Path) -> None:
        from checkrd.watchers import _LIVE_KILLSWITCH_WATCHERS, KillSwitchFileWatcher
        watcher = KillSwitchFileWatcher(MagicMock(), tmp_path / "ks", interval_secs=60)
        assert watcher in _LIVE_KILLSWITCH_WATCHERS


# ============================================================
# Real-fork integration test
# ============================================================
#
# The simulated-fork tests cover the reset *logic*. This one exercises
# the actual ``os.register_at_fork`` hook end-to-end via
# ``multiprocessing`` with the ``fork`` start method, mirroring the
# Gunicorn/uWSGI ``preload_app=True`` scenario in production.


def _child_inspect_batcher_state(result_file: str) -> None:
    """Run inside the forked child. Snapshots the inherited batcher's
    state to a JSON file the parent reads back. Module-level so the
    multiprocessing pickle can find it."""
    import json as _json

    from checkrd.batcher import _LIVE_BATCHERS

    batchers = list(_LIVE_BATCHERS)
    info = {
        "child_pid": os.getpid(),
        "live_count": len(batchers),
        "first_pid": batchers[0]._pid if batchers else None,
        "first_thread_alive": (
            batchers[0]._thread.is_alive() if batchers else None
        ),
    }
    Path(result_file).write_text(_json.dumps(info))


@pytest.mark.skipif(sys.platform == "win32", reason="fork() not available on Windows")
class TestRealForkIntegration:
    """End-to-end: actually fork() and verify the at-fork handler
    reset the batcher in the child process."""

    def test_batcher_pid_and_thread_reset_in_real_fork(
        self, tmp_path: Path
    ) -> None:
        """Spawn a child via ``multiprocessing.get_context('fork')``,
        let the at-fork handler fire in the child, and assert the
        batcher's ``_pid`` matches the child's PID and the thread is
        a freshly-spawned live one (not the parent's stale daemon).
        """
        import json as _json
        import multiprocessing as mp

        from checkrd.batcher import TelemetryBatcher

        engine = MagicMock()
        engine.sign_telemetry_batch.return_value = {
            "content_digest": "d", "signature_input": "si",
            "signature": "s", "instance_id": "iid", "expires": 0,
        }
        batcher = TelemetryBatcher(
            base_url="http://localhost:8080",
            api_key="ck_test_fake",
            engine=engine,
            signer_agent_id="test-agent",
        )
        parent_pid = batcher._pid
        try:
            result = tmp_path / "fork_result.json"
            ctx = mp.get_context("fork")
            proc = ctx.Process(
                target=_child_inspect_batcher_state, args=(str(result),)
            )
            proc.start()
            proc.join(timeout=10)
            assert proc.exitcode == 0, "child process did not exit cleanly"

            info = _json.loads(result.read_text())
            assert info["live_count"] >= 1
            # The handler ran in the child → _pid was rewritten to the
            # child's PID, which is necessarily different from ours.
            assert info["first_pid"] == info["child_pid"]
            assert info["first_pid"] != parent_pid
            # The child spawned a fresh thread that is live there.
            # (The thread in the *parent* is unaffected — we don't
            #  assert anything about it.)
            assert info["first_thread_alive"] is True
        finally:
            batcher.stop()


# ============================================================
# P1-2: SensitiveHeadersFilter
# ============================================================


class TestSensitiveHeadersFilter:
    """Verify that credential-bearing headers are redacted from log messages."""

    def _filter_message(self, msg: str, args: tuple = ()) -> str:
        """Run a log message through the filter and return the result."""
        f = SensitiveHeadersFilter()
        record = logging.LogRecord(
            "test", logging.DEBUG, "test.py", 1, msg, args, None,
        )
        f.filter(record)
        # Format the final message the way a handler would.
        if record.args:
            return record.msg % record.args
        return record.msg

    def test_redacts_authorization_header(self) -> None:
        result = self._filter_message("Authorization: Bearer sk-abc123xyz")
        assert "sk-abc123xyz" not in result
        assert "[REDACTED]" in result

    def test_redacts_x_api_key_header(self) -> None:
        result = self._filter_message("X-API-Key: ck_live_secret_key_123")
        assert "ck_live_secret_key_123" not in result
        assert "[REDACTED]" in result

    def test_redacts_api_key_header(self) -> None:
        result = self._filter_message("api-key: my-secret-key")
        assert "my-secret-key" not in result
        assert "[REDACTED]" in result

    def test_redacts_cookie_header(self) -> None:
        result = self._filter_message("Cookie: session=abc123")
        assert "session=abc123" not in result
        assert "[REDACTED]" in result

    def test_redacts_proxy_authorization(self) -> None:
        result = self._filter_message("proxy-authorization: Basic dXNlcjpwYXNz")
        assert "Basic" not in result
        assert "[REDACTED]" in result

    def test_preserves_non_sensitive_headers(self) -> None:
        result = self._filter_message("Content-Type: application/json")
        assert "application/json" in result
        assert "[REDACTED]" not in result

    def test_redacts_tuple_format(self) -> None:
        """httpx logs headers as ('Header', 'value') tuples."""
        result = self._filter_message("('authorization', 'Bearer sk-secret')")
        assert "sk-secret" not in result
        assert "[REDACTED]" in result

    def test_case_insensitive(self) -> None:
        result = self._filter_message("AUTHORIZATION: Bearer secret")
        assert "secret" not in result
        assert "[REDACTED]" in result

    def test_redacts_in_args(self) -> None:
        """Log message args (%-formatting) must also be redacted."""
        f = SensitiveHeadersFilter()
        record = logging.LogRecord(
            "test", logging.DEBUG, "test.py", 1,
            "sending request with %s", ("Authorization: Bearer sk-key",), None,
        )
        f.filter(record)
        formatted = record.msg % record.args
        assert "sk-key" not in formatted
        assert "[REDACTED]" in formatted

    def test_multiple_headers_in_one_message(self) -> None:
        msg = "headers: Authorization: sk-abc, X-API-Key: ck_live_123, Content-Type: json"
        result = self._filter_message(msg)
        assert "sk-abc" not in result
        assert "ck_live_123" not in result
        assert "json" in result  # non-sensitive preserved

    def test_filter_installed_on_httpx_logger(self) -> None:
        """The SensitiveHeadersFilter must be installed on httpx logger."""
        # Force import of checkrd to trigger filter installation.
        import checkrd  # noqa: F401

        httpx_logger = logging.getLogger("httpx")
        filter_types = {type(f).__name__ for f in httpx_logger.filters}
        assert "SensitiveHeadersFilter" in filter_types

    def test_filter_installed_on_httpcore_logger(self) -> None:
        import checkrd  # noqa: F401

        httpcore_logger = logging.getLogger("httpcore")
        filter_types = {type(f).__name__ for f in httpcore_logger.filters}
        assert "SensitiveHeadersFilter" in filter_types

    def test_filter_installed_on_checkrd_logger(self) -> None:
        import checkrd  # noqa: F401

        checkrd_logger = logging.getLogger("checkrd")
        filter_types = {type(f).__name__ for f in checkrd_logger.filters}
        assert "SensitiveHeadersFilter" in filter_types

    def test_safe_on_non_string_message(self) -> None:
        """Filter must not crash on non-string log messages."""
        f = SensitiveHeadersFilter()
        record = logging.LogRecord(
            "test", logging.DEBUG, "test.py", 1, 42, (), None,  # type: ignore[arg-type]
        )
        assert f.filter(record) is True  # always allows through

    def test_empty_message(self) -> None:
        result = self._filter_message("")
        assert result == ""


# ============================================================
# P1-3: .env file permissions
# ============================================================


_skip_if_root = pytest.mark.skipif(
    hasattr(os, "getuid") and os.getuid() == 0,
    reason="root bypasses filesystem permission checks",
)
_skip_if_windows = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Unix permission model not available on Windows",
)


@_skip_if_root
@_skip_if_windows
class TestEnvFilePermissions:
    """Verify write_env_file() sets 0o600 permissions."""

    def test_env_file_is_owner_only(self, tmp_path: Path) -> None:
        from checkrd._init_wizard import write_env_file

        env_path = tmp_path / ".env"
        write_env_file(
            api_key="ck_test_secret_key",
            agent_id="test-agent",
            agent_key_b64="base64encodedkey==",
            path=env_path,
        )

        assert env_path.exists()
        mode = env_path.stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"

    def test_env_file_contains_credentials(self, tmp_path: Path) -> None:
        """The file must contain the API key and agent key."""
        from checkrd._init_wizard import write_env_file

        env_path = tmp_path / ".env"
        write_env_file(
            api_key="ck_test_key_123",
            agent_id="my-agent",
            agent_key_b64="c2VjcmV0",
            path=env_path,
        )

        content = env_path.read_text()
        assert "CHECKRD_API_KEY=ck_test_key_123" in content
        assert "CHECKRD_AGENT_KEY=c2VjcmV0" in content
        assert "CHECKRD_AGENT_ID=my-agent" in content

    def test_env_file_not_group_readable(self, tmp_path: Path) -> None:
        from checkrd._init_wizard import write_env_file

        env_path = tmp_path / ".env"
        write_env_file(
            api_key="ck_test_key",
            agent_id="agent",
            agent_key_b64="key==",
            path=env_path,
        )

        mode = env_path.stat().st_mode
        assert not (mode & stat.S_IRGRP), "file should not be group-readable"
        assert not (mode & stat.S_IROTH), "file should not be other-readable"

    def test_env_file_preserves_existing_non_checkrd_lines(self, tmp_path: Path) -> None:
        from checkrd._init_wizard import write_env_file

        env_path = tmp_path / ".env"
        env_path.write_text("OTHER_VAR=hello\n")

        write_env_file(
            api_key="ck_test_key",
            agent_id="agent",
            agent_key_b64="key==",
            path=env_path,
        )

        content = env_path.read_text()
        assert "OTHER_VAR=hello" in content
        assert "CHECKRD_API_KEY=ck_test_key" in content

    def test_overwrite_keeps_permissions(self, tmp_path: Path) -> None:
        """Second write_env_file() call must maintain 0o600."""
        from checkrd._init_wizard import write_env_file

        env_path = tmp_path / ".env"
        write_env_file(
            api_key="ck_first", agent_id="a1", agent_key_b64="k1==", path=env_path,
        )
        write_env_file(
            api_key="ck_second", agent_id="a2", agent_key_b64="k2==", path=env_path,
        )

        mode = env_path.stat().st_mode & 0o777
        assert mode == 0o600
        assert "ck_second" in env_path.read_text()
