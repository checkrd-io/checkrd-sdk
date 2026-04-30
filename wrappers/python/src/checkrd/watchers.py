"""Filesystem watchers for offline / Tier 3 deployments.

These two watchers turn the local filesystem into the control plane for
deployments that don't use the Checkrd cloud:

- :class:`PolicyFileWatcher` polls a policy YAML file's mtime and hot-reloads
  the WASM engine when it changes. Pair with config management (Ansible,
  Chef, GitOps, ArgoCD) to push policy updates without restarting agents.
- :class:`KillSwitchFileWatcher` polls the existence of a sentinel file and
  toggles the WASM kill switch on transitions. Pair with any orchestration
  that can ``touch`` or ``rm`` a file (cron, systemd, k8s configmap, etc.).

Both watchers run as daemon threads (so they don't block process exit) and
register an ``atexit`` cleanup hook. Both use simple polling rather than
``watchdog`` or ``inotify`` so the wrapper has zero new runtime dependencies.
The default poll interval is 5 seconds — sub-second responsiveness can be
configured by the caller, but the polling overhead is negligible at 5s.

Both watchers tolerate missing files at startup and handle invalid policy
content gracefully (log a warning, keep the previous state).
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
import weakref
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal, Optional, Union

import yaml

from checkrd._fork import register_fork_handler
from checkrd.exceptions import CheckrdInitError

if TYPE_CHECKING:
    from checkrd.engine import WasmEngine

logger = logging.getLogger("checkrd")

#: Default polling interval (seconds) for the file watchers. 5s gives near-
#: real-time response without burning CPU. Customers can override per watcher.
DEFAULT_POLL_INTERVAL_SECS = 5.0

#: Public type alias for the watcher backend selector. Mirrors the JS
#: SDK's pattern of letting users opt into a richer implementation when
#: they've installed the optional dependency.
FileWatcherBackend = Literal["auto", "watchdog", "poll"]

# Fork-safety registries. The ``os.register_at_fork`` handlers walk every
# live watcher in the forked child and call ``_reinit_after_fork`` on each.
# Same pattern as the telemetry batcher; see ``checkrd._fork``.
_LIVE_POLICY_WATCHERS: "weakref.WeakSet[PolicyFileWatcher]" = weakref.WeakSet()
_LIVE_KILLSWITCH_WATCHERS: "weakref.WeakSet[KillSwitchFileWatcher]" = (
    weakref.WeakSet()
)


def _resolve_backend(
    requested: FileWatcherBackend,
) -> Literal["watchdog", "poll"]:
    """Pick the concrete backend honoring availability of the optional
    ``watchdog`` dependency.

    Selection rules:

    - ``"poll"`` always returns ``"poll"`` (deterministic — used by tests
      and by operators who explicitly want stat-based detection).
    - ``"watchdog"`` returns ``"watchdog"`` when the package is
      importable, raises :class:`CheckrdInitError` otherwise (loud
      misconfiguration — operator asked for it but didn't install it).
    - ``"auto"`` (default) returns ``"watchdog"`` when available, else
      ``"poll"``. The polling fallback means the SDK keeps working
      unchanged in environments that haven't installed the extra.

    The watchdog backend uses inotify on Linux, FSEvents on macOS, and
    ReadDirectoryChangesW on Windows — sub-millisecond reaction to
    file changes vs the 5-second polling default. Recommended for
    production air-gapped deployments where policy changes must
    propagate immediately.
    """
    if requested == "poll":
        return "poll"
    if requested == "watchdog":
        try:
            import watchdog  # noqa: F401
        except ImportError as exc:
            raise CheckrdInitError(
                "backend='watchdog' requires the 'watchdog' package. "
                "Install with `pip install 'checkrd[watchdog]'` or pass "
                "backend='poll' to use the default mtime-polling backend.",
            ) from exc
        return "watchdog"
    # "auto"
    try:
        import watchdog  # noqa: F401
    except ImportError:
        return "poll"
    return "watchdog"


class _WatchdogObserverHandle:
    """Thin wrapper around ``watchdog.observers.Observer`` so the
    watcher classes can ignore the (lazy) import path. Construction
    is the only place watchdog is touched; everything else uses this
    handle.

    Watchdog watches *directories*, not files, so we register the
    parent and filter events by absolute path. The ``recursive=False``
    keeps the watch shallow — listing one directory's events is cheap
    even on busy mounts.
    """

    def __init__(
        self, target: Path, on_change: Callable[[], None],
    ) -> None:
        # Lazy imports — keeps `import checkrd.watchers` cheap and
        # callable in environments that don't install the optional dep.
        from watchdog.events import FileSystemEvent, FileSystemEventHandler
        from watchdog.observers import Observer

        target_str = str(target)

        class _Handler(FileSystemEventHandler):
            """Filter every directory-level event down to the single
            file we care about. Watchdog fires modify/create/delete
            on every change inside the watched directory; this handler
            ignores anything that isn't our target path."""

            def _is_target(self, event: FileSystemEvent) -> bool:
                return (
                    not event.is_directory
                    and event.src_path == target_str
                )

            def on_modified(self, event: FileSystemEvent) -> None:
                if self._is_target(event):
                    on_change()

            def on_created(self, event: FileSystemEvent) -> None:
                if self._is_target(event):
                    on_change()

            def on_deleted(self, event: FileSystemEvent) -> None:
                if self._is_target(event):
                    on_change()

            def on_moved(self, event: FileSystemEvent) -> None:
                # ``mv`` of a sibling onto our target shows up as a
                # moved event with ``dest_path`` matching. Polling
                # would catch this through mtime; watchdog won't
                # without explicit dest_path inspection.
                dest = getattr(event, "dest_path", None)
                if dest == target_str or self._is_target(event):
                    on_change()

        self._observer = Observer()
        self._observer.schedule(_Handler(), str(target.parent), recursive=False)

    def start(self) -> None:
        self._observer.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._observer.stop()
        self._observer.join(timeout=timeout)


class PolicyFileWatcher:
    """Watch a policy YAML file and hot-reload the WASM engine on changes.

    Polls ``os.stat(path).st_mtime`` every ``interval_secs``. On change,
    re-reads the file, parses YAML, and calls
    :meth:`WasmEngine.reload_policy`. If parsing fails, logs a warning and
    keeps the previously-loaded policy active — a malformed file never breaks
    a running agent.

    Args:
        engine: The WasmEngine to reload.
        path: Path to the policy YAML file.
        interval_secs: Poll interval. Default 5s.

    Example::

        watcher = PolicyFileWatcher(engine, "/etc/checkrd/policy.yaml")
        watcher.start()
        # ... agent runs ...
        watcher.stop()  # called automatically via atexit
    """

    def __init__(
        self,
        engine: WasmEngine,
        path: Union[str, Path],
        *,
        interval_secs: float = DEFAULT_POLL_INTERVAL_SECS,
        backend: FileWatcherBackend = "auto",
    ) -> None:
        self._engine = engine
        self._path = Path(path).resolve()
        self._interval = interval_secs
        self._backend = _resolve_backend(backend)

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._stopped = False
        self._pid = os.getpid()

        # Capture the initial mtime so the first poll cycle doesn't trigger
        # an unnecessary reload (the engine was just initialized with this
        # file's contents).
        self._last_mtime = self._safe_mtime()

        _LIVE_POLICY_WATCHERS.add(self)

    def _safe_mtime(self) -> Optional[float]:
        try:
            return self._path.stat().st_mtime
        except OSError:
            return None

    def _reinit_after_fork(self) -> None:
        """Reset thread state after ``os.fork()`` in the child process.

        Called by the module-level fork handler. Idempotent: a no-op
        when PID hasn't changed (handler ran in parent or test invoked
        manually).
        """
        pid = os.getpid()
        if pid == self._pid:
            return
        self._pid = pid
        self._stop_event = threading.Event()
        self._thread = None
        self._stopped = False
        logger.debug("checkrd: policy watcher reset after fork (pid=%d)", pid)

    def start(self) -> None:
        """Spawn the daemon thread. Idempotent. Fork-safe."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"checkrd-policy-watcher-{self._path.name}",
            daemon=True,
        )
        self._thread.start()
        atexit.register(self.stop)

    def stop(self) -> None:
        """Signal the watcher to stop and wait briefly for the thread to exit."""
        if self._stopped:
            return
        self._stopped = True
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=self._interval + 1.0)

    def _run(self) -> None:
        if self._backend == "watchdog":
            self._run_watchdog()
        else:
            self._run_polling()

    def _run_polling(self) -> None:
        """Stat-based change detection — the default. ``self._poll``
        re-reads the file when ``mtime`` advances; the loop sleeps on
        ``stop_event`` so shutdown is responsive."""
        while not self._stop_event.is_set():
            if self._stop_event.wait(timeout=self._interval):
                return
            self._poll()

    def _run_watchdog(self) -> None:
        """OS-event-based change detection — used when the optional
        ``watchdog`` package is installed. Reacts in microseconds vs
        the 5-second polling window. The handler invokes ``self._poll``
        on every relevant event so the same change-detection logic
        (mtime comparison + reload) covers both backends."""
        try:
            handle = _WatchdogObserverHandle(self._path, self._poll)
        except Exception as exc:
            logger.warning(
                "checkrd: failed to start watchdog observer for %s "
                "(%s); falling back to mtime polling",
                self._path,
                exc,
            )
            self._run_polling()
            return
        handle.start()
        try:
            # Run an initial poll so a change between init and start
            # isn't lost — parity with the polling backend's first
            # iteration.
            self._poll()
            self._stop_event.wait()
        finally:
            handle.stop()

    def _poll(self) -> None:
        current_mtime = self._safe_mtime()
        if current_mtime is None:
            # File missing — log once per transition and keep current policy.
            if self._last_mtime is not None:
                logger.warning(
                    "checkrd: policy file %s disappeared; keeping previous policy",
                    self._path,
                )
                self._last_mtime = None
            return

        if self._last_mtime is not None and current_mtime <= self._last_mtime:
            return  # unchanged

        # mtime advanced (or file just appeared) → attempt reload.
        try:
            content = self._path.read_text(encoding="utf-8")
            parsed = yaml.safe_load(content)
            if not isinstance(parsed, dict):
                raise CheckrdInitError(
                    f"policy file {self._path} must contain a YAML mapping, "
                    f"got {type(parsed).__name__}"
                )
            policy_json = json.dumps(parsed)
            self._engine.reload_policy(policy_json)
        except (OSError, yaml.YAMLError, CheckrdInitError, ValueError) as exc:
            logger.warning(
                "checkrd: failed to reload policy from %s (%s); "
                "keeping previous policy",
                self._path,
                exc,
            )
            # Don't update _last_mtime on failure — we'll retry next poll
            # if the file changes again.
            return

        logger.info("checkrd: reloaded policy from %s", self._path)
        self._last_mtime = current_mtime


class KillSwitchFileWatcher:
    """Watch a sentinel file and toggle the WASM kill switch on transitions.

    The presence of the file means "kill switch ON" (deny all requests). The
    absence means "kill switch OFF" (normal operation). Polled at
    ``interval_secs`` intervals; transitions in either direction call
    :meth:`WasmEngine.set_kill_switch`.

    Cooperates with the existing ``CHECKRD_DISABLED`` env var: the env var
    bypasses the entire SDK at wrap time, so an env-var-disabled agent is
    already inert regardless of file presence. The file watcher only matters
    for the normal (non-bypassed) flow.

    Args:
        engine: The WasmEngine to toggle.
        path: Path to the sentinel file.
        interval_secs: Poll interval. Default 5s.

    Example::

        watcher = KillSwitchFileWatcher(engine, "/var/lib/checkrd/killswitch")
        watcher.start()
        # Operator triggers: `touch /var/lib/checkrd/killswitch`
        # → next poll cycle, kill switch ON
        # Operator clears: `rm /var/lib/checkrd/killswitch`
        # → next poll cycle, kill switch OFF
    """

    def __init__(
        self,
        engine: WasmEngine,
        path: Union[str, Path],
        *,
        interval_secs: float = DEFAULT_POLL_INTERVAL_SECS,
        backend: FileWatcherBackend = "auto",
    ) -> None:
        self._engine = engine
        self._path = Path(path).resolve()
        self._interval = interval_secs
        self._backend = _resolve_backend(backend)

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._stopped = False
        self._pid = os.getpid()

        # Capture initial state so the first poll only acts on transitions.
        self._last_present = self._path.exists()
        # Apply the initial state to the engine immediately so wrap()-time
        # behavior reflects the file's existence at startup.
        if self._last_present:
            self._safe_set(True)

        _LIVE_KILLSWITCH_WATCHERS.add(self)

    def _reinit_after_fork(self) -> None:
        """Reset thread state after ``os.fork()`` in the child process.

        Called by the module-level fork handler. Idempotent.
        """
        pid = os.getpid()
        if pid == self._pid:
            return
        self._pid = pid
        self._stop_event = threading.Event()
        self._thread = None
        self._stopped = False
        logger.debug("checkrd: kill switch watcher reset after fork (pid=%d)", pid)

    def start(self) -> None:
        """Spawn the daemon thread. Idempotent. Fork-safe."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"checkrd-killswitch-watcher-{self._path.name}",
            daemon=True,
        )
        self._thread.start()
        atexit.register(self.stop)

    def stop(self) -> None:
        """Signal the watcher to stop and wait briefly for the thread to exit."""
        if self._stopped:
            return
        self._stopped = True
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=self._interval + 1.0)

    def _run(self) -> None:
        if self._backend == "watchdog":
            self._run_watchdog()
        else:
            self._run_polling()

    def _run_polling(self) -> None:
        while not self._stop_event.is_set():
            if self._stop_event.wait(timeout=self._interval):
                return
            self._poll()

    def _run_watchdog(self) -> None:
        """Mirror of :meth:`PolicyFileWatcher._run_watchdog` — the
        sentinel-file watcher reacts to ``on_created`` / ``on_deleted``
        events directly via the same poll-on-event pattern."""
        try:
            handle = _WatchdogObserverHandle(self._path, self._poll)
        except Exception as exc:
            logger.warning(
                "checkrd: failed to start watchdog observer for %s "
                "(%s); falling back to existence polling",
                self._path,
                exc,
            )
            self._run_polling()
            return
        handle.start()
        try:
            self._poll()
            self._stop_event.wait()
        finally:
            handle.stop()

    def _poll(self) -> None:
        present = self._path.exists()
        if present == self._last_present:
            return  # no transition
        self._safe_set(present)
        self._last_present = present
        logger.info(
            "checkrd: kill switch %s (sentinel file %s %s)",
            "ENABLED" if present else "DISABLED",
            self._path,
            "appeared" if present else "removed",
        )

    def _safe_set(self, active: bool) -> None:
        try:
            self._engine.set_kill_switch(active)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "checkrd: failed to set kill switch (%s); will retry on next poll",
                exc,
            )


# Used by tests to set the polling interval much lower for fast feedback.
# Production code should NOT call this — use the constructor parameter.
def _set_default_poll_interval_for_tests(interval_secs: float) -> None:
    """Test-only: override the default poll interval globally."""
    global DEFAULT_POLL_INTERVAL_SECS
    DEFAULT_POLL_INTERVAL_SECS = interval_secs


# Register the at-fork handlers. Placed at module bottom (after both
# class definitions) so the forward-references inside the WeakSets are
# resolved by the time the handlers walk the registries.
register_fork_handler(
    _LIVE_POLICY_WATCHERS, "_reinit_after_fork", "policy file watcher",
)
register_fork_handler(
    _LIVE_KILLSWITCH_WATCHERS, "_reinit_after_fork", "kill switch file watcher",
)
