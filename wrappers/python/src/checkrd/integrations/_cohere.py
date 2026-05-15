"""Cohere SDK integration.

Patches ``cohere.Client`` / ``cohere.AsyncClient`` (V1) and
``cohere.ClientV2`` / ``cohere.AsyncClientV2`` (V2).

The Cohere Python SDK keeps its httpx client three levels deep:
``c._client_wrapper.httpx_client.httpx_client``. The two outer
attributes are a Cohere ``SyncClientWrapper`` and a Cohere ``HttpClient``
respectively; the leaf is a real ``httpx.Client`` (or
``httpx.AsyncClient`` for the async variants).

Historical note: an earlier version of this instrumentor relied on
the base class' single-attribute heuristic (``_client_attr = "_client"``)
and only targeted the V2 surface. That worked against an older Cohere
SDK that exposed ``_client`` directly; the current package
(``cohere>=5``) renamed everything, so the heuristic silently produced
no instrumentation. Both the V1 and V2 surfaces are now covered so
``cohere.Client(...)``, ``cohere.AsyncClient(...)``, ``cohere.ClientV2(...)``,
and ``cohere.AsyncClientV2(...)`` are all instrumented.
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


class CohereInstrumentor(HttpxClientInstrumentor):
    _target_module_name: ClassVar[str] = "cohere"
    _target_classes: ClassVar[Tuple[str, ...]] = (
        "Client",
        "AsyncClient",
        "ClientV2",
        "AsyncClientV2",
    )

    # The nested path on every Cohere top-level client. The outer two
    # are Cohere's own wrapper types; the leaf is the real httpx
    # client (sync or async depending on the parent class).
    _wrapper_attr: ClassVar[str] = "_client_wrapper"
    _http_owner_attr: ClassVar[str] = "httpx_client"  # on the wrapper
    _httpx_leaf_attr: ClassVar[str] = "httpx_client"  # on the HttpClient

    def _wrap_instance_transport(
        self,
        instance: Any,
        context: _GlobalContext,
    ) -> None:
        wrapper = getattr(instance, self._wrapper_attr, None)
        if wrapper is None:
            logger.warning(
                "checkrd: %s instance has no %r attribute; cannot instrument "
                "(Cohere SDK internals changed?)",
                type(instance).__name__,
                self._wrapper_attr,
            )
            return
        owner = getattr(wrapper, self._http_owner_attr, None)
        if owner is None:
            logger.warning(
                "checkrd: %s.%s has no %r attribute; cannot instrument",
                type(instance).__name__,
                self._wrapper_attr,
                self._http_owner_attr,
            )
            return
        http_client = getattr(owner, self._httpx_leaf_attr, None)
        if http_client is None:
            logger.warning(
                "checkrd: %s.%s.%s.%s missing; cannot instrument",
                type(instance).__name__,
                self._wrapper_attr,
                self._http_owner_attr,
                self._httpx_leaf_attr,
            )
            return

        if isinstance(http_client, httpx.AsyncClient):
            transport_class: type = CheckrdAsyncTransport
        elif isinstance(http_client, httpx.Client):
            transport_class = CheckrdTransport
        else:
            logger.warning(
                "checkrd: %s.%s.%s.%s is not an httpx client (got %s); "
                "cannot instrument",
                type(instance).__name__,
                self._wrapper_attr,
                self._http_owner_attr,
                self._httpx_leaf_attr,
                type(http_client).__name__,
            )
            return

        current_transport = http_client._transport
        if getattr(current_transport, "_checkrd_instrumented", False):
            return
        http_client._transport = transport_class(
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
