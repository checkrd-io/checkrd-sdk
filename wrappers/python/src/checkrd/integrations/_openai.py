"""OpenAI SDK integration.

Patches :class:`openai.OpenAI` and :class:`openai.AsyncOpenAI` so every
request goes through the Checkrd policy engine. The
:class:`openai.AzureOpenAI` and :class:`openai.AsyncAzureOpenAI`
subclasses are covered automatically because they call
``super().__init__()``.

Usage::

    import checkrd
    checkrd.init(policy="policy.yaml")
    checkrd.instrument_openai()

    from openai import OpenAI
    client = OpenAI()   # now proxied through Checkrd
    client.chat.completions.create(model="gpt-4o", messages=[...])

The OpenAI SDK stores its internal ``httpx.Client`` on the
``_client`` attribute of the main ``OpenAI`` / ``AsyncOpenAI`` class
(inherited from ``openai._base_client.SyncAPIClient`` /
``AsyncAPIClient``), which is the standard shape our
:class:`checkrd.integrations.HttpxClientInstrumentor` targets.
"""

from __future__ import annotations

from typing import ClassVar, Tuple

from checkrd.integrations._base import HttpxClientInstrumentor


class OpenAIInstrumentor(HttpxClientInstrumentor):
    """Auto-instrumentor for the ``openai`` Python SDK.

    Patches ``OpenAI`` and ``AsyncOpenAI``. Their Azure subclasses
    (``AzureOpenAI``, ``AsyncAzureOpenAI``) are covered transitively
    because they call ``super().__init__()`` which now flows through
    the patched sync / async parent ``__init__``.

    This instrumentor is a near-empty subclass: all the behavior lives
    in :class:`HttpxClientInstrumentor`. Two declarative class
    attributes fully specify what to patch, which makes adding support
    for additional httpx-based SDKs (cohere, mistralai, groq, fireworks,
    together, etc.) a one-file change.
    """

    _target_module_name: ClassVar[str] = "openai"
    _target_classes: ClassVar[Tuple[str, ...]] = ("OpenAI", "AsyncOpenAI")
