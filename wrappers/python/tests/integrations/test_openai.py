"""Tests for :class:`checkrd.integrations.OpenAIInstrumentor`.

Runs against a fake ``openai`` module (see ``conftest.py::fake_openai_module``)
so the test suite does not require the real ``openai`` package. A
separate smoke test is gated on the real package being installed.

Coverage matrix:

- Sync ``OpenAI()`` constructor is patched and its transport is wrapped.
- ``AsyncOpenAI()`` constructor is patched and its async transport is wrapped.
- User-supplied ``http_client=`` is preserved (not replaced) and its
  transport is wrapped.
- ``AzureOpenAI`` (subclass of ``OpenAI``) is covered transitively.
- Idempotency: double-wrapping is skipped via the transport marker.
- ``uninstrument()`` restores the original ``__init__`` on every patched
  class.
- Instrumentor can be re-used: ``instrument()`` -> ``uninstrument()`` ->
  ``instrument()`` works.
"""

from __future__ import annotations

import importlib.util

import httpx
import pytest

import checkrd
from checkrd.integrations import OpenAIInstrumentor
from checkrd.transports._httpx import CheckrdAsyncTransport, CheckrdTransport
from tests.conftest import requires_wasm

#: Reference for the "real openai is importable in this environment"
#: smoke test. Skipped when the package isn't present.
_HAS_REAL_OPENAI = importlib.util.find_spec("openai") is not None


@requires_wasm
class TestSyncInstrumentation:
    """The sync ``OpenAI`` client is the headline path: this is what
    80% of the SDK's users write."""

    def test_sync_client_transport_is_wrapped(
        self,
        fake_openai_module,
        initialized_checkrd,
    ) -> None:
        inst = OpenAIInstrumentor()
        inst.instrument()

        client = fake_openai_module.OpenAI(api_key="sk-test")
        assert isinstance(client._client._transport, CheckrdTransport), (
            "sync OpenAI transport should be wrapped by CheckrdTransport"
        )

    def test_original_transport_preserved(
        self,
        fake_openai_module,
        initialized_checkrd,
    ) -> None:
        # The user's original transport must be reachable via the
        # wrapper so HTTP requests still reach the real upstream.
        inst = OpenAIInstrumentor()
        inst.instrument()

        user_transport = httpx.MockTransport(
            lambda req: httpx.Response(200, json={"ok": True})
        )
        with httpx.Client(transport=user_transport) as user_client:
            client = fake_openai_module.OpenAI(
                api_key="sk-test", http_client=user_client
            )

            wrapper = client._client._transport
            assert isinstance(wrapper, CheckrdTransport)
            assert wrapper._transport is user_transport

    def test_default_client_still_works(
        self,
        fake_openai_module,
        initialized_checkrd,
    ) -> None:
        # The patched __init__ must not crash when the caller lets the
        # SDK build its own default client (the most common case).
        inst = OpenAIInstrumentor()
        inst.instrument()

        client = fake_openai_module.OpenAI(api_key="sk-test")
        assert isinstance(client._client, httpx.Client)


@requires_wasm
class TestAsyncInstrumentation:
    def test_async_client_transport_is_async_wrapper(
        self,
        fake_openai_module,
        initialized_checkrd,
    ) -> None:
        inst = OpenAIInstrumentor()
        inst.instrument()

        client = fake_openai_module.AsyncOpenAI(api_key="sk-test")
        assert isinstance(
            client._client._transport, CheckrdAsyncTransport
        ), "async OpenAI transport should be wrapped by CheckrdAsyncTransport"

    def test_async_user_transport_preserved(
        self,
        fake_openai_module,
        initialized_checkrd,
    ) -> None:
        inst = OpenAIInstrumentor()
        inst.instrument()

        user_transport = httpx.MockTransport(
            lambda req: httpx.Response(200)
        )
        user_client = httpx.AsyncClient(transport=user_transport)
        client = fake_openai_module.AsyncOpenAI(
            api_key="sk-test", http_client=user_client
        )
        wrapper = client._client._transport
        assert isinstance(wrapper, CheckrdAsyncTransport)
        assert wrapper._transport is user_transport


@requires_wasm
class TestSubclassCoverage:
    """AzureOpenAI inherits from OpenAI and delegates to super().__init__,
    so patching OpenAI covers it transitively without needing to list
    it in _target_classes. This test pins that behavior so future
    refactors don't silently lose Azure support."""

    def test_azure_subclass_is_covered_transitively(
        self,
        fake_openai_module,
        initialized_checkrd,
    ) -> None:
        inst = OpenAIInstrumentor()
        inst.instrument()

        azure_client = fake_openai_module.AzureOpenAI(api_key="sk-test")
        assert isinstance(
            azure_client._client._transport, CheckrdTransport
        ), "AzureOpenAI should inherit instrumentation via super().__init__"


