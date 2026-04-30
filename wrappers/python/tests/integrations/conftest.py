"""Shared fixtures for checkrd.integrations tests.

Provides ``fake_openai_module`` and ``fake_anthropic_module`` fixtures
that inject minimal stand-ins for the real SDKs into ``sys.modules``.
The fakes mirror the attribute shape our instrumentors target —
specifically, a class with an ``__init__`` that stores an
``httpx.Client`` / ``httpx.AsyncClient`` on ``self._client``. The
fakes also honor a ``http_client=`` kwarg the way the real SDKs do,
so we can test that user-supplied clients are still wrapped.

Using fakes keeps the tests:

- Fast (no real HTTP, no real dependencies to install).
- Hermetic (no network, no version drift, no mocks of Anthropic's
  or OpenAI's internal modules).
- Scoped (each fixture cleans up ``sys.modules`` on teardown, so
  parallel tests don't collide).
"""

from __future__ import annotations

import sys
import types
from typing import Any, Iterator, Optional

import httpx
import pytest

import checkrd


# --------------------------------------------------------------------
# Fake SDK module factories
# --------------------------------------------------------------------


def _make_openai_module() -> types.ModuleType:
    """Build a minimal stand-in for ``openai`` that exposes the classes
    our :class:`OpenAIInstrumentor` patches."""
    module = types.ModuleType("openai")

    class OpenAI:
        """Fake ``openai.OpenAI`` mirroring the real SDK's client shape."""

        def __init__(
            self,
            *,
            api_key: Optional[str] = None,
            http_client: Optional[httpx.Client] = None,
            **_kwargs: Any,
        ) -> None:
            self.api_key = api_key
            # The real SDK builds a default ``httpx.Client`` when one is
            # not supplied. Our fake mimics that so the instrumentor's
            # "wrap the existing transport" path is exercised.
            self._client: httpx.Client = http_client or httpx.Client()

    class AsyncOpenAI:
        """Fake ``openai.AsyncOpenAI`` mirroring the real SDK."""

        def __init__(
            self,
            *,
            api_key: Optional[str] = None,
            http_client: Optional[httpx.AsyncClient] = None,
            **_kwargs: Any,
        ) -> None:
            self.api_key = api_key
            self._client: httpx.AsyncClient = http_client or httpx.AsyncClient()

    class AzureOpenAI(OpenAI):
        """Fake Azure subclass that relies on super().__init__ for wiring.

        Crucially, this class does NOT override ``__init__`` beyond
        calling ``super().__init__()``. That's how the real
        ``AzureOpenAI`` is built, which means our patch of ``OpenAI``
        covers it automatically without needing to list it in
        ``_target_classes``.
        """

        pass

    module.OpenAI = OpenAI  # type: ignore[attr-defined]
    module.AsyncOpenAI = AsyncOpenAI  # type: ignore[attr-defined]
    module.AzureOpenAI = AzureOpenAI  # type: ignore[attr-defined]
    return module


def _make_anthropic_module() -> types.ModuleType:
    """Build a minimal stand-in for ``anthropic``."""
    module = types.ModuleType("anthropic")

    class Anthropic:
        def __init__(
            self,
            *,
            api_key: Optional[str] = None,
            http_client: Optional[httpx.Client] = None,
            **_kwargs: Any,
        ) -> None:
            self.api_key = api_key
            self._client: httpx.Client = http_client or httpx.Client()

    class AsyncAnthropic:
        def __init__(
            self,
            *,
            api_key: Optional[str] = None,
            http_client: Optional[httpx.AsyncClient] = None,
            **_kwargs: Any,
        ) -> None:
            self.api_key = api_key
            self._client: httpx.AsyncClient = http_client or httpx.AsyncClient()

    module.Anthropic = Anthropic  # type: ignore[attr-defined]
    module.AsyncAnthropic = AsyncAnthropic  # type: ignore[attr-defined]
    return module


def _make_cohere_module() -> types.ModuleType:
    """Build a minimal stand-in for ``cohere``."""
    module = types.ModuleType("cohere")

    class ClientV2:
        def __init__(
            self,
            *,
            api_key: Optional[str] = None,
            http_client: Optional[httpx.Client] = None,
            **_kwargs: Any,
        ) -> None:
            self.api_key = api_key
            self._client: httpx.Client = http_client or httpx.Client()

    class AsyncClientV2:
        def __init__(
            self,
            *,
            api_key: Optional[str] = None,
            http_client: Optional[httpx.AsyncClient] = None,
            **_kwargs: Any,
        ) -> None:
            self.api_key = api_key
            self._client: httpx.AsyncClient = http_client or httpx.AsyncClient()

    module.ClientV2 = ClientV2  # type: ignore[attr-defined]
    module.AsyncClientV2 = AsyncClientV2  # type: ignore[attr-defined]
    return module


