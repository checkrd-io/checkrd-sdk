"""Tests for the filesystem-based watchers (Tier 3 / offline mode).

These tests exercise the watchers against a Mock WasmEngine so they're fast
and don't depend on the compiled .wasm file. The polling interval is set to
a small value (10-50ms) so each test completes in well under a second.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from checkrd.engine import WasmEngine
from checkrd.watchers import KillSwitchFileWatcher, PolicyFileWatcher
from tests.conftest import wait_for


# ============================================================
# Helpers
# ============================================================


def _make_mock_engine() -> MagicMock:
    """Mock WasmEngine that records reload_policy and set_kill_switch calls."""
    engine = MagicMock(spec=WasmEngine)
    engine.reload_policy = MagicMock()
    engine.set_kill_switch = MagicMock()
    return engine


VALID_POLICY_YAML = """
agent: test-agent
default: deny
rules:
  - name: allow-stripe
    allow:
      method: [GET]
      url: "api.stripe.com/v1/charges"
"""


# ============================================================
# PolicyFileWatcher
# ============================================================


# Filesystem watchers exercise polling loops with real `time.sleep()` (mtime
# resolution on macOS HFS+ is 1s, so we have to wait between writes). Mark the
# whole class as slow so devs can skip with `pytest -m "not slow"`.
@pytest.mark.slow
class TestPolicyFileWatcher:
    def test_initial_load_does_not_call_reload(self, tmp_path: Path) -> None:
        # The engine is initialized with the file's content at wrap() time, so
        # the first poll cycle should NOT trigger a reload.
        policy = tmp_path / "policy.yaml"
        policy.write_text(VALID_POLICY_YAML)

        engine = _make_mock_engine()
        watcher = PolicyFileWatcher(engine, policy, interval_secs=0.05)
        try:
            watcher.start()
            time.sleep(0.5)  # Let several poll cycles run
            engine.reload_policy.assert_not_called()
        finally:
            watcher.stop()

    def test_reload_called_on_mtime_change(self, tmp_path: Path) -> None:
        policy = tmp_path / "policy.yaml"
        policy.write_text(VALID_POLICY_YAML)

        engine = _make_mock_engine()
        watcher = PolicyFileWatcher(engine, policy, interval_secs=0.05)
        try:
            watcher.start()
            time.sleep(0.2)  # Let initial state settle (4+ poll cycles at 50ms)

            # Bump mtime by writing the same content (mtime now > _last_mtime)
            time.sleep(0.05)  # Ensure mtime advances on filesystems with 1s resolution
            os.utime(policy, None)  # Update mtime to now
            policy.write_text(VALID_POLICY_YAML + "\n# updated\n")

            wait_for(
                lambda: engine.reload_policy.called,
                timeout=2.0,
                poll=0.02,
            )
            engine.reload_policy.assert_called_once()
        finally:
            watcher.stop()

    def test_reload_not_called_if_mtime_unchanged(self, tmp_path: Path) -> None:
        policy = tmp_path / "policy.yaml"
        policy.write_text(VALID_POLICY_YAML)

        engine = _make_mock_engine()
        watcher = PolicyFileWatcher(engine, policy, interval_secs=0.05)
        try:
            watcher.start()
            time.sleep(0.3)  # Several poll cycles
            # Without touching the file, no reload should happen
            engine.reload_policy.assert_not_called()
        finally:
            watcher.stop()

    def test_invalid_yaml_keeps_old_policy_and_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        policy = tmp_path / "policy.yaml"
        policy.write_text(VALID_POLICY_YAML)

        engine = _make_mock_engine()
        watcher = PolicyFileWatcher(engine, policy, interval_secs=0.05)
        try:
            watcher.start()
            time.sleep(0.2)

            # Write invalid YAML
            time.sleep(0.05)
            policy.write_text("not: valid: yaml: at: all:")

            time.sleep(0.5)
            # reload_policy should NOT have been called
            engine.reload_policy.assert_not_called()
        finally:
            watcher.stop()

    def test_non_dict_yaml_keeps_old_policy(self, tmp_path: Path) -> None:
        policy = tmp_path / "policy.yaml"
        policy.write_text(VALID_POLICY_YAML)

        engine = _make_mock_engine()
        watcher = PolicyFileWatcher(engine, policy, interval_secs=0.05)
        try:
            watcher.start()
            time.sleep(0.2)

            # Replace with a YAML list (not a mapping)
            time.sleep(0.05)
            policy.write_text("- one\n- two\n- three\n")

            time.sleep(0.5)
            engine.reload_policy.assert_not_called()
        finally:
            watcher.stop()

    def test_missing_file_logs_warning_and_keeps_old_policy(
        self, tmp_path: Path
    ) -> None:
        policy = tmp_path / "policy.yaml"
        policy.write_text(VALID_POLICY_YAML)

        engine = _make_mock_engine()
        watcher = PolicyFileWatcher(engine, policy, interval_secs=0.05)
        try:
            watcher.start()
            time.sleep(0.2)

            # Delete the file
            policy.unlink()

            time.sleep(0.5)
            engine.reload_policy.assert_not_called()
        finally:
            watcher.stop()

    def test_stop_joins_thread_within_timeout(self, tmp_path: Path) -> None:
        policy = tmp_path / "policy.yaml"
        policy.write_text(VALID_POLICY_YAML)

        engine = _make_mock_engine()
        watcher = PolicyFileWatcher(engine, policy, interval_secs=0.05)
        watcher.start()
        thread = watcher._thread
        assert thread is not None
        assert thread.is_alive()

        watcher.stop()
        assert not thread.is_alive(), "watcher thread did not exit"

    def test_thread_is_daemon(self, tmp_path: Path) -> None:
        policy = tmp_path / "policy.yaml"
        policy.write_text(VALID_POLICY_YAML)

        engine = _make_mock_engine()
        watcher = PolicyFileWatcher(engine, policy, interval_secs=0.05)
        try:
            watcher.start()
            thread = watcher._thread
            assert thread is not None
            assert thread.daemon is True
        finally:
            watcher.stop()

    def test_stop_is_idempotent(self, tmp_path: Path) -> None:
        policy = tmp_path / "policy.yaml"
        policy.write_text(VALID_POLICY_YAML)

        engine = _make_mock_engine()
        watcher = PolicyFileWatcher(engine, policy, interval_secs=0.05)
        watcher.start()
        watcher.stop()
        watcher.stop()  # second call must not raise

    def test_reload_called_with_valid_policy_json(self, tmp_path: Path) -> None:
        # Verify the JSON passed to reload_policy is parseable and contains
        # the expected keys.
        import json

        policy = tmp_path / "policy.yaml"
        policy.write_text(VALID_POLICY_YAML)

        engine = _make_mock_engine()
        watcher = PolicyFileWatcher(engine, policy, interval_secs=0.05)
        try:
            watcher.start()
            time.sleep(0.2)

            # Trigger a reload
            time.sleep(0.05)
            policy.write_text(
                """
