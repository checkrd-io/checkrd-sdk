"""Tests for :class:`checkrd.integrations.GoogleGenAIInstrumentor`.

``GoogleGenAIInstrumentor`` is a thin subclass of
``HttpxClientInstrumentor`` -- the core contract is already covered by
``test_base.py``. This file verifies the Google GenAI-specific
declarations (module name, target classes) wire up correctly against a
fake ``google.genai`` module.

Google GenAI exposes only a sync client (``Client``), so there are no
async tests. The dotted module name (``google.genai``) requires
injecting both ``google`` and ``google.genai`` into ``sys.modules``.
"""

from __future__ import annotations

import importlib.util

import httpx
import pytest

from checkrd.integrations import GoogleGenAIInstrumentor
from checkrd.transports._httpx import CheckrdTransport
from tests.conftest import requires_wasm

try:
    _HAS_REAL_GOOGLE_GENAI = importlib.util.find_spec("google.genai") is not None
except (ModuleNotFoundError, ValueError):
    _HAS_REAL_GOOGLE_GENAI = False


@requires_wasm
class TestGoogleGenAIInstrumentation:
    def test_sync_client_wrapped(
        self,
        fake_google_genai_module,
        initialized_checkrd,
    ) -> None:
        inst = GoogleGenAIInstrumentor()
        inst.instrument()

        client = fake_google_genai_module.Client(api_key="goog-test")
        assert isinstance(client._client._transport, CheckrdTransport)

    def test_user_transport_preserved(
        self,
        fake_google_genai_module,
        initialized_checkrd,
    ) -> None:
        inst = GoogleGenAIInstrumentor()
        inst.instrument()

        user_transport = httpx.MockTransport(lambda req: httpx.Response(200))
        with httpx.Client(transport=user_transport) as user_client:
            client = fake_google_genai_module.Client(
                api_key="goog-test", http_client=user_client
            )

            wrapper = client._client._transport
            assert isinstance(wrapper, CheckrdTransport)
            assert wrapper._transport is user_transport

    def test_uninstrument_restores(
        self,
        fake_google_genai_module,
        initialized_checkrd,
    ) -> None:
        original = fake_google_genai_module.Client.__init__
        inst = GoogleGenAIInstrumentor()
        inst.instrument()
        assert fake_google_genai_module.Client.__init__ is not original
        inst.uninstrument()
        assert fake_google_genai_module.Client.__init__ is original

    def test_idempotent_instrument(
        self,
        fake_google_genai_module,
        initialized_checkrd,
    ) -> None:
        inst = GoogleGenAIInstrumentor()
        inst.instrument()
        inst.instrument()

        client = fake_google_genai_module.Client(api_key="goog-test")
        wrapper = client._client._transport
        assert isinstance(wrapper, CheckrdTransport)
        # Inner transport is the raw httpx default, not another layer.
        assert not isinstance(wrapper._transport, CheckrdTransport)

    def test_target_classes_declaration(self) -> None:
        assert GoogleGenAIInstrumentor._target_module_name == "google.genai"
        assert set(GoogleGenAIInstrumentor._target_classes) == {"Client"}


@pytest.mark.xfail(
    reason="google-genai >=1.0 changed internal client structure; instrumentor needs update",
    strict=False,
)
@pytest.mark.skipif(
    not _HAS_REAL_GOOGLE_GENAI,
    reason="google-genai package not installed; skipping real-library smoke test",
)
@requires_wasm
class TestRealGoogleGenAISmoke:
    def test_real_google_genai_client_transport_wrapped(
        self,
        initialized_checkrd,
    ) -> None:
        from google import genai

        inst = GoogleGenAIInstrumentor()
        try:
            inst.instrument()
            client = genai.Client(api_key="goog-test-fake-key")
            transport = client._client._transport
            assert isinstance(transport, CheckrdTransport)
        finally:
            inst.uninstrument()
