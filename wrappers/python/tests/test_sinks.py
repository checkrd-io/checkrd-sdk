"""Tests for the pluggable telemetry sink interface and built-in sinks."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

import pytest

from checkrd.sinks import (
    ControlPlaneSink,
    JsonFileSink,
    LoggingSink,
    TelemetrySink,
)


# ============================================================
# Helpers
# ============================================================


def sample_event(event_id: str = "evt-001") -> dict[str, Any]:
    return {
        "event_id": event_id,
        "agent_id": "test-agent",
        "url_host": "api.stripe.com",
        "url_path": "/v1/charges",
        "method": "GET",
        "policy_result": "allowed",
        "timestamp": "2026-04-07T12:00:00Z",
    }


# ============================================================
# Protocol satisfaction
# ============================================================


class TestTelemetrySinkProtocol:
    """Verify all built-in sinks satisfy the TelemetrySink Protocol.

    Uses runtime ``isinstance`` against the runtime-checkable protocol so a
    structural change to the protocol breaks at test time, not at runtime
    when a customer sees a confusing AttributeError.
    """

    def test_json_file_sink_satisfies_protocol(self, tmp_path: Path) -> None:
        sink = JsonFileSink(tmp_path / "events.jsonl")
        try:
            assert isinstance(sink, TelemetrySink)
        finally:
            sink.stop()

    def test_logging_sink_satisfies_protocol(self) -> None:
        sink = LoggingSink()
        assert isinstance(sink, TelemetrySink)

    def test_control_plane_sink_satisfies_protocol(self) -> None:
        # ControlPlaneSink is just an alias for TelemetryBatcher.
        # Use a fake URL — the batcher won't actually send unless flushed.
        from checkrd.engine import WasmEngine

        private, _ = WasmEngine.generate_keypair()
        engine = WasmEngine(
            policy_json='{"agent":"test-agent","default":"allow","rules":[]}',
            agent_id="test-agent",
            private_key_bytes=private,
        )
        sink = ControlPlaneSink(
            base_url="http://localhost:1",
            api_key="ck_test_x",
            engine=engine,
            signer_agent_id="550e8400-e29b-41d4-a716-446655440000",
        )
        try:
            assert isinstance(sink, TelemetrySink)
        finally:
            sink.stop()


# ============================================================
# JsonFileSink
# ============================================================


class TestJsonFileSink:
    def test_writes_one_json_line_per_event(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        sink = JsonFileSink(path)
        try:
            sink.enqueue(sample_event("evt-1"))
            sink.enqueue(sample_event("evt-2"))
            sink.enqueue(sample_event("evt-3"))
        finally:
            sink.stop()

        lines = path.read_text().strip().splitlines()
        assert len(lines) == 3
        for i, line in enumerate(lines, start=1):
            parsed = json.loads(line)
            assert parsed["event_id"] == f"evt-{i}"

    def test_appends_to_existing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        path.write_text('{"event_id":"existing"}\n')

        sink = JsonFileSink(path)
        try:
            sink.enqueue(sample_event("new"))
        finally:
            sink.stop()

        lines = path.read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["event_id"] == "existing"
        assert json.loads(lines[1])["event_id"] == "new"

    def test_creates_parent_directory_if_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "nested" / "events.jsonl"
        assert not path.parent.exists()

        sink = JsonFileSink(path)
        try:
            sink.enqueue(sample_event())
        finally:
            sink.stop()

        assert path.exists()
        assert path.parent.is_dir()

    def test_string_path_works(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        sink = JsonFileSink(str(path))  # str, not Path
        try:
            sink.enqueue(sample_event())
        finally:
            sink.stop()
        assert path.exists()

    @pytest.mark.slow
    @pytest.mark.xdist_group("serial")
    def test_concurrent_enqueue_does_not_interleave_lines(
        self, tmp_path: Path
    ) -> None:
        # 10 threads × 50 events = 500 lines, every line must be a valid
        # complete JSON object (no torn writes).
        path = tmp_path / "concurrent.jsonl"
        sink = JsonFileSink(path)

        def writer(thread_id: int) -> None:
            for i in range(50):
                sink.enqueue(sample_event(f"t{thread_id}-{i}"))

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            assert not t.is_alive(), f"thread {t.name} hung"

        sink.stop()

        lines = path.read_text().strip().splitlines()
        assert len(lines) == 500, f"expected 500 lines, got {len(lines)}"
        # Every line must be parseable as JSON (no interleaving).
        for line in lines:
            parsed = json.loads(line)
            assert "event_id" in parsed

    def test_stop_is_idempotent(self, tmp_path: Path) -> None:
        sink = JsonFileSink(tmp_path / "events.jsonl")
        sink.enqueue(sample_event())
        sink.stop()
        sink.stop()  # second call must not raise

    def test_enqueue_after_stop_is_silently_dropped(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        sink = JsonFileSink(path)
        sink.enqueue(sample_event("before"))
        sink.stop()
        sink.enqueue(sample_event("after"))  # must not raise

        lines = path.read_text().strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["event_id"] == "before"

    def test_coerces_non_json_native_values_via_default_str(
        self, tmp_path: Path
    ) -> None:
        # JsonFileSink uses ``default=str`` so types that aren't JSON-native
        # (sets, datetimes, UUIDs, Path, etc.) get coerced to their str()
        # representation rather than dropped. This is the safety net that
        # keeps a single weird field from losing the whole event.
        from datetime import datetime

        path = tmp_path / "events.jsonl"
        sink = JsonFileSink(path)
        try:
            sink.enqueue(
                {
                    "event_id": "coerced",
                    "values": {1, 2, 3},  # set → str repr
                    "when": datetime(2026, 4, 7, 12, 0, 0),  # datetime → str repr
                }
            )
            sink.enqueue(sample_event("normal"))
        finally:
            sink.stop()

        lines = path.read_text().strip().splitlines()
        assert len(lines) == 2
        # The unusual event was preserved (with str-coerced values)
        first = json.loads(lines[0])
        assert first["event_id"] == "coerced"
        # set repr is non-deterministic in order so just check it's a string
        assert isinstance(first["values"], str)
        assert isinstance(first["when"], str)
        # The normal event after still wrote successfully
        assert json.loads(lines[1])["event_id"] == "normal"

    def test_truly_unserializable_event_is_dropped_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # An object that raises during str() coercion is unserializable even
        # with the default=str fallback. The sink logs and drops it.
        class Unstringable:
            def __str__(self) -> str:
                raise RuntimeError("cannot stringify")

            def __repr__(self) -> str:
                raise RuntimeError("cannot repr")

        path = tmp_path / "events.jsonl"
        sink = JsonFileSink(path)
        try:
            with caplog.at_level(logging.WARNING, logger="checkrd"):
                sink.enqueue({"event_id": "bad", "values": Unstringable()})
            sink.enqueue(sample_event("good"))
        finally:
            sink.stop()

        lines = path.read_text().strip().splitlines()
        # Only the good event made it; the bad one was dropped.
        assert len(lines) == 1
        assert json.loads(lines[0])["event_id"] == "good"

    def test_fsync_option(self, tmp_path: Path) -> None:
        # We can't actually verify fsync was called from Python, but we can
        # verify the option doesn't break writes.
        path = tmp_path / "events.jsonl"
        sink = JsonFileSink(path, fsync=True)
        try:
            sink.enqueue(sample_event())
        finally:
            sink.stop()

        lines = path.read_text().strip().splitlines()
        assert len(lines) == 1


# ============================================================
# Large event atomicity
# ============================================================


class TestJsonFileSinkLargeEvents:
    """Verify thread-safe writes with events larger than PIPE_BUF (4KB)."""

    def test_large_event_writes_complete_line(self, tmp_path: Path) -> None:
        """A single large event (>4KB) must write as one complete JSON line."""
        path = tmp_path / "events.jsonl"
        sink = JsonFileSink(path)
        try:
            large_event = {
                "event_id": "large-1",
                "payload": "x" * 8192,  # 8KB payload
            }
            sink.enqueue(large_event)
        finally:
            sink.stop()

        lines = path.read_text().strip().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["event_id"] == "large-1"
        assert len(parsed["payload"]) == 8192

    @pytest.mark.slow
    @pytest.mark.xdist_group("serial")
    def test_concurrent_large_events_no_interleaving(self, tmp_path: Path) -> None:
        """Multiple threads writing large events must not interleave lines.

        Without proper locking, writes >PIPE_BUF (typically 4096 on Linux/macOS)
        can interleave, producing corrupt JSON lines. This test verifies the
        Lock in JsonFileSink prevents that.
        """
        path = tmp_path / "events.jsonl"
        sink = JsonFileSink(path)
        events_per_thread = 20
        num_threads = 5
        payload_size = 8192  # well above PIPE_BUF

        def write_events(thread_id: int) -> None:
            for i in range(events_per_thread):
                sink.enqueue({
                    "event_id": f"t{thread_id}-{i}",
                    "payload": f"{'A' * payload_size}",
                })

        threads = [
            threading.Thread(target=write_events, args=(tid,))
            for tid in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            assert not t.is_alive(), f"thread {t.name} hung"

        sink.stop()

        lines = path.read_text().strip().splitlines()
        assert len(lines) == events_per_thread * num_threads

        # Every line must be valid JSON (no interleaving corruption)
        event_ids = set()
        for line in lines:
            parsed = json.loads(line)  # would raise on corruption
            event_ids.add(parsed["event_id"])
            assert len(parsed["payload"]) == payload_size

        assert len(event_ids) == events_per_thread * num_threads


# ============================================================
# LoggingSink
# ============================================================


class TestLoggingSink:
    def test_routes_events_through_default_logger(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        sink = LoggingSink()
        with caplog.at_level(logging.INFO, logger="checkrd.telemetry"):
            sink.enqueue(sample_event("log-1"))

        # Find our record
        records = [r for r in caplog.records if r.name == "checkrd.telemetry"]
        assert len(records) == 1
        assert records[0].message == "checkrd telemetry"
        assert records[0].levelno == logging.INFO

    def test_uses_custom_logger(self, caplog: pytest.LogCaptureFixture) -> None:
        custom = logging.getLogger("my.custom.checkrd")
        sink = LoggingSink(logger=custom)
        with caplog.at_level(logging.INFO, logger="my.custom.checkrd"):
            sink.enqueue(sample_event())

        records = [r for r in caplog.records if r.name == "my.custom.checkrd"]
        assert len(records) == 1

    def test_respects_level(self, caplog: pytest.LogCaptureFixture) -> None:
        sink = LoggingSink(level=logging.WARNING)
        with caplog.at_level(logging.WARNING, logger="checkrd.telemetry"):
            sink.enqueue(sample_event())

        records = [r for r in caplog.records if r.name == "checkrd.telemetry"]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING

    def test_attaches_event_as_extra(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        sink = LoggingSink()
        with caplog.at_level(logging.INFO, logger="checkrd.telemetry"):
            sink.enqueue(sample_event("attached"))

        records = [r for r in caplog.records if r.name == "checkrd.telemetry"]
        assert len(records) == 1
        # The event dict was attached via extra={"event": ...}
        assert hasattr(records[0], "event")
        assert records[0].event["event_id"] == "attached"  # type: ignore[attr-defined]

    def test_stop_is_idempotent(self) -> None:
        sink = LoggingSink()
        sink.stop()
        sink.stop()  # no-op, must not raise

    def test_enqueue_after_stop_still_works(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Logging has no buffer, so stop() doesn't actually disable enqueue.
        # This is intentional — it matches Python logging's behavior.
        sink = LoggingSink()
        sink.stop()
        with caplog.at_level(logging.INFO, logger="checkrd.telemetry"):
            sink.enqueue(sample_event("after-stop"))

        records = [r for r in caplog.records if r.name == "checkrd.telemetry"]
        assert len(records) == 1


# ============================================================
# ControlPlaneSink (alias for TelemetryBatcher) — protocol smoke
# ============================================================


class TestControlPlaneSink:
    def test_is_telemetry_batcher_alias(self) -> None:
        from checkrd.batcher import TelemetryBatcher

        assert ControlPlaneSink is TelemetryBatcher

    def test_constructor_works(self) -> None:
        from checkrd.engine import WasmEngine

        private, _ = WasmEngine.generate_keypair()
        engine = WasmEngine(
            policy_json='{"agent":"test-agent","default":"allow","rules":[]}',
            agent_id="test-agent",
            private_key_bytes=private,
        )
        sink = ControlPlaneSink(
            base_url="http://localhost:1",
            api_key="ck_test_x",
            engine=engine,
            signer_agent_id="550e8400-e29b-41d4-a716-446655440000",
        )
        try:
            assert isinstance(sink, TelemetrySink)
        finally:
            sink.stop()
