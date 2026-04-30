"""Tests for :class:`checkrd.integrations.TogetherInstrumentor`.

``TogetherInstrumentor`` is a thin subclass of
``HttpxClientInstrumentor`` -- the core contract is already covered by
``test_base.py``. This file verifies the Together-specific declarations
(module name, target classes) wire up correctly against a fake
``together`` module.
"""

from __future__ import annotations

import importlib.util

import httpx
import pytest

from checkrd.integrations import TogetherInstrumentor
from checkrd.transports._httpx import CheckrdAsyncTransport, CheckrdTransport
from tests.conftest import requires_wasm

_HAS_REAL_TOGETHER = importlib.util.find_spec("together") is not None


@requires_wasm
class TestTogetherInstrumentation:
    def test_sync_client_wrapped(
        self,
        fake_together_module,
        initialized_checkrd,
    ) -> None:
        inst = TogetherInstrumentor()
        inst.instrument()

        client = fake_together_module.Together(api_key="tog-test")
        assert isinstance(client._client._transport, CheckrdTransport)

    def test_async_client_wrapped(
        self,
        fake_together_module,
        initialized_checkrd,
    ) -> None:
        inst = TogetherInstrumentor()
        inst.instrument()

        client = fake_together_module.AsyncTogether(api_key="tog-test")
        assert isinstance(client._client._transport, CheckrdAsyncTransport)

    def test_user_transport_preserved(
        self,
        fake_together_module,
        initialized_checkrd,
    ) -> None:
        inst = TogetherInstrumentor()
        inst.instrument()

        user_transport = httpx.MockTransport(lambda req: httpx.Response(200))
        with httpx.Client(transport=user_transport) as user_client:
            client = fake_together_module.Together(
                api_key="tog-test", http_client=user_client
            )

            wrapper = client._client._transport
            assert isinstance(wrapper, CheckrdTransport)
            assert wrapper._transport is user_transport

    def test_uninstrument_restores(
        self,
        fake_together_module,
        initialized_checkrd,
    ) -> None:
        original = fake_together_module.Together.__init__
        inst = TogetherInstrumentor()
        inst.instrument()
        assert fake_together_module.Together.__init__ is not original
        inst.uninstrument()
        assert fake_together_module.Together.__init__ is original

    def test_idempotent_instrument(
        self,
        fake_together_module,
        initialized_checkrd,
    ) -> None:
        inst = TogetherInstrumentor()
        inst.instrument()
        inst.instrument()

        client = fake_together_module.Together(api_key="tog-test")
        wrapper = client._client._transport
        assert isinstance(wrapper, CheckrdTransport)
        # Inner transport is the raw httpx default, not another layer.
        assert not isinstance(wrapper._transport, CheckrdTransport)

    def test_target_classes_declaration(self) -> None:
        assert TogetherInstrumentor._target_module_name == "together"
        assert set(TogetherInstrumentor._target_classes) == {
            "Together",
            "AsyncTogether",
        }


@pytest.mark.skipif(
    not _HAS_REAL_TOGETHER,
    reason="together package not installed; skipping real-library smoke test",
)
@requires_wasm
class TestRealTogetherSmoke:
    def test_real_together_client_transport_wrapped(
        self,
        initialized_checkrd,
    ) -> None:
        import together

        inst = TogetherInstrumentor()
        try:
            inst.instrument()
            client = together.Together(api_key="tog-test-fake-key")
            transport = client._client._transport
            assert isinstance(transport, CheckrdTransport)
        finally:
            inst.uninstrument()
