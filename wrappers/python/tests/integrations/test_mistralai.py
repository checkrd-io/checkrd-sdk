"""Tests for :class:`checkrd.integrations.MistralInstrumentor`.

``MistralInstrumentor`` is a thin subclass of
``HttpxClientInstrumentor`` -- the core contract is already covered by
``test_base.py``. This file verifies the Mistral-specific declarations
(module name, target classes) wire up correctly against a fake
``mistralai`` module.

Mistral exposes only a sync client (``Mistral``), so there are no
async tests.
"""

from __future__ import annotations

import importlib.util

import httpx
import pytest

from checkrd.integrations import MistralInstrumentor
from checkrd.transports._httpx import CheckrdTransport
from tests.conftest import requires_wasm

_HAS_REAL_MISTRAL = importlib.util.find_spec("mistralai") is not None


@requires_wasm
class TestMistralInstrumentation:
    def test_sync_client_wrapped(
        self,
        fake_mistralai_module,
        initialized_checkrd,
    ) -> None:
        inst = MistralInstrumentor()
        inst.instrument()

        client = fake_mistralai_module.Mistral(api_key="mist-test")
        assert isinstance(client._client._transport, CheckrdTransport)

    def test_user_transport_preserved(
        self,
        fake_mistralai_module,
        initialized_checkrd,
    ) -> None:
        inst = MistralInstrumentor()
        inst.instrument()

        user_transport = httpx.MockTransport(lambda req: httpx.Response(200))
        with httpx.Client(transport=user_transport) as user_client:
            client = fake_mistralai_module.Mistral(
                api_key="mist-test", http_client=user_client
            )

            wrapper = client._client._transport
            assert isinstance(wrapper, CheckrdTransport)
            assert wrapper._transport is user_transport

    def test_uninstrument_restores(
        self,
        fake_mistralai_module,
        initialized_checkrd,
    ) -> None:
        original = fake_mistralai_module.Mistral.__init__
        inst = MistralInstrumentor()
        inst.instrument()
        assert fake_mistralai_module.Mistral.__init__ is not original
        inst.uninstrument()
        assert fake_mistralai_module.Mistral.__init__ is original

    def test_idempotent_instrument(
        self,
        fake_mistralai_module,
        initialized_checkrd,
    ) -> None:
        inst = MistralInstrumentor()
        inst.instrument()
        inst.instrument()

        client = fake_mistralai_module.Mistral(api_key="mist-test")
        wrapper = client._client._transport
        assert isinstance(wrapper, CheckrdTransport)
        # Inner transport is the raw httpx default, not another layer.
        assert not isinstance(wrapper._transport, CheckrdTransport)

    def test_target_classes_declaration(self) -> None:
        assert MistralInstrumentor._target_module_name == "mistralai"
        assert set(MistralInstrumentor._target_classes) == {"Mistral"}


@pytest.mark.xfail(
    reason="mistralai >=1.0 renamed internal _client attribute; instrumentor needs update",
    strict=False,
)
@pytest.mark.skipif(
    not _HAS_REAL_MISTRAL,
    reason="mistralai package not installed; skipping real-library smoke test",
)
@requires_wasm
class TestRealMistralSmoke:
    def test_real_mistral_client_transport_wrapped(
        self,
        initialized_checkrd,
    ) -> None:
        import mistralai

        inst = MistralInstrumentor()
        try:
            inst.instrument()
            client = mistralai.Mistral(api_key="mist-test-fake-key")
            transport = client._client._transport
            assert isinstance(transport, CheckrdTransport)
        finally:
            inst.uninstrument()