def _make_mistralai_module() -> types.ModuleType:
    """Build a minimal stand-in for ``mistralai``."""
    module = types.ModuleType("mistralai")

    class Mistral:
        def __init__(
            self,
            *,
            api_key: Optional[str] = None,
            http_client: Optional[httpx.Client] = None,
            **_kwargs: Any,
        ) -> None:
            self.api_key = api_key
            self._client: httpx.Client = http_client or httpx.Client()

    module.Mistral = Mistral  # type: ignore[attr-defined]
    return module


def _make_groq_module() -> types.ModuleType:
    """Build a minimal stand-in for ``groq``."""
    module = types.ModuleType("groq")

    class Groq:
        def __init__(
            self,
            *,
            api_key: Optional[str] = None,
            http_client: Optional[httpx.Client] = None,
            **_kwargs: Any,
        ) -> None:
            self.api_key = api_key
            self._client: httpx.Client = http_client or httpx.Client()

    class AsyncGroq:
        def __init__(
            self,
            *,
            api_key: Optional[str] = None,
            http_client: Optional[httpx.AsyncClient] = None,
            **_kwargs: Any,
        ) -> None:
            self.api_key = api_key
            self._client: httpx.AsyncClient = http_client or httpx.AsyncClient()

    module.Groq = Groq  # type: ignore[attr-defined]
    module.AsyncGroq = AsyncGroq  # type: ignore[attr-defined]
    return module


def _make_together_module() -> types.ModuleType:
    """Build a minimal stand-in for ``together``."""
    module = types.ModuleType("together")

    class Together:
        def __init__(
            self,
            *,
            api_key: Optional[str] = None,
            http_client: Optional[httpx.Client] = None,
            **_kwargs: Any,
        ) -> None:
            self.api_key = api_key
            self._client: httpx.Client = http_client or httpx.Client()

    class AsyncTogether:
        def __init__(
            self,
            *,
            api_key: Optional[str] = None,
            http_client: Optional[httpx.AsyncClient] = None,
            **_kwargs: Any,
        ) -> None:
            self.api_key = api_key
            self._client: httpx.AsyncClient = http_client or httpx.AsyncClient()

    module.Together = Together  # type: ignore[attr-defined]
    module.AsyncTogether = AsyncTogether  # type: ignore[attr-defined]
    return module


def _make_google_genai_module() -> types.ModuleType:
    """Build a minimal stand-in for ``google.genai``.

    Returns the ``google.genai`` sub-module. The caller must also
    inject a parent ``google`` namespace package into ``sys.modules``
    so that ``import google.genai`` resolves correctly.
    """
    google_pkg = types.ModuleType("google")
    google_pkg.__path__ = []  # type: ignore[attr-defined]

    genai_module = types.ModuleType("google.genai")

    class Client:
        def __init__(
            self,
            *,
            api_key: Optional[str] = None,
            http_client: Optional[httpx.Client] = None,
            **_kwargs: Any,
        ) -> None:
            self.api_key = api_key
            self._client: httpx.Client = http_client or httpx.Client()

    genai_module.Client = Client  # type: ignore[attr-defined]

    # Wire the sub-module onto the parent so attribute access works.
    google_pkg.genai = genai_module  # type: ignore[attr-defined]

    # Stash the parent on the sub-module so the fixture can inject both.
    genai_module._google_pkg = google_pkg  # type: ignore[attr-defined]
    return genai_module


# --------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------


@pytest.fixture
def fake_openai_module() -> Iterator[types.ModuleType]:
    """Inject a fake ``openai`` module into sys.modules for one test.

    Removes the real module (if installed) for the duration of the
    test and restores it on teardown. Also unloads the checkrd
    integration module so it re-imports the fresh fake (Python caches
    importlib lookups per-process).
    """
    saved = sys.modules.get("openai")
    fake = _make_openai_module()
    sys.modules["openai"] = fake
    try:
        yield fake
    finally:
        if saved is not None:
            sys.modules["openai"] = saved
        else:
            sys.modules.pop("openai", None)


