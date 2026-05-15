"""Google GenAI SDK integration.

Patches ``google.genai.Client``.

The Google GenAI SDK does not expose its httpx client directly — it
holds a :class:`google.genai._api_client.BaseApiClient` at
``_api_client``, which in turn owns separate sync and async
``httpx`` clients (``_httpx_client`` and ``_async_httpx_client``).
We override :meth:`_wrap_instance_transport` to walk that two-level
path and wrap both transports, so the same ``Client`` can be used
for both ``models.generate_content(...)`` (sync) and
``models.aio.generate_content(...)`` (async).

History: the base class only supported a single flat attribute name
on the patched instance. When Google moved their httpx client behind
an internal ``BaseApiClient`` (some time after their 1.0 release),
that flat-attribute heuristic silently broke and every
``GoogleGenAI()`` started slipping past Checkrd. This override walks
the new layout and is itself defensive so a third rearrangement
would log a warning rather than crash the user's app.
"""

from __future__ import annotations

from typing import Any, ClassVar, Tuple

import httpx

from checkrd._state import _GlobalContext
from checkrd.integrations._base import (
    CheckrdAsyncTransport,
    CheckrdTransport,
    HttpxClientInstrumentor,
    logger,
)


class GoogleGenAIInstrumentor(HttpxClientInstrumentor):
    _target_module_name: ClassVar[str] = "google.genai"
    _target_classes: ClassVar[Tuple[str, ...]] = ("Client",)

    # Path on the patched ``google.genai.Client`` to the inner
    # BaseApiClient. The base class' single-attribute heuristic
    # cannot reach the two httpx clients it owns, so we look up
    # this owner and wrap both transports below.
    _owner_attr: ClassVar[str] = "_api_client"
    # Attributes on the BaseApiClient that hold sync / async httpx
    # clients respectively. Each is wrapped independently so a Client
    # instance used for both sync + async calls is fully covered.
    _sync_client_attr: ClassVar[str] = "_httpx_client"
    _async_client_attr: ClassVar[str] = "_async_httpx_client"

    def _wrap_instance_transport(
        self,
        instance: Any,
        context: _GlobalContext,
    ) -> None:
        owner = getattr(instance, self._owner_attr, None)
        if owner is None:
            logger.warning(
                "checkrd: %s instance has no %r attribute; cannot instrument "
                "(Google GenAI internals changed?)",
                type(instance).__name__,
                self._owner_attr,
            )
            return

        for attr_name, transport_cls, expected_cls in (
            (self._sync_client_attr, CheckrdTransport, httpx.Client),
            (self._async_client_attr, CheckrdAsyncTransport, httpx.AsyncClient),
        ):
            http_client = getattr(owner, attr_name, None)
            if http_client is None:
                # Half the layout — sync-only or async-only API surface.
                # Not necessarily an error; log at debug for diagnostics.
                logger.debug(
                    "checkrd: %s.%s.%s missing; skipping that half",
                    type(instance).__name__,
                    self._owner_attr,
                    attr_name,
                )
                continue
            if not isinstance(http_client, expected_cls):
                logger.warning(
                    "checkrd: %s.%s.%s is not an %s (got %s); cannot instrument",
                    type(instance).__name__,
                    self._owner_attr,
                    attr_name,
                    expected_cls.__name__,
                    type(http_client).__name__,
                )
                continue

            current_transport = http_client._transport
            if getattr(current_transport, "_checkrd_instrumented", False):
                continue
            http_client._transport = transport_cls(
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
