/**
 * Tests for cost-metering wiring in the telemetry batcher (M-12, step 5).
 *
 * When `costMetering` is ON and a signed pricing bundle is installed
 * (`engine.getActivePricingVersion() > 0`), the batcher settles each
 * event's token usage in-WASM and stamps the cost fields onto the wire
 * event. When OFF, or when no bundle is installed, the settle path is inert
 * and the cost fields are unset.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { TelemetryBatcher, buildUsageInput } from "../src/batcher.js";
import type { TelemetryEvent } from "../src/batcher.js";
import type { SettleResult, UsageInput, WasmEngine } from "../src/engine.js";

/** A signing+settling fake engine. `pricingVersion` gates the settle path. */
function fakeEngine(opts: {
  pricingVersion: number;
  settle?: (requestId: string, usage: UsageInput) => SettleResult;
  settleSpy?: ReturnType<typeof vi.fn>;
}): WasmEngine {
  const settleImpl =
    opts.settle ??
    ((_id: string, _usage: UsageInput): SettleResult => ({
      cost_usd_micros: 10_500,
      currency: "USD",
      pricing_bundle_version: opts.pricingVersion,
      pricing_status: "priced",
      overflow: false,
      sku_id: "anthropic-claude-sonnet",
    }));
  return {
    signTelemetryBatch: vi.fn().mockReturnValue({
      content_digest: "sha-256=:abc=:",
      signature_input: 'sig=("@target-uri")',
      signature: "sig=:zzz=:",
      dsse_envelope: "{}",
      instance_id: "0123456789abcdef",
      expires: Math.floor(Date.now() / 1000) + 300,
    }),
    getActivePricingVersion: vi.fn().mockReturnValue(opts.pricingVersion),
    settleUsage: opts.settleSpy ?? vi.fn(settleImpl),
  } as unknown as WasmEngine;
}

/** Drain the body of the single POST the batcher's flush() issued. */
async function flushAndReadEvent(
  batcher: TelemetryBatcher,
  fetchMock: ReturnType<typeof vi.fn>,
): Promise<Record<string, unknown>> {
  await batcher.flush();
  expect(fetchMock).toHaveBeenCalledTimes(1);
  const init = fetchMock.mock.calls[0]![1] as RequestInit;
  const body = JSON.parse(init.body as string) as {
    events: Record<string, unknown>[];
  };
  return body.events[0]!;
}

const GENAI_EVENT: TelemetryEvent = {
  event_id: "req-1",
  agent_id: "a",
  url_host: "api.anthropic.com",
  url_path: "/v1/messages",
  method: "POST",
  status_code: 200,
  "gen_ai.provider.name": "anthropic",
  "gen_ai.response.model": "claude-sonnet-4",
  "gen_ai.usage.input_tokens": 1000,
  "gen_ai.usage.output_tokens": 500,
};

let active: TelemetryBatcher | undefined;
afterEach(async () => {
  if (active) {
    await active.stop();
    active = undefined;
  }
});

describe("cost metering — default OFF", () => {
  it("does not settle and leaves cost fields unset when costMetering is unset", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(null, { status: 204 }));
    const engine = fakeEngine({ pricingVersion: 7 });
    const batcher = new TelemetryBatcher({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_test",
      agentId: "a",
      engine,
      fetch: fetchMock as unknown as typeof fetch,
      // costMetering omitted → default false
    });
    active = batcher;
    batcher.enqueue({ ...GENAI_EVENT });
    const event = await flushAndReadEvent(batcher, fetchMock);

    expect(engine.settleUsage).not.toHaveBeenCalled();
    expect(event.cost_usd_micros).toBeUndefined();
    expect(event.currency).toBeUndefined();
    expect(event.pricing_bundle_version).toBeUndefined();
    expect(event.pricing_status).toBeUndefined();
  });

  it("does not even probe the engine's pricing version when off", () => {
    const engine = fakeEngine({ pricingVersion: 7 });
    const batcher = new TelemetryBatcher({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_test",
      agentId: "a",
      engine,
      fetch: vi.fn().mockResolvedValue(new Response(null, { status: 204 })) as unknown as typeof fetch,
    });
    active = batcher;
    batcher.enqueue({ ...GENAI_EVENT });
    expect(engine.getActivePricingVersion).not.toHaveBeenCalled();
  });
});

