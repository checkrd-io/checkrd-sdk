/**
 * Tests for the public-key registrar.
 *
 * The registrar is the security-critical glue that ensures the control
 * plane has a key to verify telemetry signatures against. Failures
 * silently drop the SDK to "signed but unverifiable" mode, so every
 * branch (success, permanent failure, transient retry, exhaustion)
 * deserves explicit coverage.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { registerPublicKey } from "../src/_key_registrar.js";

const PUBLIC_KEY = new Uint8Array(32).fill(0xab);

function noopLogger(): {
  debug: ReturnType<typeof vi.fn>;
  info: ReturnType<typeof vi.fn>;
  warn: ReturnType<typeof vi.fn>;
  error: ReturnType<typeof vi.fn>;
} {
  return {
    debug: vi.fn(),
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
  };
}

beforeEach(() => {
  // Speed up retry sleeps so tests don't actually wait seconds.
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("registerPublicKey — happy path", () => {
  it("POSTs to /v1/agents/:id/public-key with hex-encoded key", async () => {
    const fetch = vi.fn(async () => new Response(null, { status: 204 }));
    await registerPublicKey({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_live_test",
      agentId: "test-agent",
      publicKey: PUBLIC_KEY,
      fetch: fetch as unknown as typeof globalThis.fetch,
    });
    expect(fetch).toHaveBeenCalledOnce();
    const call = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(call[0]).toBe("https://api.example.com/v1/agents/test-agent/public-key");
    expect(call[1].method).toBe("POST");
    const body = JSON.parse(call[1].body as string) as { public_key: string };
    expect(body.public_key).toBe("ab".repeat(32));
  });

  it("URL-encodes the agent id", async () => {
    const fetch = vi.fn(async () => new Response(null, { status: 204 }));
    await registerPublicKey({
      controlPlaneUrl: "https://api.example.com/",
      apiKey: "ck_live_test",
      agentId: "team/agent with spaces",
      publicKey: PUBLIC_KEY,
      fetch: fetch as unknown as typeof globalThis.fetch,
    });
    const call = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(call[0]).toBe(
      "https://api.example.com/v1/agents/team%2Fagent%20with%20spaces/public-key",
    );
  });

  it("strips any trailing slashes from controlPlaneUrl", async () => {
    const fetch = vi.fn(async () => new Response(null, { status: 204 }));
    await registerPublicKey({
      controlPlaneUrl: "https://api.example.com////",
      apiKey: "ck_live_test",
      agentId: "a",
      publicKey: PUBLIC_KEY,
      fetch: fetch as unknown as typeof globalThis.fetch,
    });
    const call = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(call[0]).toBe("https://api.example.com/v1/agents/a/public-key");
  });

  it("sends the API key in X-API-Key", async () => {
    const fetch = vi.fn(async () => new Response(null, { status: 204 }));
    await registerPublicKey({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_live_secret",
      agentId: "a",
      publicKey: PUBLIC_KEY,
      fetch: fetch as unknown as typeof globalThis.fetch,
    });
    const call = fetch.mock.calls[0] as unknown as [string, RequestInit];
    const headers = call[1].headers as Record<string, string>;
    expect(headers["X-API-Key"]).toBe("ck_live_secret");
    expect(headers["Content-Type"]).toBe("application/json");
  });

  it("sets a stable Idempotency-Key across retries", async () => {
    let firstKey: string | undefined;
    const fetch = vi.fn(async (_url: unknown, init: RequestInit) => {
      const headers = init.headers as Record<string, string>;
      if (firstKey === undefined) {
        firstKey = headers["Idempotency-Key"];
        return new Response(null, { status: 503 });
      }
      // Stable across the retry — required for server-side dedup.
      expect(headers["Idempotency-Key"]).toBe(firstKey);
      return new Response(null, { status: 204 });
    });
    await registerPublicKey({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_live",
      agentId: "a",
      publicKey: PUBLIC_KEY,
      fetch: fetch as unknown as typeof globalThis.fetch,
      logger: noopLogger(),
    });
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});

describe("registerPublicKey — permanent failures", () => {
  it("does not retry on 409 (key mismatch)", async () => {
    const log = noopLogger();
    const fetch = vi.fn(async () => new Response(null, { status: 409 }));
    await registerPublicKey({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_live",
      agentId: "test-agent",
      publicKey: PUBLIC_KEY,
      fetch: fetch as unknown as typeof globalThis.fetch,
      logger: log,
    });
    expect(fetch).toHaveBeenCalledOnce();
    expect(log.warn).toHaveBeenCalled();
    const warnArgs = log.warn.mock.calls[0]?.[0] as string;
    expect(warnArgs).toContain("test-agent");
    expect(warnArgs).toContain("revoke");
  });

  it("does not retry on 401 (auth)", async () => {
    const log = noopLogger();
    const fetch = vi.fn(async () => new Response(null, { status: 401 }));
    await registerPublicKey({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_live_bad",
      agentId: "a",
      publicKey: PUBLIC_KEY,
      fetch: fetch as unknown as typeof globalThis.fetch,
      logger: log,
    });
    expect(fetch).toHaveBeenCalledOnce();
    expect(log.warn).toHaveBeenCalled();
  });

  it("does not retry on 403 (forbidden)", async () => {
    const fetch = vi.fn(async () => new Response(null, { status: 403 }));
    await registerPublicKey({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_live",
      agentId: "a",
      publicKey: PUBLIC_KEY,
      fetch: fetch as unknown as typeof globalThis.fetch,
      logger: noopLogger(),
    });
    expect(fetch).toHaveBeenCalledOnce();
  });

  it("rejects keys of the wrong length without making a request", async () => {
    const log = noopLogger();
    const fetch = vi.fn(async () => new Response(null, { status: 204 }));
    await registerPublicKey({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_live",
      agentId: "a",
      publicKey: new Uint8Array(16), // wrong length
      fetch: fetch as unknown as typeof globalThis.fetch,
      logger: log,
    });
    expect(fetch).not.toHaveBeenCalled();
    expect(log.warn).toHaveBeenCalled();
  });
});

describe("registerPublicKey — transient failures", () => {
  it("retries on 5xx and succeeds before exhaustion", async () => {
    let calls = 0;
    const fetch = vi.fn(async () => {
      calls++;
      if (calls < 2) return new Response(null, { status: 503 });
      return new Response(null, { status: 204 });
    });
    await registerPublicKey({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_live",
      agentId: "a",
      publicKey: PUBLIC_KEY,
      fetch: fetch as unknown as typeof globalThis.fetch,
      logger: noopLogger(),
    });
    expect(calls).toBe(2);
  });

  it("retries on network failure (thrown error) and succeeds", async () => {
    let calls = 0;
    const fetch = vi.fn(async () => {
      calls++;
      if (calls < 2) throw new TypeError("fetch failed");
      return new Response(null, { status: 204 });
    });
    await registerPublicKey({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_live",
      agentId: "a",
      publicKey: PUBLIC_KEY,
      fetch: fetch as unknown as typeof globalThis.fetch,
      logger: noopLogger(),
    });
    expect(calls).toBe(2);
  });

  it("logs a clear warning after exhausting all retries", async () => {
    const log = noopLogger();
    const fetch = vi.fn(async () => new Response(null, { status: 503 }));
    await registerPublicKey({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_live",
      agentId: "stuck-agent",
      publicKey: PUBLIC_KEY,
      fetch: fetch as unknown as typeof globalThis.fetch,
      logger: log,
    });
    expect(fetch).toHaveBeenCalledTimes(3);
    const final = log.warn.mock.calls.at(-1)?.[0] as string;
    expect(final).toContain("stuck-agent");
    expect(final).toContain("api.example.com");
  });
});