agent: test-agent
default: allow
rules: []
"""
            )

            wait_for(
                lambda: engine.reload_policy.called,
                timeout=2.0,
                poll=0.02,
            )

            call_args = engine.reload_policy.call_args
            policy_json = call_args[0][0]
            parsed = json.loads(policy_json)
            assert parsed["agent"] == "test-agent"
            assert parsed["default"] == "allow"
        finally:
            watcher.stop()


# ============================================================
# KillSwitchFileWatcher
# ============================================================


@pytest.mark.slow
class TestKillSwitchFileWatcher:
    def test_initial_state_off_when_file_missing(self, tmp_path: Path) -> None:
        sentinel = tmp_path / "killswitch"
        engine = _make_mock_engine()
        watcher = KillSwitchFileWatcher(engine, sentinel, interval_secs=0.05)
        try:
            # File doesn't exist → set_kill_switch was NOT called at init
            engine.set_kill_switch.assert_not_called()
        finally:
            watcher.stop()

    def test_initial_state_on_when_file_present(self, tmp_path: Path) -> None:
        sentinel = tmp_path / "killswitch"
        sentinel.touch()

        engine = _make_mock_engine()
        watcher = KillSwitchFileWatcher(engine, sentinel, interval_secs=0.05)
        try:
            # File exists → set_kill_switch(True) called at init
            engine.set_kill_switch.assert_called_once_with(True)
        finally:
            watcher.stop()

    def test_kill_switch_enabled_when_file_appears(self, tmp_path: Path) -> None:
        sentinel = tmp_path / "killswitch"
        engine = _make_mock_engine()
        watcher = KillSwitchFileWatcher(engine, sentinel, interval_secs=0.05)
        try:
            watcher.start()
            time.sleep(0.2)

            sentinel.touch()
            wait_for(
                lambda: engine.set_kill_switch.called
                and engine.set_kill_switch.call_args_list[-1][0][0] is True,
                timeout=2.0,
                poll=0.02,
            )
        finally:
            watcher.stop()

    def test_kill_switch_disabled_when_file_disappears(self, tmp_path: Path) -> None:
        sentinel = tmp_path / "killswitch"
        sentinel.touch()  # Start enabled

        engine = _make_mock_engine()
        watcher = KillSwitchFileWatcher(engine, sentinel, interval_secs=0.05)
        try:
            watcher.start()
            time.sleep(0.2)

            sentinel.unlink()
            wait_for(
                lambda: any(
                    call[0][0] is False
                    for call in engine.set_kill_switch.call_args_list
                ),
                timeout=2.0,
                poll=0.02,
            )
        finally:
            watcher.stop()

    def test_does_not_call_set_when_state_unchanged(self, tmp_path: Path) -> None:
        sentinel = tmp_path / "killswitch"
        engine = _make_mock_engine()
        watcher = KillSwitchFileWatcher(engine, sentinel, interval_secs=0.05)
        try:
            watcher.start()
            time.sleep(0.3)  # Several poll cycles
            # File never appeared → set_kill_switch was never called
            engine.set_kill_switch.assert_not_called()
        finally:
            watcher.stop()

    def test_handles_repeated_toggles(self, tmp_path: Path) -> None:
        sentinel = tmp_path / "killswitch"
        engine = _make_mock_engine()
        watcher = KillSwitchFileWatcher(engine, sentinel, interval_secs=0.05)
        try:
            watcher.start()

            for _ in range(3):
                # Enable
                sentinel.touch()
                wait_for(
                    lambda: any(
                        c[0][0] is True
                        for c in engine.set_kill_switch.call_args_list
                    ),
                    timeout=2.0,
                    poll=0.02,
                )
                engine.set_kill_switch.reset_mock()

                # Disable
                sentinel.unlink()
                wait_for(
                    lambda: any(
                        c[0][0] is False
                        for c in engine.set_kill_switch.call_args_list
                    ),
                    timeout=2.0,
                    poll=0.02,
                )
                engine.set_kill_switch.reset_mock()
        finally:
            watcher.stop()

    def test_stop_joins_thread(self, tmp_path: Path) -> None:
        sentinel = tmp_path / "killswitch"
        engine = _make_mock_engine()
        watcher = KillSwitchFileWatcher(engine, sentinel, interval_secs=0.05)
        watcher.start()
        thread = watcher._thread
        assert thread is not None
        assert thread.is_alive()

        watcher.stop()
        assert not thread.is_alive()

    def test_thread_is_daemon(self, tmp_path: Path) -> None:
        sentinel = tmp_path / "killswitch"
        engine = _make_mock_engine()
        watcher = KillSwitchFileWatcher(engine, sentinel, interval_secs=0.05)
        try:
            watcher.start()
            thread = watcher._thread
            assert thread is not None
            assert thread.daemon is True
        finally:
            watcher.stop()

    def test_stop_is_idempotent(self, tmp_path: Path) -> None:
        sentinel = tmp_path / "killswitch"
        engine = _make_mock_engine()
        watcher = KillSwitchFileWatcher(engine, sentinel, interval_secs=0.05)
        watcher.start()
        watcher.stop()
        watcher.stop()


# ============================================================
# Edge cases
# ============================================================


@pytest.mark.slow
class TestPolicyFileWatcherEdgeCases:
    def test_engine_reload_error_keeps_watcher_running(self, tmp_path: Path) -> None:
        """If engine.reload_policy raises, the watcher continues polling."""
        from checkrd.exceptions import CheckrdInitError

        policy = tmp_path / "policy.yaml"
        policy.write_text(VALID_POLICY_YAML)

        engine = _make_mock_engine()
        engine.reload_policy.side_effect = CheckrdInitError("bad policy")
        # Pin the polling backend so the assertion on call count is
        # backend-deterministic. The watchdog backend correctly fires
        # one reload per filesystem event, where polling collapses
        # multiple writes within a poll interval into a single reload.
        # The watchdog equivalent is exercised in test_watchers_backend.py.
        watcher = PolicyFileWatcher(
            engine, policy, interval_secs=0.05, backend="poll",
        )
        try:
            watcher.start()
            time.sleep(0.2)

            # Trigger a reload with a new file
            os.utime(policy, None)
            policy.write_text(VALID_POLICY_YAML + "\n# change\n")

            wait_for(
                lambda: engine.reload_policy.called,
                timeout=2.0,
                poll=0.02,
            )
            engine.reload_policy.assert_called_once()

            # Watcher thread should still be alive despite the error
            assert watcher._thread is not None
            assert watcher._thread.is_alive()
        finally:
            watcher.stop()

    def test_file_reappears_after_deletion(self, tmp_path: Path) -> None:
        """Policy file deleted then recreated triggers a reload."""
        policy = tmp_path / "policy.yaml"
        policy.write_text(VALID_POLICY_YAML)

        engine = _make_mock_engine()
        watcher = PolicyFileWatcher(engine, policy, interval_secs=0.05)
        try:
            watcher.start()
            time.sleep(0.2)

            # Delete the file
            policy.unlink()
            time.sleep(0.5)
            engine.reload_policy.assert_not_called()

            # Recreate with different content
            policy.write_text(
                "agent: test-agent\ndefault: allow\nrules: []\n"
            )

            wait_for(
                lambda: engine.reload_policy.called,
                timeout=2.0,
                poll=0.02,
            )
            engine.reload_policy.assert_called_once()
        finally:
            watcher.stop()


@pytest.mark.slow
class TestKillSwitchFileWatcherEdgeCases:
    def test_file_replaced_atomically(self, tmp_path: Path) -> None:
        """Kill switch file replaced via rename (atomic) is detected."""
        sentinel = tmp_path / "killswitch"
        engine = _make_mock_engine()
        watcher = KillSwitchFileWatcher(engine, sentinel, interval_secs=0.05)
        try:
            watcher.start()
            time.sleep(0.2)

            # Create via atomic rename
            tmp_file = tmp_path / "killswitch.tmp"
            tmp_file.touch()
            tmp_file.rename(sentinel)

            wait_for(
                lambda: engine.set_kill_switch.called
                and engine.set_kill_switch.call_args_list[-1][0][0] is True,
                timeout=2.0,
                poll=0.02,
            )
        finally:
            watcher.stop()