describe("cost metering — ON with a pricing bundle", () => {
  it("settles and stamps the cost fields on the wire event", async () => {
    const settleSpy = vi.fn(
      (_id: string, _u: UsageInput): SettleResult => ({
        cost_usd_micros: 10_500,
        currency: "USD",
        pricing_bundle_version: 7,
        pricing_status: "priced",
        overflow: false,
        sku_id: "anthropic-claude-sonnet",
      }),
    );
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(null, { status: 204 }));
    const engine = fakeEngine({ pricingVersion: 7, settleSpy });
    const batcher = new TelemetryBatcher({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_test",
      agentId: "a",
      engine,
      fetch: fetchMock as unknown as typeof fetch,
      costMetering: true,
    });
    active = batcher;
    batcher.enqueue({ ...GENAI_EVENT });
    const event = await flushAndReadEvent(batcher, fetchMock);

    expect(settleSpy).toHaveBeenCalledTimes(1);
    // requestId is sourced from event_id, usage from the gen_ai attrs.
    expect(settleSpy.mock.calls[0]![0]).toBe("req-1");
    expect(settleSpy.mock.calls[0]![1]).toMatchObject({
      provider: "anthropic",
      model: "claude-sonnet-4",
      input_tokens: 1000,
      output_tokens: 500,
    });
    expect(event.cost_usd_micros).toBe(10_500);
    expect(event.currency).toBe("USD");
    expect(event.pricing_bundle_version).toBe(7);
    expect(event.pricing_status).toBe("priced");
  });

  it("is inert when ON but no bundle is installed (version 0)", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(null, { status: 204 }));
    const engine = fakeEngine({ pricingVersion: 0 });
    const batcher = new TelemetryBatcher({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_test",
      agentId: "a",
      engine,
      fetch: fetchMock as unknown as typeof fetch,
      costMetering: true,
    });
    active = batcher;
    batcher.enqueue({ ...GENAI_EVENT });
    const event = await flushAndReadEvent(batcher, fetchMock);

    // The cheap version gate short-circuits before settle.
    expect(engine.getActivePricingVersion).toHaveBeenCalled();
    expect(engine.settleUsage).not.toHaveBeenCalled();
    expect(event.cost_usd_micros).toBeUndefined();
    expect(event.pricing_status).toBeUndefined();
  });

  it("skips settle for events with no token usage", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(null, { status: 204 }));
    const engine = fakeEngine({ pricingVersion: 7 });
    const batcher = new TelemetryBatcher({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_test",
      agentId: "a",
      engine,
      fetch: fetchMock as unknown as typeof fetch,
      costMetering: true,
    });
    active = batcher;
    // A plain policy-decision event with no gen_ai usage.
    batcher.enqueue({
      event_id: "r-2",
      url_host: "api.stripe.com",
      url_path: "/v1/charges",
      method: "POST",
      status_code: 200,
    });
    const event = await flushAndReadEvent(batcher, fetchMock);
    expect(engine.settleUsage).not.toHaveBeenCalled();
    expect(event.cost_usd_micros).toBeUndefined();
  });

  it("never breaks telemetry delivery if settle throws (fail-soft)", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(null, { status: 204 }));
    const engine = fakeEngine({
      pricingVersion: 7,
      settleSpy: vi.fn(() => {
        throw new Error("boom");
      }),
    });
    const batcher = new TelemetryBatcher({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_test",
      agentId: "a",
      engine,
      fetch: fetchMock as unknown as typeof fetch,
      costMetering: true,
    });
    active = batcher;
    batcher.enqueue({ ...GENAI_EVENT });
    // The event still ships; the cost fields are just absent.
    const event = await flushAndReadEvent(batcher, fetchMock);
    expect(event.cost_usd_micros).toBeUndefined();
    expect(event.url_host).toBe("api.anthropic.com");
  });
});

describe("buildUsageInput (pure)", () => {
  it("maps dotted OTel keys to the WASM UsageInput shape", () => {
    const usage = buildUsageInput({
      "gen_ai.provider.name": "anthropic",
      "gen_ai.response.model": "claude-sonnet-4",
      "gen_ai.usage.input_tokens": 1000,
      "gen_ai.usage.output_tokens": 500,
      "gen_ai.usage.cache_read.input_tokens": 200,
      "gen_ai.usage.cache_creation.input_tokens": 50,
      "gen_ai.usage.reasoning.output_tokens": 30,
    });
    expect(usage).toEqual({
      provider: "anthropic",
      model: "claude-sonnet-4",
      input_tokens: 1000,
      output_tokens: 500,
      cache_read_tokens: 200,
      cache_creation_tokens: 50,
      reasoning_tokens: 30,
    });
  });

  it("falls back to flat wire-schema keys (framework adapters)", () => {
    const usage = buildUsageInput({
      gen_ai_system: "openai",
      gen_ai_model: "gpt-4o",
      gen_ai_input_tokens: 100,
      gen_ai_output_tokens: 40,
    });
    expect(usage).toMatchObject({
      provider: "openai",
      model: "gpt-4o",
      input_tokens: 100,
      output_tokens: 40,
    });
  });

  it("returns null when the event has no token usage", () => {
    expect(buildUsageInput({ url_host: "api.stripe.com" })).toBeNull();
  });

  it("settles a zero-token call (0 is distinct from absent)", () => {
    const usage = buildUsageInput({
      "gen_ai.usage.input_tokens": 0,
      "gen_ai.usage.output_tokens": 0,
    });
    expect(usage).toEqual({ input_tokens: 0, output_tokens: 0 });
  });

  it("prefers the response model over the request model", () => {
    const usage = buildUsageInput({
      "gen_ai.request.model": "claude-sonnet-4-requested",
      "gen_ai.response.model": "claude-sonnet-4-served",
      "gen_ai.usage.input_tokens": 1,
    });
    expect(usage?.model).toBe("claude-sonnet-4-served");
  });
});
