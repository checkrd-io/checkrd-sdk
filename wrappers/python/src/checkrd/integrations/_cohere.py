"""Cohere SDK integration.

Patches ``cohere.ClientV2`` and ``cohere.AsyncClientV2``.
"""

from __future__ import annotations

from typing import ClassVar, Tuple

from checkrd.integrations._base import HttpxClientInstrumentor


class CohereInstrumentor(HttpxClientInstrumentor):
    _target_module_name: ClassVar[str] = "cohere"
    _target_classes: ClassVar[Tuple[str, ...]] = ("ClientV2", "AsyncClientV2")
