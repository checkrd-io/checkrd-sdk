"""Tests for :class:`checkrd.integrations.AnthropicInstrumentor`.

``AnthropicInstrumentor`` is a near-identical twin of
``OpenAIInstrumentor`` — both are thin subclasses of
``HttpxClientInstrumentor`` — so the core contract is already
covered by ``test_base.py`` and ``test_openai.py``. This file verifies
the Anthropic-specific declarations (module name, target classes) wire
up correctly against a fake ``anthropic`` module, without duplicating
every assertion.
"""

from __future__ import annotations

import importlib.util

import httpx
import pytest

from checkrd.integrations import AnthropicInstrumentor
from checkrd.transports._httpx import CheckrdAsyncTransport, CheckrdTransport
from tests.conftest import requires_wasm

_HAS_REAL_ANTHROPIC = importlib.util.find_spec("anthropic") is not None


@requires_wasm
class TestAnthropicInstrumentation:
    def test_sync_client_wrapped(
        self,
        fake_anthropic_module,
        initialized_checkrd,
    ) -> None:
        inst = AnthropicInstrumentor()
        inst.instrument()

        client = fake_anthropic_module.Anthropic(api_key="sk-ant-test")
        assert isinstance(client._client._transport, CheckrdTransport)

    def test_async_client_wrapped_with_async_transport(
        self,
        fake_anthropic_module,
        initialized_checkrd,
    ) -> None:
        inst = AnthropicInstrumentor()
        inst.instrument()

        client = fake_anthropic_module.AsyncAnthropic(api_key="sk-ant-test")
        assert isinstance(
            client._client._transport, CheckrdAsyncTransport
        )

    def test_user_http_client_preserved(
        self,
        fake_anthropic_module,
        initialized_checkrd,
    ) -> None:
        inst = AnthropicInstrumentor()
        inst.instrument()

        user_transport = httpx.MockTransport(lambda req: httpx.Response(200))
        with httpx.Client(transport=user_transport) as user_client:
            client = fake_anthropic_module.Anthropic(
                api_key="sk-ant-test", http_client=user_client
            )

            wrapper = client._client._transport
            assert isinstance(wrapper, CheckrdTransport)
            assert wrapper._transport is user_transport

    def test_uninstrument_restores(
        self,
        fake_anthropic_module,
        initialized_checkrd,
    ) -> None:
        original = fake_anthropic_module.Anthropic.__init__
        inst = AnthropicInstrumentor()
        inst.instrument()
        assert fake_anthropic_module.Anthropic.__init__ is not original
        inst.uninstrument()
        assert fake_anthropic_module.Anthropic.__init__ is original

    def test_idempotent_instrument(
        self,
        fake_anthropic_module,
        initialized_checkrd,
    ) -> None:
        inst = AnthropicInstrumentor()
        inst.instrument()
        inst.instrument()

        client = fake_anthropic_module.Anthropic(api_key="sk-ant-test")
        wrapper = client._client._transport
        assert isinstance(wrapper, CheckrdTransport)
        # Inner transport is the raw httpx default, not another layer.
        assert not isinstance(wrapper._transport, CheckrdTransport)

    def test_target_classes_declaration(self) -> None:
        # Contract test: the instrumentor must declare both sync and
        # async top-level classes. If this changes in the future, both
        # the subclass declaration and the test are updated together.
        assert AnthropicInstrumentor._target_module_name == "anthropic"
        assert set(AnthropicInstrumentor._target_classes) == {
            "Anthropic",
            "AsyncAnthropic",
        }


@pytest.mark.skipif(
    not _HAS_REAL_ANTHROPIC,
    reason="anthropic package not installed; skipping real-library smoke test",
)
@requires_wasm
class TestRealAnthropicSmoke:
    def test_real_anthropic_client_transport_wrapped(
        self,
        initialized_checkrd,
    ) -> None:
        import anthropic

        inst = AnthropicInstrumentor()
        try:
            inst.instrument()
            client = anthropic.Anthropic(api_key="sk-ant-test-fake-key")
            transport = client._client._transport
            assert isinstance(transport, CheckrdTransport)
        finally:
            inst.uninstrument()
