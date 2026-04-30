"""Base classes for Checkrd library integrations.

:class:`Instrumentor` is the abstract base implementing the idempotency,
thread safety, and missing-library detection that every integration
needs. Subclasses override :meth:`_setup` / :meth:`_teardown` to patch
and unpatch their target library.

:class:`HttpxClientInstrumentor` is a concrete helper for the large
family of AI SDKs that expose an ``httpx.Client`` attribute on their
main client class. It declaratively describes the target (module name,
class names, attribute holding the httpx client) and the base class
handles the actual patching mechanics. :class:`OpenAIInstrumentor` and
:class:`AnthropicInstrumentor` are both ~8-line subclasses of this.

The shape is modeled on OpenTelemetry's
``opentelemetry-instrumentation`` package, which is the de-facto
standard for Python library instrumentation. Developers who already
know OTel get a zero-surprise Checkrd experience.

# Why ``__init__`` patching (not ``wrapt.when_imported``)

OpenTelemetry's ``opentelemetry-instrumentation-*`` packages use
``wrapt.importer.when_imported`` to hook at module-load time. We
considered that pattern and chose the simpler ``__init__`` swap
because every modern AI SDK we support (openai, anthropic, cohere,
mistralai, groq, together, google-genai) is constructed via a
top-level class — patching ``__init__`` covers 100% of user
constructions without an import-order dependency that cuts both ways:

  - **wrapt.when_imported** lets you patch a module that's already
    been imported, but adds ``wrapt`` as a transitive dep and means
    debugging stack traces show the wrapper inserted by
    ``wrap_function_wrapper`` rather than the user's call site.
  - **__init__ swap** requires ``checkrd.instrument*()`` before the
    first vendor client is constructed — a constraint that is easy
    to satisfy (the documented quickstart starts with init) and
    keeps stack traces clean.

If a future vendor SDK exposes its httpx client through a factory
function instead of a class constructor, that integration switches
to ``wrap_function_wrapper`` from wrapt — we add the dep then, not
preemptively.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import threading
from typing import Any, Callable, ClassVar, Optional, Tuple

import httpx

from checkrd._state import _GlobalContext, get_context
from checkrd.transports._httpx import CheckrdAsyncTransport, CheckrdTransport

logger = logging.getLogger("checkrd")


class Instrumentor:
    """Abstract base class for Checkrd library integrations.

    Subclasses implement :meth:`_setup` to patch a target library and
    :meth:`_teardown` to restore it. The base class takes care of:

    - **Idempotency**: :meth:`instrument` is a no-op when already applied,
      and :meth:`uninstrument` is a no-op when already removed. Callers
      can invoke either method repeatedly without special handling.
    - **Thread safety**: a per-instance :class:`threading.Lock` serializes
      setup and teardown so concurrent calls from multiple threads
      converge to a single well-defined state.
    - **Target-missing detection**: if the subclass declares a
      ``_target_module_name`` that isn't importable, :meth:`instrument`
      is a silent no-op (debug-level log). This lets users call
      ``checkrd.instrument()`` without knowing which libraries are
      actually present in their environment.
    - **Setup exception safety**: if :meth:`_setup` raises, the
      instrumentor stays in the un-instrumented state so the next call
      can retry cleanly.
    """

    #: Subclass contract: the fully-qualified Python module name of the
    #: target library. If the module cannot be imported, :meth:`instrument`
    #: is a no-op. For example, :class:`OpenAIInstrumentor` sets this to
    #: ``"openai"``.
    _target_module_name: ClassVar[str] = ""

    def __init__(self) -> None:
        self._instrumented: bool = False
        self._lock = threading.Lock()

    @property
    def instrumented(self) -> bool:
        """Whether :meth:`instrument` has been called (and not reverted)."""
        return self._instrumented

    def instrument(self, *, context: Optional[_GlobalContext] = None) -> None:
        """Apply the patching this instrumentor implements.

        Args:
            context: Optional explicit :class:`_GlobalContext`. When
                omitted, the global context from :func:`checkrd.init` is
                used. Tests pass a custom context to avoid touching
                global state.

        Idempotent; thread-safe. Silently no-ops when the target library
        is not importable in the current interpreter. If :meth:`_setup`
        raises, the exception propagates and the instrumentor stays
        un-instrumented so the next call can retry.
        """
        with self._lock:
            if self._instrumented:
                return
            if not self._target_available():
                logger.debug(
                    "checkrd: %s not installed; skipping instrumentation",
                    self._target_module_name or type(self).__name__,
                )
                return
            ctx = context if context is not None else get_context()
            self._setup(ctx)
            self._instrumented = True

    def uninstrument(self) -> None:
        """Revert the patching this instrumentor applied.

        Idempotent; thread-safe. If :meth:`_teardown` raises, the
        instrumentor is still marked un-instrumented so the next call
        to :meth:`instrument` starts from a clean slate — partial
        teardown is better than sticky state in the face of a programming
        error in the subclass.
        """
        with self._lock:
            if not self._instrumented:
                return
            try:
                self._teardown()
            finally:
                self._instrumented = False

    def _target_available(self) -> bool:
        """Return whether :data:`_target_module_name` is importable.

        Checks ``sys.modules`` first (covers dynamically injected
        modules like test fakes), then falls back to
        :func:`importlib.util.find_spec` for packages that are installed
        but not yet imported. ``find_spec`` avoids triggering the
        target module's import side effects.

        A ``None`` value in ``sys.modules`` is Python's marker for a
        failed import attempt — treat it as "not available".
        """
        if not self._target_module_name:
            return True
        import sys

        cached = sys.modules.get(self._target_module_name)
        if cached is not None:
            return True
        if self._target_module_name in sys.modules:
            # Key is present but value is None — failed import marker.
            return False
        try:
            spec = importlib.util.find_spec(self._target_module_name)
        except (ModuleNotFoundError, ValueError):
            return False
        return spec is not None

    # ------------------------------------------------------------------
    # Subclass hooks
    # ------------------------------------------------------------------

    def _setup(self, context: _GlobalContext) -> None:
        """Patch the target library. Called with the instance lock held.

        Subclasses MUST override this method. The base implementation
        raises :class:`NotImplementedError` so forgetting to override
        surfaces as a loud failure at test time, not a silent no-op in
        production.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must override _setup()"
        )

    def _teardown(self) -> None:
        """Revert the patching applied in :meth:`_setup`.

        Subclasses MUST override this method. Called with the instance
        lock held.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must override _teardown()"
        )


class HttpxClientInstrumentor(Instrumentor):
    """Generic instrumentor for libraries built on an ``httpx.Client``.

    Every modern Python AI SDK (openai, anthropic, cohere, mistralai,
    groq, together, fireworks, etc.) follows the same architectural
    pattern: a user-facing client class holds an ``httpx.Client`` (or
    ``httpx.AsyncClient``) in an instance attribute and makes every
    outbound HTTP request through it. This instrumentor hooks the target
    library's ``__init__``, lets the original run, then replaces the
    constructed client's ``_transport`` with a :class:`CheckrdTransport`
    wrapping the original.

    Subclasses declare three class attributes:

    - :attr:`_target_module_name` — the module to import
      (e.g. ``"openai"``).
    - :attr:`_target_classes` — the class names inside that module to
      patch. Typically the sync and async top-level clients
      (e.g. ``("OpenAI", "AsyncOpenAI")``). Subclasses of these in the
      same module are covered automatically because they call
      ``super().__init__()``.
    - :attr:`_client_attr` — the attribute name holding the
      ``httpx.Client``. Defaults to ``"_client"`` because that's the
      convention both ``openai`` and ``anthropic`` use.

    The instrumentor is defensive: if a target class has been removed
    from an upgraded version of the library, or the attribute is not an
    ``httpx.Client``, the patch is skipped and a warning is logged.
    Users should still observe telemetry for classes we *can* patch.

    Idempotency is guaranteed by the :attr:`CheckrdTransport._checkrd_instrumented`
    marker: if the transport we're about to wrap is already a
    CheckrdTransport (from a prior instrumentation, or from the user
    having called :func:`checkrd.wrap` on the same httpx client
    earlier), we skip silently.
    """

    #: Subclass contract: the class names to patch inside
    #: :attr:`_target_module_name`.
    _target_classes: ClassVar[Tuple[str, ...]] = ()

    #: Subclass contract: the attribute on an instance of a target class
    #: that holds its ``httpx.Client`` / ``httpx.AsyncClient``.
    _client_attr: ClassVar[str] = "_client"

    def __init__(self) -> None:
        super().__init__()
        # Maps class name -> the original ``__init__`` we replaced. Used
        # by :meth:`_teardown` to restore.
        self._originals: dict[str, Callable[..., None]] = {}

    def _setup(self, context: _GlobalContext) -> None:
        module = importlib.import_module(self._target_module_name)

        for class_name in self._target_classes:
            target_cls = getattr(module, class_name, None)
            if target_cls is None:
                logger.debug(
                    "checkrd: %s.%s not found — skipping",
                    self._target_module_name,
                    class_name,
                )
                continue

            original_init = target_cls.__init__
            self._originals[class_name] = original_init
            target_cls.__init__ = self._make_patched_init(
                original_init, context
            )

    def _teardown(self) -> None:
        module = importlib.import_module(self._target_module_name)
        for class_name, original_init in self._originals.items():
            target_cls = getattr(module, class_name, None)
            if target_cls is None:
                continue
            target_cls.__init__ = original_init
        self._originals.clear()

    def _make_patched_init(
        self,
        original_init: Callable[..., None],
        context: _GlobalContext,
    ) -> Callable[..., None]:
        """Build the replacement ``__init__`` that wraps the httpx transport.

        The closure captures the original ``__init__`` and the global
        context. It runs the original first (preserving whatever the
        library wants to do with constructor args, including a
        user-supplied ``http_client=``), then swaps in a
        :class:`CheckrdTransport` on the resulting ``httpx.Client``.

        Defining this as a method rather than an inline closure inside
        :meth:`_setup` keeps the patch logic testable in isolation and
        lets subclasses override the transport-wrapping behavior without
        re-implementing the iteration over ``_target_classes``.
        """
        instrumentor = self

        def patched_init(instance: Any, *args: Any, **kwargs: Any) -> None:
            original_init(instance, *args, **kwargs)
            instrumentor._wrap_instance_transport(instance, context)

        # Preserve name and docstring for debuggers / repr.
        patched_init.__name__ = original_init.__name__
        patched_init.__qualname__ = original_init.__qualname__
        patched_init.__doc__ = original_init.__doc__
        return patched_init

    def _wrap_instance_transport(
        self,
        instance: Any,
        context: _GlobalContext,
    ) -> None:
        """Replace the transport on the httpx client attached to ``instance``.

        Finds the ``httpx.Client`` / ``httpx.AsyncClient`` via
        :attr:`_client_attr`, verifies it's the right type, and wraps its
        current ``_transport`` with a :class:`CheckrdTransport` unless
        the transport is already a Checkrd transport (idempotency).

        This is a separate method so the test suite can call it directly
        with a mock instance, verifying the wrapping logic without going
        through the ``__init__`` patch indirection.
        """
        http_client = getattr(instance, self._client_attr, None)
        if http_client is None:
            logger.warning(
                "checkrd: %s instance has no %r attribute; cannot instrument",
                type(instance).__name__,
                self._client_attr,
            )
            return

        if isinstance(http_client, httpx.AsyncClient):
            transport_class: type = CheckrdAsyncTransport
        elif isinstance(http_client, httpx.Client):
            transport_class = CheckrdTransport
        else:
            logger.warning(
                "checkrd: %s.%s is not an httpx client (got %s); cannot instrument",
                type(instance).__name__,
                self._client_attr,
                type(http_client).__name__,
            )
            return

        current_transport = http_client._transport
        if getattr(current_transport, "_checkrd_instrumented", False):
            # Already wrapped (idempotency: super chains, double-instrument,
            # or the user already called checkrd.wrap on this client).
            return

        new_transport = transport_class(
            current_transport,
            context.engine,
            enforce=context.enforce,
            batcher=context.sink,
            agent_id=context.settings.agent_id,
            dashboard_url=context.settings.dashboard_url or "",
            on_deny=context.on_deny,
            on_allow=context.on_allow,
            before_request=context.before_request,
            security_mode=context.settings.security_mode,
        )
        http_client._transport = new_transport
