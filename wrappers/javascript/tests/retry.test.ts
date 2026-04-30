import { describe, expect, it, vi } from "vitest";

import { Checkrd } from "../src/client.js";

import {
  defaultControlHeaders,
  fetchWithRetry,
  newIdempotencyKey,
} from "../src/_retry.js";
import {
  RateLimitError,
  InternalServerError,
  APIConnectionError,
  APIUserAbortError,
  BadRequestError,
} from "../src/exceptions.js";

describe("newIdempotencyKey", () => {
  it("returns a unique prefixed UUID on each call", () => {
    const a = newIdempotencyKey();
    const b = newIdempotencyKey();
    expect(a).toMatch(/^checkrd-/);
    expect(b).toMatch(/^checkrd-/);
    expect(a).not.toBe(b);
  });
});

describe("defaultControlHeaders", () => {
  it("stamps Content-Type, X-API-Key, and a fresh Idempotency-Key", () => {
    const h = defaultControlHeaders("ck_live_abc");
    expect(h["Content-Type"]).toBe("application/json");
    expect(h["X-API-Key"]).toBe("ck_live_abc");
    expect(h["Idempotency-Key"]).toMatch(/^checkrd-/);
  });
});

describe("fetchWithRetry", () => {
  it("returns 2xx responses immediately without retrying", async () => {
    const fetch = vi.fn(async () => new Response("{}", { status: 200 }));
    const res = await fetchWithRetry(
      "https://example.com/",
      { method: "POST" },
      { fetch: fetch as unknown as typeof globalThis.fetch },
    );
    expect(res.status).toBe(200);
    expect(fetch).toHaveBeenCalledOnce();
  });

  describe("redirect handling", () => {
    it('passes redirect: "error" by default to the underlying fetch', async () => {
      // The control plane is a known single-origin endpoint; it should
      // never redirect. Defaulting to "error" turns a redirect from
      // ``api.checkrd.io`` (which would only happen via DNS hijack /
      // compromised proxy / corporate-MITM) into a clean failure
      // instead of silently following to an attacker-controlled host.
      const fetch = vi.fn(async () => new Response("{}", { status: 200 }));
      await fetchWithRetry(
        "https://example.com/",
        { method: "POST" },
        { fetch: fetch as unknown as typeof globalThis.fetch },
      );
      // vi.fn infers a zero-arg signature from the body; cast the
      // captured call to the real fetch signature so we can read
      // the second positional ``init`` argument.
      const calls = fetch.mock.calls as unknown as [string, RequestInit][];
      const init = calls[0]?.[1];
      expect(init?.redirect).toBe("error");
    });

    it("honors caller-supplied redirect mode (override)", async () => {
      // Some private gateways legitimately redirect to internal URLs
      // (e.g., a customer's reverse proxy behind their own auth).
      // Operators that need this hand the explicit redirect mode in
      // through ``init`` and the retry helper passes it untouched.
      const fetch = vi.fn(async () => new Response("{}", { status: 200 }));
      await fetchWithRetry(
        "https://example.com/",
        { method: "POST", redirect: "follow" },
        { fetch: fetch as unknown as typeof globalThis.fetch },
      );
      // vi.fn infers a zero-arg signature from the body; cast the
      // captured call to the real fetch signature so we can read
      // the second positional ``init`` argument.
      const calls = fetch.mock.calls as unknown as [string, RequestInit][];
      const init = calls[0]?.[1];
      expect(init?.redirect).toBe("follow");
    });
  });

  it("retries 500 errors up to maxAttempts, then throws InternalServerError", async () => {
    const fetch = vi.fn(async () => new Response("boom", { status: 500 }));
    await expect(
      fetchWithRetry(
        "https://example.com/",
        { method: "POST" },
        {
          fetch: fetch as unknown as typeof globalThis.fetch,
          maxAttempts: 3,
          maxSleepSecs: 0,
        },
      ),
    ).rejects.toBeInstanceOf(InternalServerError);
    expect(fetch).toHaveBeenCalledTimes(3);
  });

  it("does not retry 400 — raises BadRequestError on first attempt", async () => {
    const fetch = vi.fn(
      async () => new Response(JSON.stringify({ error: { message: "bad" } }), { status: 400 }),
    );
    await expect(
      fetchWithRetry(
        "https://example.com/",
        { method: "POST" },
        { fetch: fetch as unknown as typeof globalThis.fetch },
      ),
    ).rejects.toBeInstanceOf(BadRequestError);
    expect(fetch).toHaveBeenCalledOnce();
  });

  it("honors retry-after-ms server hint on 429", async () => {
    let call = 0;
    const start = Date.now();
    const fetch = vi.fn(async () => {
      call += 1;
      if (call === 1) {
        return new Response("rl", {
          status: 429,
          headers: { "retry-after-ms": "50" },
        });
      }
      return new Response("ok", { status: 200 });
    });
    const res = await fetchWithRetry(
      "https://example.com/",
      { method: "POST" },
      { fetch: fetch as unknown as typeof globalThis.fetch, maxAttempts: 2 },
    );
    expect(res.status).toBe(200);
    // Allow generous slack for CI schedulers; we only verify the hint was honored.
    expect(Date.now() - start).toBeGreaterThanOrEqual(40);
  });

  it("raises RateLimitError after exhausting retries on 429", async () => {
    const fetch = vi.fn(
      async () => new Response("rl", { status: 429, headers: { "retry-after-ms": "1" } }),
    );
    await expect(
      fetchWithRetry(
        "https://example.com/",
        { method: "POST" },
        { fetch: fetch as unknown as typeof globalThis.fetch, maxAttempts: 2 },
      ),
    ).rejects.toBeInstanceOf(RateLimitError);
  });

  it("wraps network errors in APIConnectionError after final retry", async () => {
    const fetch = vi.fn(async () => { throw new Error("ECONNREFUSED"); });
    await expect(
      fetchWithRetry(
        "https://example.com/",
        { method: "POST" },
        {
          fetch: fetch as unknown as typeof globalThis.fetch,
          maxAttempts: 2,
          maxSleepSecs: 0,
        },
      ),
    ).rejects.toBeInstanceOf(APIConnectionError);
  });

  it("raises APIUserAbortError when the signal is already aborted", async () => {
    const controller = new AbortController();
    controller.abort();
    const fetch = vi.fn(async () => new Response("should not call"));
    await expect(
      fetchWithRetry(
        "https://example.com/",
        { method: "POST" },
        { fetch: fetch as unknown as typeof globalThis.fetch, signal: controller.signal },
      ),
    ).rejects.toBeInstanceOf(APIUserAbortError);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("stamps X-Checkrd-Retry-Count on retry attempts (not the first)", async () => {
    const seen: (string | null)[] = [];
    const fetch = vi.fn(async (_url: string, init: RequestInit) => {
      const headers = new Headers(init.headers);
      seen.push(headers.get("X-Checkrd-Retry-Count"));
      // Always 503 → forces 3 attempts.
      return new Response("boom", { status: 503 });
    });
    await expect(
      fetchWithRetry(
        "https://example.com/",
        { method: "POST", headers: { "X-API-Key": "ck_live_xyz" } },
        {
          fetch: fetch as unknown as typeof globalThis.fetch,
          maxAttempts: 3,
          maxSleepSecs: 0,
        },
      ),
    ).rejects.toBeInstanceOf(InternalServerError);
    expect(seen).toEqual([null, "1", "2"]);
  });
});

describe("Checkrd constructor — public retry/timeout API", () => {
  it("accepts maxRetries / timeout / connectTimeout without throwing", () => {
    expect(
      () => new Checkrd({ maxRetries: 5, timeout: 60_000, connectTimeout: 10_000 }),
    ).not.toThrow();
  });

  it("preserves retry/timeout overrides through withOptions", () => {
    const c = new Checkrd({ maxRetries: 5, timeout: 60_000 });
    const c2 = c.withOptions({ maxRetries: 10 });
    // Frozen options expose the overrides for introspection.
    expect((c2 as unknown as { options: { maxRetries?: number; timeout?: number } }).options.maxRetries).toBe(10);
    expect((c2 as unknown as { options: { maxRetries?: number; timeout?: number } }).options.timeout).toBe(60_000);
  });
});
