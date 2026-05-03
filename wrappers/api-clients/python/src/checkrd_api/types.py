"""Public type re-exports.

Models are defined by the generator (under
``checkrd_api._generated.models``) but the convention in
Stainless-generated SDKs (OpenAI, Anthropic) is to surface them
under ``<package>.types`` so user code reads naturally::

    from checkrd_api.types import Agent

This file is the only place external code should import models
from. The ``_generated`` subpackage is not part of the supported
API and may be replaced or restructured by the generator at any
time; ``checkrd_api.types`` is the stable contract.
"""
from __future__ import annotations

from ._generated.models import (
    Agent,
    CreateAgentRequest,
    DeleteResponse,
    ErrorBody,
    ErrorResponse,
    KillSwitchRequest,
    PaginatedAgents,
    RegisterPublicKeyRequest,
    RegisterPublicKeyResponse,
    UpdateAgentRequest,
)

__all__ = [
    "Agent",
    "CreateAgentRequest",
    "DeleteResponse",
    "ErrorBody",
    "ErrorResponse",
    "KillSwitchRequest",
    "PaginatedAgents",
    "RegisterPublicKeyRequest",
    "RegisterPublicKeyResponse",
    "UpdateAgentRequest",
]
