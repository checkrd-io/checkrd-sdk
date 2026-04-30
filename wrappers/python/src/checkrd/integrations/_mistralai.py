"""Mistral AI SDK integration.

Patches ``mistralai.Mistral``.
"""

from __future__ import annotations

from typing import ClassVar, Tuple

from checkrd.integrations._base import HttpxClientInstrumentor


class MistralInstrumentor(HttpxClientInstrumentor):
    _target_module_name: ClassVar[str] = "mistralai"
    _target_classes: ClassVar[Tuple[str, ...]] = ("Mistral",)
