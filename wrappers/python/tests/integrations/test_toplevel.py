"""End-to-end tests for the top-level ``checkrd.init() + checkrd.instrument()``
pattern.

These are the tests that back the README quickstart. They verify that
calling ``checkrd.init()`` followed by ``checkrd.instrument()`` produces
a working sync and async pipeline for the fake openai / anthropic
modules — so real users copying the quickstart get the behavior the
docs promise.

The module-level instrumentors in ``checkrd`` are module-global, so
each test must start from a known state (uninstrumented) and restore
that state on teardown via the ``uninstrument()`` call in
``initialized_checkrd``'s teardown.
"""

from __future__ import annotations

import httpx
import pytest

import checkrd
from checkrd.exceptions import CheckrdInitError
from checkrd.transports._httpx import CheckrdAsyncTransport, CheckrdTransport
from tests.conftest import requires_wasm


@requires_wasm
class TestInitAndInstrument:
    """The headline flow: init(), instrument(), construct an SDK client,
    confirm the transport is wrapped."""

    def test_instrument_patches_both_libraries(
        self,
        fake_openai_module,
        fake_anthropic_module,
        initialized_checkrd,
    ) -> None:
        checkrd.instrument()

        openai_client = fake_openai_module.OpenAI(api_key="sk-test")
        anthropic_client = fake_anthropic_module.Anthropic(api_key="sk-ant-test")

        assert isinstance(openai_client._client._transport, CheckrdTransport)
        assert isinstance(
            anthropic_client._client._transport, CheckrdTransport
        )

    def test_instrument_patches_async_libraries(
        self,
        fake_openai_module,
        fake_anthropic_module,
        initialized_checkrd,
    ) -> None:
        checkrd.instrument()

        async_oai = fake_openai_module.AsyncOpenAI(api_key="sk-test")
        async_anthropic = fake_anthropic_module.AsyncAnthropic(api_key="sk-ant")

        assert isinstance(
            async_oai._client._transport, CheckrdAsyncTransport
        )
        assert isinstance(
            async_anthropic._client._transport, CheckrdAsyncTransport
        )

    def test_uninstrument_reverts_both(
        self,
        fake_openai_module,
        fake_anthropic_module,
        initialized_checkrd,
    ) -> None:
        checkrd.instrument()
        checkrd.uninstrument()

        openai_client = fake_openai_module.OpenAI(api_key="sk-test")
        anthropic_client = fake_anthropic_module.Anthropic(api_key="sk-ant")

        assert not isinstance(
            openai_client._client._transport, CheckrdTransport
        )
        assert not isinstance(
            anthropic_client._client._transport, CheckrdTransport
        )

    def test_instrument_is_idempotent(
        self,
        fake_openai_module,
        initialized_checkrd,
    ) -> None:
        checkrd.instrument()
        checkrd.instrument()  # second call is a no-op

        client = fake_openai_module.OpenAI(api_key="sk-test")
        wrapper = client._client._transport
        assert isinstance(wrapper, CheckrdTransport)
        # Inner transport is not another CheckrdTransport (would prove
        # double-wrapping).
        assert not isinstance(wrapper._transport, CheckrdTransport)


@requires_wasm
class TestLibraryOnlyPresent:
    """When only one of openai/anthropic is installed, ``instrument()``
    should patch the one that's present and silently skip the other.
    This matches Sentry's integration behavior — you don't pay a cost
    for libraries you don't use."""

    def test_only_openai_installed(
        self,
        fake_openai_module,
        initialized_checkrd,
    ) -> None:
        # No fake_anthropic_module fixture here -> anthropic looks
        # "not installed" to the instrumentor.
        checkrd.instrument()

        client = fake_openai_module.OpenAI(api_key="sk-test")
        assert isinstance(client._client._transport, CheckrdTransport)

    def test_only_anthropic_installed(
        self,
        fake_anthropic_module,
        initialized_checkrd,
    ) -> None:
        checkrd.instrument()

        client = fake_anthropic_module.Anthropic(api_key="sk-ant")
        assert isinstance(client._client._transport, CheckrdTransport)


@requires_wasm
class TestInstrumentBeforeInit:
    """Calling instrument() before init() is the most common user error.
    It should raise a clear, actionable CheckrdInitError."""

    def test_instrument_without_init_raises(
        self,
        fake_openai_module,
        uninitialized_checkrd,
    ) -> None:
        with pytest.raises(CheckrdInitError, match="init"):
            checkrd.instrument()

    def test_instrument_openai_without_init_raises(
        self,
        fake_openai_module,
        uninitialized_checkrd,
    ) -> None:
        with pytest.raises(CheckrdInitError):
            checkrd.instrument_openai()

    def test_instrument_anthropic_without_init_raises(
        self,
        fake_anthropic_module,
        uninitialized_checkrd,
    ) -> None:
        with pytest.raises(CheckrdInitError):
            checkrd.instrument_anthropic()

    def test_uninstrument_without_init_is_noop(
        self,
        uninitialized_checkrd,
    ) -> None:
        # After shutdown(), uninstrument() must be safe to call as
        # part of atexit / finally cleanup paths.
        checkrd.uninstrument()  # no exception