@pytest.fixture
def fake_anthropic_module() -> Iterator[types.ModuleType]:
    """Same as :func:`fake_openai_module` but for anthropic."""
    saved = sys.modules.get("anthropic")
    fake = _make_anthropic_module()
    sys.modules["anthropic"] = fake
    try:
        yield fake
    finally:
        if saved is not None:
            sys.modules["anthropic"] = saved
        else:
            sys.modules.pop("anthropic", None)


@pytest.fixture
def fake_cohere_module() -> Iterator[types.ModuleType]:
    """Inject a fake ``cohere`` module into sys.modules for one test."""
    saved = sys.modules.get("cohere")
    fake = _make_cohere_module()
    sys.modules["cohere"] = fake
    try:
        yield fake
    finally:
        if saved is not None:
            sys.modules["cohere"] = saved
        else:
            sys.modules.pop("cohere", None)


@pytest.fixture
def fake_mistralai_module() -> Iterator[types.ModuleType]:
    """Inject a fake ``mistralai`` module into sys.modules for one test."""
    saved = sys.modules.get("mistralai")
    fake = _make_mistralai_module()
    sys.modules["mistralai"] = fake
    try:
        yield fake
    finally:
        if saved is not None:
            sys.modules["mistralai"] = saved
        else:
            sys.modules.pop("mistralai", None)


@pytest.fixture
def fake_groq_module() -> Iterator[types.ModuleType]:
    """Inject a fake ``groq`` module into sys.modules for one test."""
    saved = sys.modules.get("groq")
    fake = _make_groq_module()
    sys.modules["groq"] = fake
    try:
        yield fake
    finally:
        if saved is not None:
            sys.modules["groq"] = saved
        else:
            sys.modules.pop("groq", None)


@pytest.fixture
def fake_together_module() -> Iterator[types.ModuleType]:
    """Inject a fake ``together`` module into sys.modules for one test."""
    saved = sys.modules.get("together")
    fake = _make_together_module()
    sys.modules["together"] = fake
    try:
        yield fake
    finally:
        if saved is not None:
            sys.modules["together"] = saved
        else:
            sys.modules.pop("together", None)


@pytest.fixture
def fake_google_genai_module() -> Iterator[types.ModuleType]:
    """Inject fake ``google`` and ``google.genai`` modules into sys.modules.

    The dotted module name requires both entries in ``sys.modules``.
    Both are cleaned up on teardown.
    """
    saved_google = sys.modules.get("google")
    saved_genai = sys.modules.get("google.genai")
    fake = _make_google_genai_module()
    google_pkg = fake._google_pkg  # type: ignore[attr-defined]
    sys.modules["google"] = google_pkg
    sys.modules["google.genai"] = fake
    try:
        yield fake
    finally:
        if saved_google is not None:
            sys.modules["google"] = saved_google
        else:
            sys.modules.pop("google", None)
        if saved_genai is not None:
            sys.modules["google.genai"] = saved_genai
        else:
            sys.modules.pop("google.genai", None)


@pytest.fixture
def initialized_checkrd(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Iterator[None]:
    """Initialize the global Checkrd context for instrumentation tests.

    Isolates ``CHECKRD_CONFIG_DIR`` into a tmp_path so auto-generated
    identity keys don't touch ``~/.checkrd``. Calls
    :func:`checkrd.shutdown` on teardown so neighbor tests see a clean
    global state.
    """
    monkeypatch.setenv("CHECKRD_CONFIG_DIR", str(tmp_path))
    for var in (
        "CHECKRD_API_KEY",
        "CHECKRD_BASE_URL",
        "CHECKRD_AGENT_ID",
        "CHECKRD_ENFORCE",
        "CHECKRD_DISABLED",
    ):
        monkeypatch.delenv(var, raising=False)

    checkrd.shutdown()  # defensive — clean any leakage from a prior test
    checkrd.init(agent_id="test-agent")
    try:
        yield
    finally:
        # Ensure any instrumentors patched during the test are reverted
        # before we shut down; otherwise a leaked patch on a fake module
        # keeps references to the (now-stale) global context.
        checkrd.uninstrument()
        checkrd.shutdown()


@pytest.fixture
def uninitialized_checkrd() -> Iterator[None]:
    """Yield with the global Checkrd context guaranteed unset.

    Used by tests that verify the "call instrument() before init()"
    error path.
    """
    checkrd.shutdown()
    try:
        yield
    finally:
        checkrd.uninstrument()
        checkrd.shutdown()
