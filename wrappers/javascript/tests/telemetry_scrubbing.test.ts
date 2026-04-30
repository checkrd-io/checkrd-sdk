/**
 * Sensitive-data scrubbing at the sink boundary.
 *
 * Every telemetry sink that ships events OUTSIDE the Checkrd control
 * plane (OtlpSink, ConsoleSink, JsonFileSink) must apply the same
 * scrubbing pass — auth headers, secret-named fields, and query-string
 * tokens must never reach a third-party collector or a shared log.
 *
 * The trusted ControlPlaneSink is deliberately NOT scrubbed: the
 * control plane is the intended recipient of the full payload and
 * signing happens over canonical bytes; inserting a redaction step
 * there would invalidate the signature.
 */
import { describe, expect, it, vi } from "vitest";

import { OtlpSink } from "../src/_otlp.js";
import {
  REDACTED,
  redactSensitive,
  scrubTelemetryEvent,
  scrubUrl,
} from "../src/_sensitive.js";
import { ConsoleSink } from "../src/sinks.js";

describe("scrubUrl", () => {
  it("leaves a URL with no query string unchanged", () => {
    const input = "https://api.example.com/v1/charges";
    expect(scrubUrl(input)).toBe(input);
  });

  it("leaves a URL with only non-sensitive query params unchanged", () => {
    const input = "https://api.example.com/v1/x?limit=10&offset=20";
    expect(scrubUrl(input)).toBe(input);
  });

  it("redacts a single sensitive query param", () => {
    const got = scrubUrl("https://api.example.com/v1/x?api_key=sk-live-abc");
    expect(got).toContain("api_key=");
    expect(got).not.toContain("sk-live-abc");
    expect(got).toContain(REDACTED);
  });

  it("redacts multiple sensitive params and leaves non-sensitive intact", () => {
    const got = scrubUrl(
      "https://api.example.com/v1/x?api_key=SECRET&limit=10&token=also-SECRET",
    );
    expect(got).not.toContain("SECRET");
    expect(got).not.toContain("also-SECRET");
    expect(got).toContain("limit=10");
  });

  it("is case-insensitive in query key matching", () => {
    // Real URL query strings from the wild use mixed case; redaction
    // must not be defeated by capitalization choices.
    const got = scrubUrl("https://api.example.com/?API_KEY=SECRET");
    expect(got).not.toContain("SECRET");
  });

  it("returns the input unchanged on unparseable URLs", () => {
    // Never throws on pathological input — sink-layer code runs on
    // every telemetry event and must be robust.
    const input = "not a url";
    expect(scrubUrl(input)).toBe(input);
  });

  it.each([
    "api_key",
    "apikey",
    "access_token",
    "signature",
    "token",
    "bearer",
    "password",
    "secret",
    "private_key",
    "sig",
    "auth",
    "authorization",
  ])("redacts %s as a query param", (paramName) => {
    const got = scrubUrl(`https://api.example.com/?${paramName}=VALUE`);
    expect(got).not.toContain("VALUE");
  });
});