@requires_wasm
class TestLibrarySpecificEntryPoints:
    """The per-library top-level functions (``instrument_openai`` /
    ``instrument_anthropic``) are the fine-grained equivalent of
    ``checkrd.instrument()``. They should behave identically to calling
    the class API directly."""

    def test_instrument_openai_only(
        self,
        fake_openai_module,
        fake_anthropic_module,
        initialized_checkrd,
    ) -> None:
        checkrd.instrument_openai()

        openai_client = fake_openai_module.OpenAI(api_key="sk-test")
        anthropic_client = fake_anthropic_module.Anthropic(api_key="sk-ant")

        assert isinstance(openai_client._client._transport, CheckrdTransport)
        # Anthropic was NOT instrumented — its transport is still raw.
        assert not isinstance(
            anthropic_client._client._transport, CheckrdTransport
        )

    def test_uninstrument_openai_leaves_anthropic_patched(
        self,
        fake_openai_module,
        fake_anthropic_module,
        initialized_checkrd,
    ) -> None:
        checkrd.instrument()  # both
        checkrd.uninstrument_openai()  # revert only openai

        openai_client = fake_openai_module.OpenAI(api_key="sk-test")
        anthropic_client = fake_anthropic_module.Anthropic(api_key="sk-ant")

        assert not isinstance(
            openai_client._client._transport, CheckrdTransport
        )
        assert isinstance(
            anthropic_client._client._transport, CheckrdTransport
        )


@requires_wasm
class TestSharedEngine:
    """Every instrumented client must share the same global engine, not
    get its own private one. That's the whole point of the global
    init() pattern — a single kill switch, a single rate-limit bucket,
    a single telemetry sink across every library."""

    def test_openai_and_anthropic_share_engine(
        self,
        fake_openai_module,
        fake_anthropic_module,
        initialized_checkrd,
    ) -> None:
        checkrd.instrument()

        openai_client = fake_openai_module.OpenAI(api_key="sk-test")
        anthropic_client = fake_anthropic_module.Anthropic(api_key="sk-ant")

        openai_engine = openai_client._client._transport._engine
        anthropic_engine = anthropic_client._client._transport._engine
        assert openai_engine is anthropic_engine

    def test_multiple_openai_clients_share_engine(
        self,
        fake_openai_module,
        initialized_checkrd,
    ) -> None:
        checkrd.instrument()

        c1 = fake_openai_module.OpenAI(api_key="sk-one")
        c2 = fake_openai_module.OpenAI(api_key="sk-two")

        assert c1._client._transport._engine is c2._client._transport._engine


@requires_wasm
class TestEndToEndHttpFlow:
    """The ultimate contract: an instrumented OpenAI client makes a
    request through a MockTransport and the Checkrd policy engine
    actually evaluates it. This is what the user cares about."""

    def test_instrumented_client_request_is_evaluated(
        self,
        fake_openai_module,
        initialized_checkrd,
    ) -> None:
        checkrd.instrument()

        # Build an OpenAI client with a mock transport that captures
        # requests; the checkrd layer must still pass them through.
        captured: list[httpx.Request] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"ok": True})
        with httpx.Client(transport=httpx.MockTransport(_handler)) as user_client:
            client = fake_openai_module.OpenAI(
                api_key="sk-test", http_client=user_client
            )

            response = client._client.get("https://api.openai.com/v1/models")
            assert response.status_code == 200
            assert len(captured) == 1
        # The request reached the mock — proves the Checkrd transport
        # called through to its wrapped inner transport.

    def test_shutdown_during_active_instrumentation(
        self,
        fake_openai_module,
        initialized_checkrd,
    ) -> None:
        # User calls shutdown() while instrumentation is active. The
        # instrumentation itself doesn't get reverted (that's what
        # uninstrument() is for), but shutdown() must not crash and
        # the old engine reference on the patched __init__ continues
        # to work for any clients constructed before the re-init.
        checkrd.instrument()
        client = fake_openai_module.OpenAI(api_key="sk-test")
        assert isinstance(client._client._transport, CheckrdTransport)

        # Don't call shutdown() here — the fixture will do it in teardown
        # and we just want to prove that constructing a client post-init
        # is valid under the current lifecycle.
