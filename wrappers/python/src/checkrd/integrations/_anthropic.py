"""Anthropic SDK integration.

Patches :class:`anthropic.Anthropic` and :class:`anthropic.AsyncAnthropic`
so every request goes through the Checkrd policy engine. The Bedrock and
Vertex subclasses (``AnthropicBedrock``, ``AnthropicVertex``, and their
async counterparts) are covered automatically because they call
``super().__init__()``.

Usage::

    import checkrd
    checkrd.init(policy="policy.yaml")
    checkrd.instrument_anthropic()

    from anthropic import Anthropic
    client = Anthropic()   # now proxied through Checkrd
    client.messages.create(model="claude-opus-4-6", messages=[...])

The Anthropic SDK stores its internal ``httpx.Client`` on the ``_client``
attribute of the main ``Anthropic`` / ``AsyncAnthropic`` class (inherited
from ``anthropic._base_client.SyncAPIClient`` / ``AsyncAPIClient``), which
is the standard shape our :class:`checkrd.integrations.HttpxClientInstrumentor`
targets.
"""

from __future__ import annotations

from typing import ClassVar, Tuple

from checkrd.integrations._base import HttpxClientInstrumentor


class AnthropicInstrumentor(HttpxClientInstrumentor):
    """Auto-instrumentor for the ``anthropic`` Python SDK.

    Patches ``Anthropic`` and ``AsyncAnthropic``. Their Bedrock / Vertex
    subclasses are covered transitively because they call
    ``super().__init__()`` which now flows through the patched sync /
    async parent ``__init__``.

    Like :class:`OpenAIInstrumentor`, this is a near-empty subclass:
    two declarative class attributes specify the target. The real
    patching logic lives in :class:`HttpxClientInstrumentor`.
    """

    _target_module_name: ClassVar[str] = "anthropic"
    _target_classes: ClassVar[Tuple[str, ...]] = ("Anthropic", "AsyncAnthropic")
