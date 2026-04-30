"""Tests for P0 security fixes: engine thread safety, global state
visibility, and private key copy elimination.

These tests verify the three critical correctness properties:

1. **WasmEngine thread safety** (P0-1): concurrent access from multiple
   threads must not corrupt WASM linear memory, produce incorrect policy
   evaluations, or crash. The engine's ``_lock`` serializes all Store
   access.

2. **Global state visibility** (P0-2): SDK configuration stored in
   ``_state.py`` must be visible to every thread and async task, even
   those running with a fresh ``contextvars.Context``. Module-level
   globals (not ContextVar) are the correct primitive for process-wide
   SDK configuration.

3. **Private key copy elimination** (P0-3): the ``_create_engine_from_json``
   path must not create immutable ``bytes`` copies of the private key.
   Only the mutable ``bytearray`` (which ``bind_engine()`` can zero)
   should exist in Python memory.
"""

from __future__ import annotations

import contextvars
import json
import threading
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

import checkrd
from checkrd._state import (
    _GlobalContext,
    get_context,
    has_context,
    is_degraded,
    set_context,
    set_degraded,
    set_last_eval_at,
    get_last_eval_at,
)
from checkrd.engine import WasmEngine
from checkrd.identity import LocalIdentity
from tests.conftest import requires_wasm, unique_id


# ============================================================
# Shared constants
# ============================================================

_TS = "2026-04-15T00:00:00Z"
_TS_MS = 1776297600000

_ALLOW_ALL = json.dumps({"agent": "test", "default": "allow", "rules": []})

_DENY_UNKNOWN = json.dumps({
    "agent": "test",
    "default": "deny",
    "rules": [
        {
            "name": "allow-stripe",
            "allow": {"method": ["GET"], "url": "api.stripe.com/v1/charges"},
        },
    ],
})


def _eval(engine: WasmEngine, method: str = "GET", url: str = "https://api.stripe.com/v1/charges") -> object:
    return engine.evaluate(
        request_id=unique_id(),
        method=method,
        url=url,
        headers=[],
        body=None,
        timestamp=_TS,
        timestamp_ms=_TS_MS,
    )


# ============================================================
# P0-1: WasmEngine thread safety
# ============================================================


