"""Google GenAI SDK integration.

Patches ``google.genai.Client``.
"""

from __future__ import annotations

from typing import ClassVar, Tuple

from checkrd.integrations._base import HttpxClientInstrumentor


class GoogleGenAIInstrumentor(HttpxClientInstrumentor):
    _target_module_name: ClassVar[str] = "google.genai"
    _target_classes: ClassVar[Tuple[str, ...]] = ("Client",)
