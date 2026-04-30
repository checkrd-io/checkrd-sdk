"""Groq SDK integration.

Patches ``groq.Groq`` and ``groq.AsyncGroq``.
"""

from __future__ import annotations

from typing import ClassVar, Tuple

from checkrd.integrations._base import HttpxClientInstrumentor


class GroqInstrumentor(HttpxClientInstrumentor):
    _target_module_name: ClassVar[str] = "groq"
    _target_classes: ClassVar[Tuple[str, ...]] = ("Groq", "AsyncGroq")
