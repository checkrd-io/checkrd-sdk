/**
 * Smoke tests for the resource-based facade.
 *
 * Mirrors the Python sibling (`tests/test_facade.py`) — same
 * happy-path + error-mapping + header-injection coverage so both
 * SDKs are calibrated against the same bar.
 */
import { describe, expect, it, vi } from "vitest";

import Checkrd, {
  APIConnectionError,
  APIStatusError,
  AuthenticationError,
  BadRequestError,
  ConflictError,
  DEFAULT_API_VERSION,
  DEFAULT_BASE_URL,
  DEFAULT_MAX_RETRIES,
  DEFAULT_TIMEOUT_MS,
  InternalServerError,
  NotFoundError,
  PermissionDeniedError,
  RateLimitError,
  UnprocessableEntityError,
} from "../src/index.js";

const sampleAgent = {
  id: "11111111-1111-1111-1111-111111111111",
  org_id: "00000000-0000-0000-0000-000000000000",
  name: "agent-a",
  slug: "agent-a",
  description: null,
  status: "active",
  public_key: null,
  kill_switch_active: false,
  active_policy_mode: null,
  created_at: "2026-04-15T10:00:00Z",
};

function mockFetch(
  responses: Array<{ status: number; body?: unknown; headers?: Record<string, string> }>,
): typeof fetch {
  let i = 0;
  return vi.fn(async () => {
    const r = responses[i++] ?? { status: 500 };
    const headers = new Headers({
      "content-type": "application/json",
      ...(r.headers ?? {}),
    });
    return new Response(JSON.stringify(r.body ?? {}), {
      status: r.status,
      headers,
    });
  }) as unknown as typeof fetch;
}

// ---------------------------------------------------------------------------
// Construction + withOptions
// ---------------------------------------------------------------------------

describe("Checkrd construction", () => {
  it("uses default options when none are provided", () => {
    const client = new Checkrd();
    expect(client.apiVersion).toBe(DEFAULT_API_VERSION);
    expect(client.maxRetries).toBe(DEFAULT_MAX_RETRIES);
    expect(client.baseURL).toBe(DEFAULT_BASE_URL);
    expect(client.timeoutMs).toBe(DEFAULT_TIMEOUT_MS);
  });

  it("withOptions layers overrides without mutating the original", () => {
    const client = new Checkrd({ apiKey: "ck_test_x", maxRetries: 2, timeoutMs: 60_000 });
    const layered = client.withOptions({ maxRetries: 5, timeoutMs: 10_000 });
    expect(layered.maxRetries).toBe(5);
    expect(layered.timeoutMs).toBe(10_000);
    expect(client.maxRetries).toBe(2);
    expect(client.timeoutMs).toBe(60_000);
  });

  it("attaches resources as fields", () => {
    const client = new Checkrd({ apiKey: "ck_test_x" });
    expect(client.agents).toBeDefined();
    expect(client.agents.list).toBeInstanceOf(Function);
  });
});

// ---------------------------------------------------------------------------
// Pagination
// ---------------------------------------------------------------------------