describe("scrubTelemetryEvent", () => {
  it("redacts sensitive top-level keys", () => {
    const scrubbed = scrubTelemetryEvent({
      agent_id: "agent-1",
      api_key: "sk-live-abc",
      authorization: "Bearer xyz",
    });
    expect(scrubbed.agent_id).toBe("agent-1");
    expect(scrubbed.api_key).toBe(REDACTED);
    expect(scrubbed.authorization).toBe(REDACTED);
  });

  it("recursively redacts nested sensitive keys", () => {
    const scrubbed = scrubTelemetryEvent({
      custom_attrs: {
        user: "alice",
        password: "hunter2",
      },
    });
    const custom = scrubbed.custom_attrs as Record<string, unknown>;
    expect(custom.user).toBe("alice");
    expect(custom.password).toBe(REDACTED);
  });

  it("scrubs URL-bearing fields (including path-only url_path)", () => {
    const scrubbed = scrubTelemetryEvent({
      url: "https://api.example.com/v1/x?api_key=SK-LIVE-SECRET",
      url_full: "https://api.example.com/v1/x?token=TOKEN-SECRET",
      // Path-only form — what the telemetry pipeline stores in
      // `url_path` after host is split into its own field. `new URL()`
      // can't parse this without a base; scrubUrl must handle it.
      url_path: "/v1/x?signature=SIG-SECRET",
      method: "GET",
    });
    expect(String(scrubbed.url)).not.toContain("SK-LIVE-SECRET");
    expect(String(scrubbed.url_full)).not.toContain("TOKEN-SECRET");
    expect(String(scrubbed.url_path)).not.toContain("SIG-SECRET");
    expect(String(scrubbed.url_path)).toMatch(/^\/v1\/x\?/);
    expect(scrubbed.method).toBe("GET");
  });

  it("leaves well-formed non-sensitive events untouched", () => {
    // The typical flattened telemetry shape — must remain idempotent
    // across scrubbing so round-trip equality holds for dashboards
    // and storage systems that compare-for-dedupe.
    const input = {
      agent_id: "agent-1",
      request_id: "req-1",
      timestamp: "2026-04-24T00:00:00Z",
      method: "GET",
      url_host: "api.openai.com",
      url_path: "/v1/chat/completions",
      status_code: 200,
      latency_ms: 42.5,
      policy_result: "allowed",
    };
    const scrubbed = scrubTelemetryEvent(input);
    expect(scrubbed).toEqual(input);
  });

  it("does not mutate the input object", () => {
    // Callers may pass frozen / shared objects — scrubbing must be
    // referentially safe so the same event can be handed to multiple
    // sinks without side effects.
    const input = Object.freeze({
      agent_id: "agent-1",
      api_key: "leak-me",
    });
    const scrubbed = scrubTelemetryEvent(input);
    expect(input.api_key).toBe("leak-me");
    expect(scrubbed.api_key).toBe(REDACTED);
  });
});

describe("OtlpSink scrubbing", () => {
  it("scrubs the event at enqueue time", async () => {
    const fetchSpy = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(null, { status: 200 }),
    );
    const sink = new OtlpSink({
      endpoint: "https://otlp.example.com",
      fetch: fetchSpy,
      maxBatchSize: 1, // force immediate flush
    });
    sink.enqueue({
      request_id: "req-1",
      method: "GET",
      url_host: "api.openai.com",
      url_path: "/v1/x?api_key=SECRET",
      agent_id: "agent-1",
      api_key: "SHOULD-NOT-APPEAR",
    });
    // One batch, one flush. Wait for the in-flight fetch to resolve.
    await sink.flush();
    await sink.close();
    expect(fetchSpy).toHaveBeenCalled();
    const firstCall = fetchSpy.mock.calls[0];
    expect(firstCall).toBeDefined();
    const init = firstCall?.[1];
    const body = String(init?.body ?? "");
    expect(body).not.toContain("SHOULD-NOT-APPEAR");
    expect(body).not.toContain("SECRET");
  });
});

describe("ConsoleSink scrubbing", () => {
  it("scrubs the event before calling the logger", () => {
    const info = vi.fn();
    const sink = new ConsoleSink({
      logger: { debug: vi.fn(), info, warn: vi.fn(), error: vi.fn() },
    });
    sink.enqueue({
      request_id: "req-1",
      api_key: "LEAK",
      url: "https://api.example.com/?token=ALSO-LEAK",
    });
    expect(info).toHaveBeenCalledOnce();
    const firstCall = info.mock.calls[0];
    expect(firstCall).toBeDefined();
    const logged = firstCall?.[1] as Record<string, unknown>;
    expect(logged.api_key).toBe(REDACTED);
    expect(String(logged.url)).not.toContain("ALSO-LEAK");
  });
});

describe("redactSensitive (re-export compat)", () => {
  it("is the same function referenced from _logger.ts", async () => {
    // Sanity check: the move from `_logger.ts` to `_sensitive.ts` must
    // preserve the re-export path that existing callers depend on.
    const logger = await import("../src/_logger.js");
    expect(logger.redactSensitive).toBe(redactSensitive);
  });
});
