/**
 * End-to-end staging canary — JavaScript SDK.
 *
 * Mirrors ``wrappers/python/tests/e2e/test_staging_canary.py``.
 * Three round-trip tests against a live staging control plane:
 *
 *   1. ``/health`` reachability — the smoke test.
 *   2. Signed telemetry batch ingestion — verifies signing,
 *      idempotency, and schema-version compatibility on the wire.
 *   3. ``AuthenticationError`` dispatch on a bogus API key —
 *      regression test for the exception hierarchy at the real
 *      wire boundary.
 *
 * Skipped silently when ``CHECKRD_STAGING_URL`` is unset, so PR CI
 * sees no red. The nightly canary workflow exports the env vars and
 * runs the suite against ``api-staging.checkrd.io``.
 */

import { describe, expect, it, beforeAll } from "vitest";

import {
  AuthenticationError,
  Checkrd,
  makeAPIError,
} from "../../src/index.js";
import { WasmEngine } from "../../src/advanced.js";
import { TelemetryBatcher } from "../../src/batcher.js";

const STAGING_URL = process.env["CHECKRD_STAGING_URL"];
const STAGING_API_KEY = process.env["CHECKRD_STAGING_API_KEY"];

// vitest's ``it.skipIf`` runs the body only when the predicate is
// false. The two skip predicates are independent so a contributor
// can run the public ``/health`` check without provisioning a
// staging API key.
const skipNoUrl = STAGING_URL === undefined || STAGING_URL.length === 0;
const skipNoKey =
  skipNoUrl ||
  STAGING_API_KEY === undefined ||
  STAGING_API_KEY.length === 0;

const ALLOW_ALL_POLICY = JSON.stringify({
  agent: "canary",
  default: "allow",
  rules: [],
});

function uniqueAgentId(): string {
  return `canary-${globalThis.crypto.randomUUID().slice(0, 8)}`;
}

describe("staging canary", () => {
  beforeAll(() => {
    if (skipNoUrl) {
      console.log("CHECKRD_STAGING_URL not set; skipping staging canary");
    }
  });

  it.skipIf(skipNoUrl)("healthz reachable", async () => {
    const url = `${STAGING_URL!.replace(/\/$/, "")}/health`;
    const response = await fetch(url, {
      signal: AbortSignal.timeout(10_000),
    });
    expect(response.status).toBe(200);
  });

  it.skipIf(skipNoKey)("signed telemetry batch accepted", async () => {
    // Construct a real engine + batcher. The full hot path runs:
    // Ed25519 sign, RFC 9421 Signature-Input/Signature, RFC 9530
    // Content-Digest, idempotency key, default control headers,
    // and the canonical body serialization.
    const agentId = uniqueAgentId();
    const engine = new WasmEngine(ALLOW_ALL_POLICY, agentId);
    const batcher = new TelemetryBatcher({
      controlPlaneUrl: STAGING_URL!,
      apiKey: STAGING_API_KEY!,
      engine,
      agentId,
      apiVersion: "",
    });

    const event = {
      event_id: `canary-${Date.now().toString()}`,
      agent_id: agentId,
      timestamp: Date.now(),
      policy_result: "allow" as const,
      request: {
        url_host: "canary.example.com",
        url_path: "/v1/canary",
        method: "POST",
      },
      response: { status_code: 200, latency_ms: 1 },
    };
    batcher.enqueue(event);
    await batcher.flush();
    const diag = batcher.diagnostics();
    // Either the batch was accepted or we got a structured drop tag —
    // both are diagnosable. A network/HTTP failure on a real wire
    // call indicates either an auth issue or a schema-version drift
    // between the SDK and the deployed ingestion service.
    expect(diag.droppedSendError).toBe(0);
    expect(diag.sent).toBeGreaterThanOrEqual(1);
    await batcher.stop();
  });

  it.skipIf(skipNoUrl)(
    "bogus API key returns AuthenticationError",
    async () => {
      // The control plane MUST return 401 for an unrecognized key,
      // and the SDK's makeAPIError dispatch MUST hand back an
      // AuthenticationError instance (not a bare APIStatusError).
      const url = `${STAGING_URL!.replace(/\/$/, "")}/v1/orgs`;
      const response = await fetch(url, {
        headers: { "X-API-Key": "ck_live_definitely_not_a_real_key" },
        signal: AbortSignal.timeout(10_000),
      });
      expect(response.status).toBe(401);
      const body = (await response.json()) as { error?: { code?: string } };
      const headers: Record<string, string> = {};
      response.headers.forEach((value, key) => {
        headers[key.toLowerCase()] = value;
      });
      const err = makeAPIError({
        status: response.status,
        body: body.error,
        headers,
        requestId: headers["checkrd-request-id"],
        message: "bogus key",
      });
      expect(err).toBeInstanceOf(AuthenticationError);
      expect((err as AuthenticationError).status).toBe(401);
    },
  );

  it.skipIf(skipNoKey)(
    "Checkrd class round-trip with wrap()",
    async () => {
      // High-level integration: construct a Checkrd, wrap a fetch,
      // make a single round-trip to the control plane via the
      // wrapped fetch (the /health endpoint is unauthenticated and
      // safe to call from any wrapped fetch).
      const checkrd = new Checkrd({
        apiKey: STAGING_API_KEY!,
        controlPlaneUrl: STAGING_URL!,
        agentId: uniqueAgentId(),
        policy: ALLOW_ALL_POLICY,
      });
      try {
        const wrapped = checkrd.wrap(globalThis.fetch);
        const url = `${STAGING_URL!.replace(/\/$/, "")}/health`;
        const response = await wrapped(url, {
          signal: AbortSignal.timeout(10_000),
        });
        expect(response.status).toBe(200);
      } finally {
        await checkrd.close();
      }
    },
  );
});