describe("agents.list", () => {
  it("yields items across pages transparently via async iterator", async () => {
    const fetchImpl = mockFetch([
      {
        status: 200,
        body: {
          data: [{ ...sampleAgent, name: "agent-a" }],
          has_more: true,
          next_cursor: sampleAgent.id,
        },
      },
      {
        status: 200,
        body: {
          data: [{ ...sampleAgent, name: "agent-b", id: "22222222-2222-2222-2222-222222222222" }],
          has_more: false,
          next_cursor: null,
        },
      },
    ]);
    const client = new Checkrd({
      apiKey: "ck_test_x",
      baseURL: "https://api.example.test",
      fetch: fetchImpl,
    });
    const names: string[] = [];
    for await (const a of client.agents.list()) names.push(a.name);
    expect(names).toEqual(["agent-a", "agent-b"]);
  });

  it("first await returns just the first page", async () => {
    const fetchImpl = mockFetch([
      { status: 200, body: { data: [sampleAgent], has_more: false, next_cursor: null } },
    ]);
    const client = new Checkrd({
      apiKey: "ck_test_x",
      baseURL: "https://api.example.test",
      fetch: fetchImpl,
    });
    const page = await client.agents.list();
    expect(page.data).toHaveLength(1);
    expect(page.hasMore).toBe(false);
    expect(page.hasNextPage()).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Error class hierarchy
// ---------------------------------------------------------------------------

describe("status code → error subclass", () => {
  const cases: Array<[number, new (...args: never[]) => APIStatusError]> = [
    [400, BadRequestError],
    [401, AuthenticationError],
    [403, PermissionDeniedError],
    [404, NotFoundError],
    [409, ConflictError],
    [422, UnprocessableEntityError],
    [429, RateLimitError],
    [500, InternalServerError],
    [503, InternalServerError],
  ];

  for (const [status, ExpectedClass] of cases) {
    it(`maps ${String(status)} to ${ExpectedClass.name}`, async () => {
      // Flat RFC 9457 problem+json — the whole object IS the document.
      const fetchImpl = mockFetch([
        {
          status,
          body: {
            type: `https://checkrd.io/errors/test_code`,
            title: "Test Error",
            status,
            detail: `oops ${String(status)}`,
            instance: "/v1/agents/abc",
            code: "test_code",
          },
        },
      ]);
      const client = new Checkrd({
        apiKey: "ck_test_x",
        baseURL: "https://api.example.test",
        maxRetries: 0,
        fetch: fetchImpl,
      });
      try {
        await client.agents.retrieve("abc");
        throw new Error("expected throw");
      } catch (err) {
        expect(err).toBeInstanceOf(ExpectedClass);
        const errStatus = (err as APIStatusError).status;
        expect(typeof errStatus).toBe("number");
        // Top-level `code` (flat), not `body.error.code`.
        expect((err as APIStatusError).code).toBe("test_code");
        // Message derived from `detail` (flat).
        expect((err as APIStatusError).message).toContain("oops");
        // The whole flat document is exposed on `.body`.
        expect((err as APIStatusError).body?.type).toBe(
          "https://checkrd.io/errors/test_code",
        );
        expect((err as APIStatusError).body?.detail).toBe(`oops ${String(status)}`);
      }
    });
  }

  it("falls back to title then HTTP <status> when detail is absent", async () => {
    const fetchImpl = mockFetch([
      {
        status: 403,
        body: { title: "Forbidden", status: 403, code: "permission_denied" },
      },
    ]);
    const client = new Checkrd({
      apiKey: "ck_test_x",
      baseURL: "https://api.example.test",
      maxRetries: 0,
      fetch: fetchImpl,
    });
    try {
      await client.agents.retrieve("abc");
      throw new Error("expected throw");
    } catch (err) {
      expect(err).toBeInstanceOf(PermissionDeniedError);
      expect((err as APIStatusError).code).toBe("permission_denied");
      expect((err as APIStatusError).message).toBe("Forbidden");
    }
  });

  it("exposes per-field validation errors and .param on a 422", async () => {
    const fetchImpl = mockFetch([
      {
        status: 422,
        body: {
          type: "https://checkrd.io/errors/validation_failed",
          title: "Unprocessable Entity",
          status: 422,
          detail: "Request body failed validation.",
          code: "validation_failed",
          errors: [
            { pointer: "/email", detail: "must be a valid email" },
            { pointer: "/name", detail: "is required" },
          ],
        },
      },
    ]);
    const client = new Checkrd({
      apiKey: "ck_test_x",
      baseURL: "https://api.example.test",
      maxRetries: 0,
      fetch: fetchImpl,
    });
    try {
      await client.agents.retrieve("abc");
      throw new Error("expected throw");
    } catch (err) {
      expect(err).toBeInstanceOf(UnprocessableEntityError);
      const e = err as APIStatusError;
      expect(e.code).toBe("validation_failed");
      expect(e.errors).toHaveLength(2);
      expect(e.errors?.[0]).toEqual({ pointer: "/email", detail: "must be a valid email" });
      // `.param` is the flat replacement for the removed Stripe `param`.
      expect(e.param).toBe("/email");
    }
  });

  it("degrades gracefully on a non-JSON / empty error body (e.g. LB 502)", async () => {
    // A bare gateway 502 with an HTML/text body and no JSON.
    const fetchImpl: typeof fetch = vi.fn(
      async () =>
        new Response("<html><body>502 Bad Gateway</body></html>", {
          status: 502,
          headers: { "content-type": "text/html" },
        }),
    ) as unknown as typeof fetch;
    const client = new Checkrd({
      apiKey: "ck_test_x",
      baseURL: "https://api.example.test",
      maxRetries: 0,
      fetch: fetchImpl,
    });
    try {
      await client.agents.retrieve("abc");
      throw new Error("expected throw");
    } catch (err) {
      // Still a typed status error — never throws while building it.
      expect(err).toBeInstanceOf(InternalServerError);
      const e = err as APIStatusError;
      expect(e.status).toBe(502);
      expect(e.code).toBeUndefined();
      expect(e.body).toBeUndefined();
      expect(e.message).toBe("HTTP 502");
    }
  });

  it("surfaces parsed Retry-After (seconds) on a 429", async () => {
    const fetchImpl = mockFetch([
      {
        status: 429,
        body: {
          title: "Too Many Requests",
          status: 429,
          detail: "rate limit exceeded",
          code: "rate_limit_exceeded",
        },
        headers: { "retry-after": "30" },
      },
    ]);
    const client = new Checkrd({
      apiKey: "ck_test_x",
      baseURL: "https://api.example.test",
      maxRetries: 0,
      fetch: fetchImpl,
    });
    try {
      await client.agents.retrieve("abc");
      throw new Error("expected throw");
    } catch (err) {
      expect(err).toBeInstanceOf(RateLimitError);
      expect((err as RateLimitError).retryAfterMs).toBe(30_000);
    }
  });

  it("requestId is read from body.request_id, falling back to the header", async () => {
    const fetchImpl = mockFetch([
      {
        status: 404,
        body: {
          type: "https://checkrd.io/errors/agent_not_found",
          title: "Not Found",
          status: 404,
          detail: "no such agent",
          code: "agent_not_found",
          request_id: "req_from_body",
        },
        headers: { "checkrd-request-id": "req_from_header" },
      },
    ]);
    const client = new Checkrd({
      apiKey: "ck_test_x",
      baseURL: "https://api.example.test",
      maxRetries: 0,
      fetch: fetchImpl,
    });
    try {
      await client.agents.retrieve("abc");
      throw new Error("expected throw");
    } catch (err) {
      expect(err).toBeInstanceOf(NotFoundError);
      // Body-carried request_id wins over the header.
      expect((err as NotFoundError).requestId).toBe("req_from_body");
    }
  });

  it("requestId falls back to the header when body omits request_id", async () => {
    const fetchImpl = mockFetch([
      {
        status: 404,
        body: {
          title: "Not Found",
          status: 404,
          detail: "no such agent",
          code: "agent_not_found",
        },
        headers: { "checkrd-request-id": "req_abc123" },
      },
    ]);
    const client = new Checkrd({
      apiKey: "ck_test_x",
      baseURL: "https://api.example.test",
      maxRetries: 0,
      fetch: fetchImpl,
    });
    try {
      await client.agents.retrieve("abc");
      throw new Error("expected throw");
    } catch (err) {
      expect(err).toBeInstanceOf(NotFoundError);
      expect((err as NotFoundError).requestId).toBe("req_abc123");
    }
  });
});

// ---------------------------------------------------------------------------
// Headers + auth injection
// ---------------------------------------------------------------------------

describe("headers", () => {
  it("injects X-API-Key when apiKey is set", async () => {
    const calls: Array<{ headers: Headers }> = [];
    const fetchImpl: typeof fetch = vi.fn(async (input, init) => {
      calls.push({ headers: new Headers(init?.headers) });
      return new Response('{"data":[],"has_more":false,"next_cursor":null}', {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }) as unknown as typeof fetch;
    const client = new Checkrd({
      apiKey: "ck_test_secret",
      baseURL: "https://api.example.test",
      fetch: fetchImpl,
    });
    await client.agents.list();
    expect(calls[0]!.headers.get("x-api-key")).toBe("ck_test_secret");
  });

  it("injects Authorization Bearer when bearerToken is set", async () => {
    const calls: Array<{ headers: Headers }> = [];
    const fetchImpl: typeof fetch = vi.fn(async (input, init) => {
      calls.push({ headers: new Headers(init?.headers) });
      return new Response('{"data":[],"has_more":false,"next_cursor":null}', {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }) as unknown as typeof fetch;
    const client = new Checkrd({
      bearerToken: "jwt_xyz",
      baseURL: "https://api.example.test",
      fetch: fetchImpl,
    });
    await client.agents.list();
    expect(calls[0]!.headers.get("authorization")).toBe("Bearer jwt_xyz");
  });

  it("pins Checkrd-Version header to the configured value", async () => {
    const calls: Array<{ headers: Headers }> = [];
    const fetchImpl: typeof fetch = vi.fn(async (input, init) => {
      calls.push({ headers: new Headers(init?.headers) });
      return new Response('{"data":[],"has_more":false,"next_cursor":null}', {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }) as unknown as typeof fetch;
    const client = new Checkrd({
      apiKey: "ck_test_x",
      baseURL: "https://api.example.test",
      fetch: fetchImpl,
    });
    await client.agents.list();
    expect(calls[0]!.headers.get("checkrd-version")).toBe(DEFAULT_API_VERSION);
  });
});

// ---------------------------------------------------------------------------
// Network error handling
// ---------------------------------------------------------------------------

describe("network errors", () => {
  it("wraps fetch failures as APIConnectionError", async () => {
    const fetchImpl: typeof fetch = vi.fn(async () => {
      throw new TypeError("network down");
    }) as unknown as typeof fetch;
    const client = new Checkrd({
      apiKey: "ck_test_x",
      baseURL: "https://api.example.test",
      maxRetries: 0,
      fetch: fetchImpl,
    });
    await expect(client.agents.retrieve("abc")).rejects.toBeInstanceOf(APIConnectionError);
  });
});
