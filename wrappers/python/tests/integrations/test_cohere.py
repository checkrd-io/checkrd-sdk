"""Tests for :class:`checkrd.integrations.CohereInstrumentor`.

The cohere SDK keeps its real httpx client three levels deep:
``client._client_wrapper.httpx_client.httpx_client``. The
instrumentor walks that path for all four top-level client classes
(sync + async, V1 + V2).
"""

from __future__ import annotations

import importlib.util

import httpx
import pytest

from checkrd.integrations import CohereInstrumentor
from checkrd.transports._httpx import CheckrdAsyncTransport, CheckrdTransport
from tests.conftest import requires_wasm

_HAS_REAL_COHERE = importlib.util.find_spec("cohere") is not None


def _leaf(client: object) -> object:
    """Return the leaf httpx client for any Cohere top-level client."""
    return client._client_wrapper.httpx_client.httpx_client  # type: ignore[attr-defined]


@requires_wasm
class TestCohereInstrumentation:
    def test_sync_client_v2_wrapped(
        self,
        fake_cohere_module,
        initialized_checkrd,
    ) -> None:
        inst = CohereInstrumentor()
        inst.instrument()

        client = fake_cohere_module.ClientV2(api_key="co-test")
        assert isinstance(_leaf(client)._transport, CheckrdTransport)

    def test_async_client_v2_wrapped(
        self,
        fake_cohere_module,
        initialized_checkrd,
    ) -> None:
        inst = CohereInstrumentor()
        inst.instrument()

        client = fake_cohere_module.AsyncClientV2(api_key="co-test")
        assert isinstance(_leaf(client)._transport, CheckrdAsyncTransport)

    def test_legacy_v1_client_wrapped(
        self,
        fake_cohere_module,
        initialized_checkrd,
    ) -> None:
        """cohere.Client (V1) shares the same nested layout as V2."""
        inst = CohereInstrumentor()
        inst.instrument()

        client = fake_cohere_module.Client(api_key="co-test")
        assert isinstance(_leaf(client)._transport, CheckrdTransport)

    def test_legacy_v1_async_client_wrapped(
        self,
        fake_cohere_module,
        initialized_checkrd,
    ) -> None:
        inst = CohereInstrumentor()
        inst.instrument()

        client = fake_cohere_module.AsyncClient(api_key="co-test")
        assert isinstance(_leaf(client)._transport, CheckrdAsyncTransport)

    def test_user_transport_preserved(
        self,
        fake_cohere_module,
        initialized_checkrd,
    ) -> None:
        inst = CohereInstrumentor()
        inst.instrument()

        user_transport = httpx.MockTransport(lambda req: httpx.Response(200))
        with httpx.Client(transport=user_transport) as user_client:
            client = fake_cohere_module.ClientV2(api_key="co-test", http_client=user_client)

            wrapper = _leaf(client)._transport
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
        wrapper = _leaf(client)._transport
        assert isinstance(wrapper, CheckrdTransport)
        # Inner transport is the raw httpx default, not another layer.
        assert not isinstance(wrapper._transport, CheckrdTransport)

    def test_target_classes_declaration(self) -> None:
        assert CohereInstrumentor._target_module_name == "cohere"
        assert set(CohereInstrumentor._target_classes) == {
            "Client",
            "AsyncClient",
            "ClientV2",
            "AsyncClientV2",
        }


@pytest.mark.skipif(
    not _HAS_REAL_COHERE,
    reason="cohere package not installed; skipping real-library smoke test",
)
@requires_wasm
class TestRealCohereSmoke:
    def test_real_cohere_client_v2_transport_wrapped(
        self,
        initialized_checkrd,
    ) -> None:
        import cohere

        inst = CohereInstrumentor()
        try:
            inst.instrument()
            client = cohere.ClientV2(api_key="co-test-fake-key")
            transport = client._client_wrapper.httpx_client.httpx_client._transport
            assert isinstance(transport, CheckrdTransport)
        finally:
            inst.uninstrument()

    def test_real_cohere_async_client_v2_transport_wrapped(
        self,
        initialized_checkrd,
    ) -> None:
        import cohere

        inst = CohereInstrumentor()
        try:
            inst.instrument()
            client = cohere.AsyncClientV2(api_key="co-test-fake-key")
            transport = client._client_wrapper.httpx_client.httpx_client._transport
            assert isinstance(transport, CheckrdAsyncTransport)
        finally:
            inst.uninstrument()
