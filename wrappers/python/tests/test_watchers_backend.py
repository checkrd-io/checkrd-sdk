"""Tests for ``checkrd.watchers``'s backend-selection helper.

The two file watchers ship with a swappable change-detection backend:
``poll`` (default, stat-based) and ``watchdog`` (optional, OS-event-
based via inotify / FSEvents / ReadDirectoryChangesW). These tests
cover the selection logic in isolation, plus an end-to-end integration
test that the watchdog backend actually fires on a real file change.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from checkrd.exceptions import CheckrdInitError


class TestResolveBackend:
    """``_resolve_backend`` is the single decision point — every code
    path that picks a backend funnels through here, so the contract is
    worth pinning."""

    def test_poll_is_always_returned_unchanged(self) -> None:
        from checkrd.watchers import _resolve_backend

        assert _resolve_backend("poll") == "poll"

    def test_watchdog_returns_watchdog_when_installed(self) -> None:
        from checkrd.watchers import _resolve_backend

        # The dev install brings watchdog; this assertion would fail
        # if a future refactor accidentally dropped it from `[dev]`.
        assert _resolve_backend("watchdog") == "watchdog"

    def test_watchdog_raises_init_error_when_missing(self) -> None:
        # Simulate watchdog being absent by hiding it from the import
        # system. The helper must surface a clear error pointing at
        # the optional install path.
        from checkrd.watchers import _resolve_backend

        with patch.dict(sys.modules, {"watchdog": None}):
            with pytest.raises(CheckrdInitError) as exc_info:
                _resolve_backend("watchdog")
        assert "pip install" in str(exc_info.value)
        assert "watchdog" in str(exc_info.value)

    def test_auto_selects_watchdog_when_available(self) -> None:
        from checkrd.watchers import _resolve_backend

        assert _resolve_backend("auto") == "watchdog"

    def test_auto_falls_back_to_poll_when_watchdog_absent(self) -> None:
        from checkrd.watchers import _resolve_backend

        with patch.dict(sys.modules, {"watchdog": None}):
            assert _resolve_backend("auto") == "poll"


class TestBackendIntegration:
    """End-to-end: when ``backend='watchdog'`` is selected, real file
    changes must trigger the same reload path the polling backend
    triggers. The integration is the strongest test that the wiring
    works correctly across the Observer-handler boundary."""

    def test_policy_watcher_with_watchdog_reloads_on_real_change(
        self, tmp_path: object,
    ) -> None:
        import time
        from pathlib import Path

        from checkrd.watchers import PolicyFileWatcher
        from tests.conftest import wait_for

        td = tmp_path  # type: ignore[assignment]
        assert isinstance(td, Path)
        policy = td / "policy.yaml"
        policy.write_text("agent: a\ndefault: allow\nrules: []\n")

        engine = MagicMock()
        watcher = PolicyFileWatcher(
            engine, policy, interval_secs=60, backend="watchdog",
        )
        try:
            watcher.start()
            # Give the OS a tick to register the inotify/FSEvents watch.
            time.sleep(0.05)
            # Modify the file — watchdog should fire on_modified.
            policy.write_text("agent: a\ndefault: deny\nrules: []\n")
            wait_for(
                lambda: engine.reload_policy.called,
                timeout=3.0,
                poll=0.02,
            )
        finally:
            watcher.stop()

    def test_killswitch_watcher_with_watchdog_toggles_on_real_change(
        self, tmp_path: object,
    ) -> None:
        import time
        from pathlib import Path

        from checkrd.watchers import KillSwitchFileWatcher
        from tests.conftest import wait_for

        td = tmp_path  # type: ignore[assignment]
        assert isinstance(td, Path)
        sentinel = td / "killswitch"

        engine = MagicMock()
        watcher = KillSwitchFileWatcher(
            engine, sentinel, interval_secs=60, backend="watchdog",
        )
        try:
            watcher.start()
            time.sleep(0.05)
            # Create the sentinel — watchdog fires on_created → poll →
            # set_kill_switch(True).
            sentinel.touch()
            wait_for(
                lambda: engine.set_kill_switch.called
                and engine.set_kill_switch.call_args_list[-1][0][0] is True,
                timeout=3.0,
                poll=0.02,
            )
        finally:
            watcher.stop()

    def test_policy_watcher_falls_back_when_observer_construction_fails(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If watchdog is installed but the Observer fails to start
        (e.g., permission denied on the parent directory, inotify
        instance limit reached), the watcher falls back to polling
        rather than crashing the host process."""
        from pathlib import Path

        from checkrd import watchers as watchers_mod

        td = tmp_path  # type: ignore[assignment]
        assert isinstance(td, Path)
        policy = td / "policy.yaml"
        policy.write_text("agent: a\ndefault: allow\nrules: []\n")

        # Force the watchdog handle to throw at construction.
        monkeypatch.setattr(
            watchers_mod,
            "_WatchdogObserverHandle",
            MagicMock(side_effect=RuntimeError("inotify limit reached")),
        )

        engine = MagicMock()
        watcher = watchers_mod.PolicyFileWatcher(
            engine, policy, interval_secs=0.05, backend="watchdog",
        )
        try:
            watcher.start()
            # The polling fallback's first iteration runs after
            # `interval_secs`. Modify the file and wait for the
            # mtime-driven reload.
            import time

            time.sleep(0.1)
            policy.write_text("agent: a\ndefault: deny\nrules: []\n")
            from tests.conftest import wait_for

            wait_for(
                lambda: engine.reload_policy.called,
                timeout=2.0,
                poll=0.02,
            )
        finally:
            watcher.stop()