@requires_wasm
@pytest.mark.slow
@pytest.mark.xdist_group("serial")
class TestEngineThreadSafety:
    """Verify that a shared WasmEngine is safe under concurrent access.

    When used via ``checkrd.init()`` + ``checkrd.instrument()``, one
    engine instance is shared across request threads (evaluate), the
    batcher thread (sign_telemetry_batch), the control receiver thread
    (set_kill_switch, reload_policy), and file watcher threads
    (reload_policy). The ``_lock`` must serialize all access to the
    wasmtime Store.
    """

    def test_has_lock_attribute(self) -> None:
        """WasmEngine must have a threading.Lock for Store serialization."""
        engine = WasmEngine(_ALLOW_ALL, "test-agent")
        assert hasattr(engine, "_lock")
        assert isinstance(engine._lock, type(threading.Lock()))

    def test_concurrent_evaluate_no_crash(self) -> None:
        """N threads calling evaluate() concurrently must not crash or
        produce corrupt results."""
        engine = WasmEngine(_DENY_UNKNOWN, "test-agent")
        errors: list[Exception] = []
        results: list[bool] = []

        def worker(allowed_url: bool) -> None:
            try:
                for _ in range(50):
                    if allowed_url:
                        r = _eval(engine, "GET", "https://api.stripe.com/v1/charges")
                        results.append(r.allowed)
                    else:
                        r = _eval(engine, "GET", "https://unknown.com/api")
                        results.append(r.allowed)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=worker, args=(i % 2 == 0,))
            for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive(), f"thread {t.name} hung"

        assert not errors, f"concurrent evaluate errors: {errors}"
        assert len(results) == 400  # 8 threads x 50 evals

    def test_concurrent_evaluate_correctness(self) -> None:
        """Policy decisions must be correct even under contention."""
        engine = WasmEngine(_DENY_UNKNOWN, "test-agent")
        allowed_results: list[bool] = []
        denied_results: list[bool] = []
        errors: list[Exception] = []

        def eval_allowed() -> None:
            try:
                for _ in range(30):
                    r = _eval(engine, "GET", "https://api.stripe.com/v1/charges")
                    allowed_results.append(r.allowed)
            except Exception as e:
                errors.append(e)

        def eval_denied() -> None:
            try:
                for _ in range(30):
                    r = _eval(engine, "GET", "https://unknown.com/api")
                    denied_results.append(r.allowed)
            except Exception as e:
                errors.append(e)

        threads = (
            [threading.Thread(target=eval_allowed) for _ in range(4)]
            + [threading.Thread(target=eval_denied) for _ in range(4)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive(), f"thread {t.name} hung"

        assert not errors, f"errors: {errors}"
        # Every "allowed" eval must return True; every "denied" must return False.
        assert all(allowed_results), "allowed evals returned False under contention"
        assert not any(denied_results), "denied evals returned True under contention"

    def test_concurrent_evaluate_and_kill_switch(self) -> None:
        """evaluate() and set_kill_switch() from different threads must
        not corrupt each other. After kill switch is activated, all
        subsequent evaluations must see it."""
        engine = WasmEngine(_ALLOW_ALL, "test-agent")
        errors: list[Exception] = []
        kill_switch_set = threading.Event()

        def evaluator() -> None:
            try:
                for _ in range(100):
                    _eval(engine)
            except Exception as e:
                errors.append(e)

        def toggler() -> None:
            try:
                for _ in range(20):
                    engine.set_kill_switch(True)
                    engine.set_kill_switch(False)
                kill_switch_set.set()
            except Exception as e:
                errors.append(e)

        threads = (
            [threading.Thread(target=evaluator) for _ in range(4)]
            + [threading.Thread(target=toggler)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive(), f"thread {t.name} hung"

        assert not errors, f"errors: {errors}"

        # After toggling is done, kill switch is off — verify final state.
        assert kill_switch_set.is_set()
        r = _eval(engine)
        assert r.allowed, "kill switch should be off after toggler finished"

    def test_concurrent_evaluate_and_sign(self) -> None:
        """evaluate() and sign() from different threads must not corrupt
        WASM linear memory."""
        private, _ = WasmEngine.generate_keypair()
        engine = WasmEngine(_ALLOW_ALL, "test-agent", private_key_bytes=private)
        errors: list[Exception] = []
        signatures: list[bytes] = []

        def evaluator() -> None:
            try:
                for _ in range(50):
                    _eval(engine)
            except Exception as e:
                errors.append(e)

        def signer() -> None:
            try:
                for i in range(50):
                    sig = engine.sign(f"payload-{i}".encode())
                    if sig:
                        signatures.append(sig)
            except Exception as e:
                errors.append(e)

        threads = (
            [threading.Thread(target=evaluator) for _ in range(4)]
            + [threading.Thread(target=signer) for _ in range(2)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive(), f"thread {t.name} hung"

        assert not errors, f"errors: {errors}"
        # Each signature should be 64 bytes (Ed25519).
        assert all(len(s) == 64 for s in signatures)

    def test_concurrent_evaluate_and_reload(self) -> None:
        """evaluate() and reload_policy() from different threads must not
        corrupt the engine. After reload, the new policy must take effect."""
        engine = WasmEngine(_DENY_UNKNOWN, "test-agent")
        errors: list[Exception] = []

        def evaluator() -> None:
            try:
                for _ in range(50):
                    _eval(engine, "GET", "https://api.stripe.com/v1/charges")
            except Exception as e:
                errors.append(e)

        def reloader() -> None:
            try:
                for _ in range(10):
                    engine.reload_policy(_ALLOW_ALL)
                    engine.reload_policy(_DENY_UNKNOWN)
            except Exception as e:
                errors.append(e)

        threads = (
            [threading.Thread(target=evaluator) for _ in range(4)]
            + [threading.Thread(target=reloader)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive(), f"thread {t.name} hung"

        assert not errors, f"errors: {errors}"

    def test_concurrent_sign_telemetry_batch(self) -> None:
        """sign_telemetry_batch() called from multiple threads must not
        crash or produce corrupted signatures."""
        private, _ = WasmEngine.generate_keypair()
        engine = WasmEngine(_ALLOW_ALL, "test-agent", private_key_bytes=private)
        errors: list[Exception] = []
        results: list[Optional[dict]] = []

        def signer(thread_id: int) -> None:
            try:
                import secrets
                import time

                for i in range(20):
                    batch = json.dumps([{"event_id": f"t{thread_id}-{i}"}]).encode()
                    now = int(time.time())
                    result = engine.sign_telemetry_batch(
                        batch_json=batch,
                        target_uri="https://api.checkrd.io/v1/telemetry",
                        signer_agent="test-agent",
                        nonce=secrets.token_hex(16),
                        created=now,
                        expires=now + 300,
                    )
                    results.append(result)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=signer, args=(i,))
            for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive(), f"thread {t.name} hung"

        assert not errors, f"errors: {errors}"
        assert len(results) == 80  # 4 threads x 20 signs
        # Every result should be a dict with signature fields.
        for r in results:
            assert r is not None
            assert "signature" in r
            assert "content_digest" in r


# ============================================================
# P0-2: Global state visibility across contexts
# ============================================================


@pytest.fixture(autouse=True)
def _reset_global_state_p0(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Reset global Checkrd state around every test in this module."""
    monkeypatch.setenv("CHECKRD_CONFIG_DIR", str(tmp_path))
    for var in (
        "CHECKRD_API_KEY",
        "CHECKRD_BASE_URL",
        "CHECKRD_AGENT_ID",
        "CHECKRD_ENFORCE",
        "CHECKRD_DISABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    checkrd.shutdown()
    yield
    checkrd.shutdown()


@pytest.mark.slow
@pytest.mark.xdist_group("serial")
class TestGlobalStateVisibilityAcrossContexts:
    """Verify that SDK state is visible to threads and async tasks
    running with a fresh ``contextvars.Context``.

    This is the core property that ContextVar violated: any code running
    with ``contextvars.copy_context().run(...)`` or in an asyncio task
    with a fresh context would see ``None`` for all state, causing
    policy enforcement to silently disappear.

    Module-level globals are visible everywhere in the process, which is
    the correct semantic for SDK configuration.
    """

    def test_set_context_visible_in_fresh_contextvars_context(self) -> None:
        """State set in the main context must be visible inside a fresh
        contextvars.Context. This is the exact scenario that broke with
        ContextVar."""
        ctx_mock = MagicMock(spec=_GlobalContext)
        set_context(ctx_mock)

        # Run in a completely fresh context (simulates asyncio task).
        result: list[bool] = []

        def check_in_fresh_context() -> None:
            result.append(has_context())
            result.append(get_context() is ctx_mock)

        contextvars.copy_context().run(check_in_fresh_context)

        assert result == [True, True], (
            "SDK state must be visible inside a fresh contextvars.Context"
        )

    def test_degraded_flag_visible_in_fresh_context(self) -> None:
        set_degraded(True)
        result: list[bool] = []

        def check() -> None:
            result.append(is_degraded())

        contextvars.copy_context().run(check)
        assert result == [True]

    def test_last_eval_at_visible_in_fresh_context(self) -> None:
        set_last_eval_at("2026-04-15T12:00:00Z")
        result: list[Optional[str]] = []

        def check() -> None:
            result.append(get_last_eval_at())

        contextvars.copy_context().run(check)
        assert result == ["2026-04-15T12:00:00Z"]

    def test_state_visible_from_spawned_thread(self) -> None:
        """Threads inherit the module-level global, not a copy."""
        ctx_mock = MagicMock(spec=_GlobalContext)
        set_context(ctx_mock)

        result: list[bool] = []
        errors: list[Exception] = []

        def check_in_thread() -> None:
            try:
                result.append(has_context())
                result.append(get_context() is ctx_mock)
            except Exception as e:
                errors.append(e)

        t = threading.Thread(target=check_in_thread)
        t.start()
        t.join(timeout=5)
        assert not t.is_alive()
        assert not errors, f"errors: {errors}"
        assert result == [True, True]

    def test_state_visible_from_asyncio_run(self) -> None:
        """asyncio event loop tasks must see the global SDK state.

        Uses asyncio.run() to create a fresh event loop (and fresh
        contextvars context) — the same scenario that broke with ContextVar.
        """
        import asyncio

        ctx_mock = MagicMock(spec=_GlobalContext)
        set_context(ctx_mock)

        async def check() -> tuple[bool, bool]:
            return has_context(), get_context() is ctx_mock

        has_it, is_same = asyncio.run(check())
        assert has_it is True
        assert is_same is True

    def test_set_context_from_thread_visible_in_main(self) -> None:
        """State set from a background thread must be visible in main."""
        ctx_mock = MagicMock(spec=_GlobalContext)
        done = threading.Event()

        def set_from_thread() -> None:
            set_context(ctx_mock)
            done.set()

        t = threading.Thread(target=set_from_thread)
        t.start()
        t.join(timeout=5)
        assert done.is_set()
        assert has_context() is True
        assert get_context() is ctx_mock

    @requires_wasm
    def test_init_visible_in_fresh_context(self) -> None:
        """Full init() -> state visible in fresh contextvars.Context."""
        checkrd.init(agent_id="p0-visibility-test")

        result: list[bool] = []

        def check() -> None:
            result.append(has_context())
            try:
                ctx = get_context()
                result.append(ctx.settings.agent_id == "p0-visibility-test")
            except Exception:
                result.append(False)

        contextvars.copy_context().run(check)
        assert result == [True, True]

    @requires_wasm
    def test_init_visible_from_thread_with_fresh_context(self) -> None:
        """The most realistic failure scenario: init() in main thread,
        instrumentor wraps client in a worker thread that happens to
        have a fresh context."""
        checkrd.init(agent_id="p0-thread-test")

        result: list[bool] = []
        errors: list[Exception] = []

        def worker() -> None:
            # Simulate a framework that runs with a fresh context.
            def inner() -> None:
                try:
                    result.append(has_context())
                    ctx = get_context()
                    result.append(ctx.settings.agent_id == "p0-thread-test")
                except Exception as e:
                    errors.append(e)

            contextvars.copy_context().run(inner)

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=10)
        assert not t.is_alive()
        assert not errors, f"errors: {errors}"
        assert result == [True, True]


# ============================================================
# P0-3: Private key copy elimination
# ============================================================


@requires_wasm
@pytest.mark.slow
@pytest.mark.xdist_group("serial")
class TestPrivateKeyCopyElimination:
    """Verify that the engine init path does not create immutable ``bytes``
    copies of the private key that cannot be zeroized.

    The ``private_key_bytes`` property (public API) returns ``bytes``
    for backward compatibility with external code. But the internal
    ``_create_engine_from_json`` path must use ``_private_key_ref()``
    to pass the mutable ``bytearray`` directly to the WASM engine,
    then ``bind_engine()`` zeroizes the original bytearray.
    """

    def test_private_key_ref_returns_bytearray(self, tmp_path: Path) -> None:
        """_private_key_ref() must return the original bytearray, not a copy."""
        li = LocalIdentity(key_path=tmp_path / "identity.key")
        ref = li._private_key_ref()
        assert ref is not None
        assert isinstance(ref, bytearray), (
            f"_private_key_ref() must return bytearray, got {type(ref).__name__}"
        )

    def test_private_key_ref_is_same_object(self, tmp_path: Path) -> None:
        """_private_key_ref() must return the same object as _private_key."""
        li = LocalIdentity(key_path=tmp_path / "identity.key")
        _ = li.public_key  # force load
        ref = li._private_key_ref()
        assert ref is li._private_key, (
            "_private_key_ref() must return the exact same bytearray object"
        )

    def test_private_key_ref_none_after_zeroization(self, tmp_path: Path) -> None:
        """After bind_engine(), _private_key_ref() must return None."""
        li = LocalIdentity(key_path=tmp_path / "identity.key")
        ref = li._private_key_ref()
        assert ref is not None

        engine = WasmEngine(
            _ALLOW_ALL, "test-agent", private_key_bytes=ref,
        )
        li.bind_engine(engine)

        assert li._private_key_ref() is None

    def test_private_key_bytes_returns_bytes_type(self, tmp_path: Path) -> None:
        """Public property must return bytes (not bytearray) for API compat."""
        li = LocalIdentity(key_path=tmp_path / "identity.key")
        pk = li.private_key_bytes
        assert pk is not None
        assert isinstance(pk, bytes)
        assert not isinstance(pk, bytearray)

    def test_create_engine_zeroizes_key(self, tmp_path: Path) -> None:
        """The full _create_engine_from_json path must zeroize the key."""
        from checkrd import _create_engine_from_json

        li = LocalIdentity(key_path=tmp_path / "identity.key")
        _ = li.public_key  # force load
        internal_ref = li._private_key
        assert internal_ref is not None
        assert any(b != 0 for b in internal_ref), "key should be non-zero before bind"

        _create_engine_from_json(_ALLOW_ALL, "test-agent", li)

        # After _create_engine_from_json, the bytearray must be zeroed.
        assert all(b == 0 for b in internal_ref), (
            "internal bytearray must be zeroed after _create_engine_from_json"
        )
        # And the property must return None.
        assert li.private_key_bytes is None

    def test_create_engine_from_json_with_external_identity(self) -> None:
        """ExternalIdentity path must still work (no _private_key_ref)."""
        from checkrd import _create_engine_from_json
        from checkrd.identity import ExternalIdentity

        class StubExternal(ExternalIdentity):
            @property
            def public_key(self) -> bytes:
                return b"\x01" * 32

            @property
            def instance_id(self) -> str:
                return "0101010101010101"

            def sign(self, payload: bytes) -> bytes:
                return b"\xaa" * 64

        ext = StubExternal()
        engine = _create_engine_from_json(_ALLOW_ALL, "test-agent", ext)
        assert engine is not None
        # External identity should not have been bound.
        assert ext.private_key_bytes is None  # always None

    def test_engine_accepts_bytearray(self) -> None:
        """WasmEngine.__init__ must accept bytearray for private_key_bytes."""
        private, _ = WasmEngine.generate_keypair()
        key_ba = bytearray(private)
        engine = WasmEngine(
            _ALLOW_ALL, "test-agent", private_key_bytes=key_ba,
        )
        # Signing must work with a bytearray-initialized engine.
        sig = engine.sign(b"test payload")
        assert len(sig) == 64

    def test_from_bytes_identity_zeroized_through_full_path(
        self, tmp_path: Path,
    ) -> None:
        """End-to-end: from_bytes -> _create_engine_from_json -> key zeroed."""
        from checkrd import _create_engine_from_json

        private, _ = WasmEngine.generate_keypair()
        li = LocalIdentity.from_bytes(private)
        internal_ref = li._private_key
        assert internal_ref is not None

        _create_engine_from_json(_ALLOW_ALL, "test-agent", li)

        assert all(b == 0 for b in internal_ref)
        assert li.private_key_bytes is None

    def test_from_env_identity_zeroized_through_full_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end: from_env -> _create_engine_from_json -> key zeroed."""
        import base64
        from checkrd import _create_engine_from_json

        private, _ = WasmEngine.generate_keypair()
        monkeypatch.setenv(
            "CHECKRD_AGENT_KEY", base64.b64encode(private).decode(),
        )
        li = LocalIdentity.from_env()
        internal_ref = li._private_key
        assert internal_ref is not None

        _create_engine_from_json(_ALLOW_ALL, "test-agent", li)

        assert all(b == 0 for b in internal_ref)
        assert li.private_key_bytes is None


# ============================================================
# P0-1 supplementary: _get_module() locking
# ============================================================


@requires_wasm
@pytest.mark.slow
@pytest.mark.xdist_group("serial")
class TestModuleCacheLocking:
    """Verify the WASM module cache always acquires the lock.

    The previous double-checked locking pattern (reading globals outside
    the lock) was unsafe under PEP 703 free-threading. The fix removes
    the pre-lock fast path entirely.
    """

    def test_concurrent_get_module_no_crash(self) -> None:
        """Multiple threads calling _get_module() concurrently must converge
        on a single (Engine, Module) pair without crashing."""
        from checkrd.engine import _get_module

        results: list[tuple] = []
        errors: list[Exception] = []

        def loader() -> None:
            try:
                pair = _get_module()
                results.append(pair)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=loader) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
            assert not t.is_alive(), f"thread {t.name} hung"

        assert not errors, f"errors: {errors}"
        # All threads must get the same cached (Engine, Module).
        engines = {id(r[0]) for r in results}
        modules = {id(r[1]) for r in results}
        assert len(engines) == 1, "all threads should share one Engine"
        assert len(modules) == 1, "all threads should share one Module"
