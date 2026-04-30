"""Together AI SDK integration.

Patches ``together.Together`` and ``together.AsyncTogether``.
"""

from __future__ import annotations

from typing import ClassVar, Tuple

from checkrd.integrations._base import HttpxClientInstrumentor


class TogetherInstrumentor(HttpxClientInstrumentor):
    _target_module_name: ClassVar[str] = "together"
    _target_classes: ClassVar[Tuple[str, ...]] = ("Together", "AsyncTogether")
