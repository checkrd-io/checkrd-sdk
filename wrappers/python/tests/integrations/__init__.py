"""Tests for checkrd.integrations.

Uses fake ``sys.modules`` entries for ``openai`` and ``anthropic`` so
the tests run without requiring the real SDKs installed. The fakes
mirror the shape the real libraries expose to the instrumentor: a
class with an ``__init__`` that stores an ``httpx.Client`` (or
``httpx.AsyncClient``) on ``self._client``.

This is the same pattern OpenTelemetry uses for its instrumentation
tests. It decouples correctness from upstream SDK version churn:
when openai ships a new major version that changes its client
attribute, the fakes still match our contract and our tests still
pass — and any real-library smoke test (skipped when the library
isn't installed) flags the divergence immediately.
"""
