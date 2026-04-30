"""End-to-end staging canary tests.

Every test in this package hits a real Checkrd control plane —
typically the staging deployment at the URL pointed to by
``CHECKRD_STAGING_URL``. They exist to catch wire-protocol regressions
that the in-process MSW / httpx-mock unit tests miss: signature
verification mismatches, idempotency-key dedupe behavior, SSE
reconnection under real network conditions, the telemetry-ingestion
schema-version contract.

Skip behavior:
    - Tests skip silently when ``CHECKRD_STAGING_URL`` is unset, so
      contributors and PR CI see no red. The expectation is that the
      nightly canary workflow exports the var and runs them against
      ``api-staging.checkrd.io``.
    - Tests also skip when ``CHECKRD_STAGING_API_KEY`` is missing —
      most endpoints require auth and an unauthenticated 401 isn't
      a useful signal.

Cost discipline:
    Each test sends one telemetry event or one control-plane request.
    The full canary suite costs cents per run, not dollars. Do not
    add tests that hammer the control plane — load tests live
    elsewhere.
"""
