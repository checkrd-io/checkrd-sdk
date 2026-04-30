"""Tests for :class:`checkrd.integrations.GroqInstrumentor`.

``GroqInstrumentor`` is a thin subclass of
``HttpxClientInstrumentor`` -- the core contract is already covered by
``test_base.py``. This file verifies the Groq-specific declarations
(module name, target classes) wire up correctly against a fake
``groq`` module.
"""

from __future__ import annotations

import importlib.util

import httpx
import pytest

from checkrd.integrations import GroqInstrumentor
from checkrd.transports._httpx import CheckrdAsyncTransport, CheckrdTransport
from tests.conftest import requires_wasm

_HAS_REAL_GROQ = importlib.util.find_spec("groq") is not None


@requires_wasm
class TestGroqInstrumentation:
    def test_sync_client_wrapped(
        self,
        fake_groq_module,
        initialized_checkrd,
    ) -> None:
        inst = GroqInstrumentor()
        inst.instrument()

        client = fake_groq_module.Groq(api_key="gsk-test")
        assert isinstance(client._client._transport, CheckrdTransport)

    def test_async_client_wrapped(
        self,
        fake_groq_module,
        initialized_checkrd,
    ) -> None:
        inst = GroqInstrumentor()
        inst.instrument()

        client = fake_groq_module.AsyncGroq(api_key="gsk-test")
        assert isinstance(client._client._transport, CheckrdAsyncTransport)

    def test_user_transport_preserved(
        self,
        fake_groq_module,
        initialized_checkrd,
    ) -> None:
        inst = GroqInstrumentor()
        inst.instrument()

        user_transport = httpx.MockTransport(lambda req: httpx.Response(200))
        with httpx.Client(transport=user_transport) as user_client:
            client = fake_groq_module.Groq(
                api_key="gsk-test", http_client=user_client
            )

            wrapper = client._client._transport
            assert isinstance(wrapper, CheckrdTransport)
            assert wrapper._transport is user_transport

    def test_uninstrument_restores(
        self,
        fake_groq_module,
        initialized_checkrd,
    ) -> None:
        original = fake_groq_module.Groq.__init__
        inst = GroqInstrumentor()
        inst.instrument()
        assert fake_groq_module.Groq.__init__ is not original
        inst.uninstrument()
        assert fake_groq_module.Groq.__init__ is original

    def test_idempotent_instrument(
        self,
        fake_groq_module,
        initialized_checkrd,
    ) -> None:
        inst = GroqInstrumentor()
        inst.instrument()
        inst.instrument()

        client = fake_groq_module.Groq(api_key="gsk-test")
        wrapper = client._client._transport
        assert isinstance(wrapper, CheckrdTransport)
        # Inner transport is the raw httpx default, not another layer.
        assert not isinstance(wrapper._transport, CheckrdTransport)

    def test_target_classes_declaration(self) -> None:
        assert GroqInstrumentor._target_module_name == "groq"
        assert set(GroqInstrumentor._target_classes) == {
            "Groq",
            "AsyncGroq",
        }


@pytest.mark.skipif(
    not _HAS_REAL_GROQ,
    reason="groq package not installed; skipping real-library smoke test",
)
@requires_wasm
class TestRealGroqSmoke:
    def test_real_groq_client_transport_wrapped(
        self,
        initialized_checkrd,
    ) -> None:
        import groq

        inst = GroqInstrumentor()
        try:
            inst.instrument()
            client = groq.Groq(api_key="gsk-test-fake-key")
            transport = client._client._transport
            assert isinstance(transport, CheckrdTransport)
        finally:
            inst.uninstrument()
