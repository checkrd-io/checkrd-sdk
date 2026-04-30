"""Tests for :class:`checkrd.integrations.CohereInstrumentor`.

``CohereInstrumentor`` is a thin subclass of
``HttpxClientInstrumentor`` -- the core contract is already covered by
``test_base.py``. This file verifies the Cohere-specific declarations
(module name, target classes) wire up correctly against a fake
``cohere`` module.
"""

from __future__ import annotations

import importlib.util

import httpx
import pytest

from checkrd.integrations import CohereInstrumentor
from checkrd.transports._httpx import CheckrdAsyncTransport, CheckrdTransport
from tests.conftest import requires_wasm

_HAS_REAL_COHERE = importlib.util.find_spec("cohere") is not None


@requires_wasm
class TestCohereInstrumentation:
    def test_sync_client_wrapped(
        self,
        fake_cohere_module,
        initialized_checkrd,
    ) -> None:
        inst = CohereInstrumentor()
        inst.instrument()

        client = fake_cohere_module.ClientV2(api_key="co-test")
        assert isinstance(client._client._transport, CheckrdTransport)

    def test_async_client_wrapped(
        self,
        fake_cohere_module,
        initialized_checkrd,
    ) -> None:
        inst = CohereInstrumentor()
        inst.instrument()

        client = fake_cohere_module.AsyncClientV2(api_key="co-test")
        assert isinstance(client._client._transport, CheckrdAsyncTransport)

    def test_user_transport_preserved(
        self,
        fake_cohere_module,
        initialized_checkrd,
    ) -> None:
        inst = CohereInstrumentor()
        inst.instrument()

        user_transport = httpx.MockTransport(lambda req: httpx.Response(200))
        with httpx.Client(transport=user_transport) as user_client:
            client = fake_cohere_module.ClientV2(
                api_key="co-test", http_client=user_client
            )

            wrapper = client._client._transport
            assert isinstance(wrapper, CheckrdTransport)
            assert wrapper._transport is user_transport

    def test_uninstrument_restores(
        self,
        fake_cohere_module,
        initialized_checkrd,
    ) -> None:
        original = fake_cohere_module.ClientV2.__init__
        inst = CohereInstrumentor()
        inst.instrument()
        assert fake_cohere_module.ClientV2.__init__ is not original
        inst.uninstrument()
        assert fake_cohere_module.ClientV2.__init__ is original

    def test_idempotent_instrument(
        self,
        fake_cohere_module,
        initialized_checkrd,
    ) -> None:
        inst = CohereInstrumentor()
        inst.instrument()
        inst.instrument()

        client = fake_cohere_module.ClientV2(api_key="co-test")
        wrapper = client._client._transport
        assert isinstance(wrapper, CheckrdTransport)
        # Inner transport is the raw httpx default, not another layer.
        assert not isinstance(wrapper._transport, CheckrdTransport)

    def test_target_classes_declaration(self) -> None:
        assert CohereInstrumentor._target_module_name == "cohere"
        assert set(CohereInstrumentor._target_classes) == {
            "ClientV2",
            "AsyncClientV2",
        }


@pytest.mark.xfail(
    reason="cohere >=6.0 renamed internal _client attribute; instrumentor needs update",
    strict=False,
)
@pytest.mark.skipif(
    not _HAS_REAL_COHERE,
    reason="cohere package not installed; skipping real-library smoke test",
)
@requires_wasm
class TestRealCohereSmoke:
    def test_real_cohere_client_transport_wrapped(
        self,
        initialized_checkrd,
    ) -> None:
        import cohere

        inst = CohereInstrumentor()
        try:
            inst.instrument()
            client = cohere.ClientV2(api_key="co-test-fake-key")
            transport = client._client._transport
            assert isinstance(transport, CheckrdTransport)
        finally:
            inst.uninstrument()