@requires_wasm
class TestIdempotency:
    def test_instrument_twice_is_noop(
        self,
        fake_openai_module,
        initialized_checkrd,
    ) -> None:
        inst = OpenAIInstrumentor()
        inst.instrument()
        inst.instrument()  # should not double-wrap

        client = fake_openai_module.OpenAI(api_key="sk-test")
        transport = client._client._transport
        assert isinstance(transport, CheckrdTransport)
        # The wrapped inner transport is the original httpx one, not
        # another CheckrdTransport.
        assert not isinstance(transport._transport, CheckrdTransport)

    def test_pre_wrapped_client_not_double_wrapped(
        self,
        fake_openai_module,
        initialized_checkrd,
    ) -> None:
        # If the user passes a client whose transport is ALREADY a
        # CheckrdTransport (e.g., they called checkrd.wrap() on it
        # earlier), the instrumentor must detect it and skip. This is
        # the idempotency guarantee from the transport marker.
        inst = OpenAIInstrumentor()
        inst.instrument()
        with httpx.Client() as user_client:
            # Manually install a CheckrdTransport like wrap() would.
            checkrd.wrap(user_client, policy={"agent": "x", "default": "allow", "rules": []})
            outer_transport_before = user_client._transport
            assert isinstance(outer_transport_before, CheckrdTransport)

            client = fake_openai_module.OpenAI(
                api_key="sk-test", http_client=user_client
            )
            # Should be the SAME CheckrdTransport — not re-wrapped.
            assert client._client._transport is outer_transport_before


@requires_wasm
class TestUninstrument:
    def test_uninstrument_restores_sync_init(
        self,
        fake_openai_module,
        initialized_checkrd,
    ) -> None:
        original_init = fake_openai_module.OpenAI.__init__
        inst = OpenAIInstrumentor()
        inst.instrument()
        assert fake_openai_module.OpenAI.__init__ is not original_init
        inst.uninstrument()
        assert fake_openai_module.OpenAI.__init__ is original_init

    def test_uninstrument_restores_async_init(
        self,
        fake_openai_module,
        initialized_checkrd,
    ) -> None:
        original_async_init = fake_openai_module.AsyncOpenAI.__init__
        inst = OpenAIInstrumentor()
        inst.instrument()
        inst.uninstrument()
        assert fake_openai_module.AsyncOpenAI.__init__ is original_async_init

    def test_uninstrumented_clients_not_wrapped(
        self,
        fake_openai_module,
        initialized_checkrd,
    ) -> None:
        inst = OpenAIInstrumentor()
        inst.instrument()
        inst.uninstrument()

        client = fake_openai_module.OpenAI(api_key="sk-test")
        assert not isinstance(client._client._transport, CheckrdTransport)

    def test_instrument_uninstrument_instrument_cycle(
        self,
        fake_openai_module,
        initialized_checkrd,
    ) -> None:
        inst = OpenAIInstrumentor()
        inst.instrument()
        inst.uninstrument()
        inst.instrument()

        client = fake_openai_module.OpenAI(api_key="sk-test")
        assert isinstance(client._client._transport, CheckrdTransport)


@requires_wasm
class TestMissingTargetHandling:
    def test_openai_not_installed_is_silent_noop(
        self,
        initialized_checkrd,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Force the target module to look "not installed" by setting
        # sys.modules["openai"] = None. Python treats this as a failed
        # import cache entry: ``import openai`` raises ImportError, and
        # ``importlib.util.find_spec("openai")`` returns None. This
        # works even when the real openai package is installed and was
        # previously imported in this process.
        #
        # monkeypatch.setitem handles save/restore automatically — no
        # manual finally block needed.
        import sys

        monkeypatch.setitem(sys.modules, "openai", None)

        inst = OpenAIInstrumentor()
        inst.instrument()
        assert inst.instrumented is False


# ============================================================
# Optional real-library smoke test
# ============================================================


@pytest.mark.skipif(
    not _HAS_REAL_OPENAI,
    reason="openai package not installed; skipping real-library smoke test",
)
@requires_wasm
class TestRealOpenAISmoke:
    """Smoke test against the real ``openai`` package when available.

    This is the escape hatch that catches upstream API drift. The fakes
    cover the instrumentation contract; this test catches the case where
    a new openai version stops exposing ``_client`` on ``OpenAI``. Run
    ``pip install openai`` locally to enable it.
    """

    def test_real_openai_client_transport_wrapped(
        self,
        initialized_checkrd,
    ) -> None:
        import openai

        inst = OpenAIInstrumentor()
        try:
            inst.instrument()
            client = openai.OpenAI(api_key="sk-test-fake-key-not-real")
            transport = client._client._transport
            assert isinstance(transport, CheckrdTransport)
        finally:
            inst.uninstrument()
