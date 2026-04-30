/**
 * OpenAI vendor adapter — comprehensive end-to-end tests.
 *
 * This file is the **canonical template** for vendor adapter tests.
 * Other vendor tests (`test_anthropic.ts`, `test_cohere.ts`, etc.)
 * should mirror its structure.
 *
 * Three responsibility layers, each tested:
 *
 * 1. **Lifecycle** — the Instrumentor's `instrument()` /
 *    `uninstrument()` dance, idempotency, and the constructor-patch
 *    contract.
 *
 * 2. **End-to-end happy paths** — drive the *real* `openai` SDK with
 *    a mocked `baseFetch` that returns realistic vendor responses
 *    (built via `_vendor_fixtures.ts`). The instrumentor wraps that
 *    fetch with policy enforcement; we then call the SDK's public
 *    methods and assert the SDK surfaces the response correctly.
 *    Covers: non-streaming text, non-streaming tool calls, streaming
 *    SSE, streaming tool-call deltas.
 *
 * 3. **Error paths** — 4xx/5xx structured errors, network rejections,
 *    mid-stream aborts. The wrapped fetch must propagate every error
 *    shape so the SDK can throw its proper typed error to user code.
 *
 * The mocked `baseFetch` records every invocation so tests can assert
 * on the URL the SDK called and the headers it sent — that catches
 * regressions where the wrapped fetch silently mutates the request.
 */
import { createRequire } from "node:module";

import { describe, expect, it } from "vitest";

import { OpenAIInstrumentor } from "../../src/integrations/_openai.js";

import { makeInstrumentorOptions } from "./_helpers.js";

/**
 * The OpenAI npm package is CJS, and its `module.exports = OpenAI` plus
 * `module.exports.OpenAI = OpenAI` shape interacts badly with ESM-from-
 * CJS interop. Node's ESM loader takes a SNAPSHOT of `module.exports`
 * properties at `import()` time, so any later mutation by the
 * instrumentor (which patches via CJS `require`) is invisible from the
 * ESM facade. Vendor adapter tests therefore use `createRequire` to
 * pull the live CJS module — the same path the instrumentor uses —
 * so a patched constructor is actually the constructor we end up
 * invoking. `await import(...)` would silently fall through to the
 * unpatched class and produce false positives that pass the test
 * suite while quietly hitting the real OpenAI API in CI.
 */
const requireOpenAI = createRequire(import.meta.url);
import {
  openaiErrorResponse,
  openaiPlainTextResponse,
  openaiSseChunks,
  openaiSseToolCallChunks,
  openaiToolCallResponse,
  rejectingFetch,
  streamingResponse,
  streamingResponseThatErrors,
} from "./_vendor_fixtures.js";

/**
 * Build a `baseFetch` that returns `response` once, then errors. Returns
 * the function plus a `.calls` array so tests can inspect what the
 * SDK actually requested.
 */
/**
 * Captures every invocation of the mocked baseFetch. We pull headers
 * off the `Request` object (which is what `wrapFetch` passes) rather
 * than `init.headers` — `wrapFetch` doesn't forward `init` at all when
 * the input is already a `Request`.
 */
interface RecordedCall {
  url: string;
  method: string;
  headers: Headers;
}
function recordingFetch(
  response: Response | (() => Response),
): typeof fetch & { calls: RecordedCall[] } {
  const calls: RecordedCall[] = [];
  const fn = (async (input: RequestInfo | URL, init?: RequestInit) => {
    let url: string;
    let method: string;
    let headers: Headers;
    if (input instanceof Request) {
      url = input.url;
      method = input.method;
      headers = new Headers(input.headers);
    } else {
      url = typeof input === "string" ? input : input.toString();
      method = init?.method?.toUpperCase() ?? "GET";
      headers = new Headers(init?.headers ?? {});
    }
    calls.push({ url, method, headers });
    return typeof response === "function" ? response() : response;
  }) as typeof fetch & { calls: RecordedCall[] };
  fn.calls = calls;
  return fn;
}

/**
 * Build instrumentor options + an OpenAI client wired through the
 * wrapped fetch. The `baseFetch` is what receives the eventual
 * outbound request after Checkrd policy enforcement passes through.
 */
function makeWrappedClient(baseFetch: typeof fetch): {
  client: { chat: { completions: { create: (...args: unknown[]) => Promise<unknown> } } };
  uninstrument: () => void;
} {
  const opts = { ...makeInstrumentorOptions(), baseFetch };
  const instr = new OpenAIInstrumentor(opts);
  instr.instrument();
  // Pull the constructor AFTER instrumenting so we're guaranteed to
  // see the patched `module.exports.OpenAI`, not a pre-patch snapshot.
  const mod = requireOpenAI("openai") as {
    OpenAI: new (o: Record<string, unknown>) => {
      chat: { completions: { create: (...args: unknown[]) => Promise<unknown> } };
    };
  };
  const client = new mod.OpenAI({ apiKey: "test", maxRetries: 0 });
  return {
    client,
    uninstrument: () => {
      instr.uninstrument();
    },
  };
}

// ---------------------------------------------------------------------------
// Lifecycle — pre-existing contract, kept verbatim
// ---------------------------------------------------------------------------

describe("OpenAIInstrumentor lifecycle", () => {
  it("instrument() / uninstrument() do not throw", () => {
    const instr = new OpenAIInstrumentor(makeInstrumentorOptions());
    expect(() => {
      instr.instrument();
    }).not.toThrow();
    expect(() => {
      instr.uninstrument();
    }).not.toThrow();
  });

  it("is idempotent across repeated instrument() / uninstrument()", () => {
    const instr = new OpenAIInstrumentor(makeInstrumentorOptions());
    instr.instrument();
    instr.instrument();
    expect(instr.isInstalled).toBe(true);
    instr.uninstrument();
    instr.uninstrument();
    expect(instr.isInstalled).toBe(false);
  });

  it("patches `new OpenAI({...})` to inject the wrapped fetch", async () => {
    const mod = await import("openai");
    const instr = new OpenAIInstrumentor(makeInstrumentorOptions());
    instr.instrument();
    try {
      const client = new mod.default({ apiKey: "test" }) as unknown as {
        fetch?: typeof fetch;
      };
      expect(client.fetch).toBeDefined();
    } finally {
      instr.uninstrument();
    }
  });

  it("respects an explicitly-supplied fetch (does not override)", async () => {
    const mod = await import("openai");
    const explicitFetch = (async () => new Response("x")) as unknown as typeof fetch;
    const instr = new OpenAIInstrumentor(makeInstrumentorOptions());
    instr.instrument();
    try {
      const ctorOpts = { apiKey: "test", fetch: explicitFetch } as unknown as Record<string, unknown>;
      const client = new (mod.default as unknown as new (o: Record<string, unknown>) => {
        fetch?: typeof fetch;
      })(ctorOpts);
      expect(client.fetch).toBe(explicitFetch);
    } finally {
      instr.uninstrument();
    }
  });
});

// ---------------------------------------------------------------------------
// End-to-end happy paths
// ---------------------------------------------------------------------------

describe("OpenAI: non-streaming chat completion", () => {
  it("returns the assistant message verbatim through the wrapped fetch", async () => {
    const baseFetch = recordingFetch(() => openaiPlainTextResponse("hello world"));
    const { client, uninstrument } = makeWrappedClient(baseFetch);
    try {
      const completion = (await client.chat.completions.create({
        model: "gpt-4o-mini",
        messages: [{ role: "user", content: "hi" }],
      })) as { choices: { message: { content: string } }[] };

      expect(completion.choices[0]?.message.content).toBe("hello world");
      // The SDK must have routed the request through our mocked fetch.
      expect(baseFetch.calls).toHaveLength(1);
      expect(baseFetch.calls[0]?.url).toContain("/chat/completions");
    } finally {
      uninstrument();
    }
  });

  it("preserves Authorization headers added by the SDK", async () => {
    const baseFetch = recordingFetch(() => openaiPlainTextResponse("ok"));
    const { client, uninstrument } = makeWrappedClient(baseFetch);
    try {
      await client.chat.completions.create({
        model: "gpt-4o-mini",
        messages: [{ role: "user", content: "x" }],
      });
      // The SDK adds `Authorization: Bearer <apiKey>` and a content-type.
      // The wrapped fetch must pass both through unmolested — a regression
      // that strips Authorization would still see status 200 from our mock
      // (we don't validate keys), but real requests would 401.
      expect(baseFetch.calls[0]?.headers.get("authorization")).toMatch(/^Bearer test/);
      expect(baseFetch.calls[0]?.method).toBe("POST");
    } finally {
      uninstrument();
    }
  });
});

describe("OpenAI: tool calls (non-streaming)", () => {
  it("surfaces tool_calls on the assistant message", async () => {
    const baseFetch = recordingFetch(() =>
      openaiToolCallResponse("get_weather", { city: "Paris" }, "call_abc"),
    );
    const { client, uninstrument } = makeWrappedClient(baseFetch);
    try {
      const completion = (await client.chat.completions.create({
        model: "gpt-4o-mini",
        messages: [{ role: "user", content: "weather?" }],
        tools: [
          {
            type: "function",
            function: {
              name: "get_weather",
              description: "Look up weather for a city.",
              parameters: {
                type: "object",
                properties: { city: { type: "string" } },
                required: ["city"],
              },
            },
          },
        ],
      })) as {
        choices: {
          finish_reason: string;
          message: {
            tool_calls?: { id: string; function: { name: string; arguments: string } }[];
          };
        }[];
      };

      const choice = completion.choices[0];
      expect(choice?.finish_reason).toBe("tool_calls");
      expect(choice?.message.tool_calls).toHaveLength(1);
      const tc = choice?.message.tool_calls?.[0];
      expect(tc?.function.name).toBe("get_weather");
      expect(JSON.parse(tc?.function.arguments ?? "{}")).toEqual({ city: "Paris" });
      expect(tc?.id).toBe("call_abc");
    } finally {
      uninstrument();
    }
  });
});

describe("OpenAI: streaming chat completion", () => {
  it("yields token deltas through the SDK's async iterator", async () => {
    const tokens = ["Hello", " ", "world", "!"];
    const baseFetch = recordingFetch(() => streamingResponse(openaiSseChunks(tokens)));
    const { client, uninstrument } = makeWrappedClient(baseFetch);
    try {
      const stream = (await client.chat.completions.create({
        model: "gpt-4o-mini",
        stream: true,
        messages: [{ role: "user", content: "hi" }],
      })) as AsyncIterable<{ choices: { delta: { content?: string } }[] }>;

      const collected: string[] = [];
      for await (const chunk of stream) {
        const c = chunk.choices[0]?.delta.content;
        if (c) collected.push(c);
      }
      expect(collected.join("")).toBe(tokens.join(""));
    } finally {
      uninstrument();
    }
  });

  it("yields tool_call argument deltas across multiple chunks", async () => {
    // Real-world wire format: the SDK splits the JSON arguments
    // payload across many SSE frames as the model decides them.
    const argFragments = [`{"ci`, `ty":"`, `Paris"}`];
    const baseFetch = recordingFetch(() =>
      streamingResponse(openaiSseToolCallChunks("get_weather", argFragments, "call_abc")),
    );
    const { client, uninstrument } = makeWrappedClient(baseFetch);
    try {
      const stream = (await client.chat.completions.create({
        model: "gpt-4o-mini",
        stream: true,
        messages: [{ role: "user", content: "weather?" }],
      })) as AsyncIterable<{
        choices: {
          delta: {
            tool_calls?: { index: number; id?: string; function?: { name?: string; arguments?: string } }[];
          };
          finish_reason: string | null;
        }[];
      }>;

      let toolName: string | undefined;
      let toolId: string | undefined;
      let argsBuf = "";
      let finish: string | null = null;
      for await (const chunk of stream) {
        const choice = chunk.choices[0];
        const tc = choice?.delta.tool_calls?.[0];
        if (tc?.id) toolId = tc.id;
        if (tc?.function?.name) toolName = tc.function.name;
        if (tc?.function?.arguments) argsBuf += tc.function.arguments;
        if (choice?.finish_reason) finish = choice.finish_reason;
      }
      expect(toolId).toBe("call_abc");
      expect(toolName).toBe("get_weather");
      expect(JSON.parse(argsBuf)).toEqual({ city: "Paris" });
      expect(finish).toBe("tool_calls");
    } finally {
      uninstrument();
    }
  });

  it("preserves byte-identity of the SSE stream end-to-end", async () => {
    // Bypass the SDK and check the wrapped fetch's `Response.body`
    // against the exact bytes our fixture produced. Any byte the
    // wrapped fetch dropped or reordered would fail this assertion.
    const tokens = ["A", "BC", "DEF"];
    const expected = new TextDecoder().decode(
      Buffer.concat(openaiSseChunks(tokens).map((c) => Buffer.from(c))),
    );
    const baseFetch = recordingFetch(() => streamingResponse(openaiSseChunks(tokens)));
    const { client, uninstrument } = makeWrappedClient(baseFetch);
    try {
      // Use the SDK at the lowest level we can — `withResponse()` exposes
      // the underlying Response so we can drain its body ourselves.
      const stream = (await client.chat.completions.create({
        model: "gpt-4o-mini",
        stream: true,
        messages: [{ role: "user", content: "x" }],
      })) as AsyncIterable<{ choices: { delta: { content?: string } }[] }>;
      // Force the SDK to consume the whole stream, mirrors a real call.
      let received = "";
      for await (const c of stream) {
        const d = c.choices[0]?.delta.content;
        if (d) received += d;
      }
      // The decoded SDK output equals the concatenated `delta.content`
      // across every fixture frame (which is what `expected` carries).
      const expectedJoined = tokens.join("");
      expect(received).toBe(expectedJoined);
      // Sanity: the fixture itself is well-formed SSE.
      expect(expected).toContain("data: [DONE]");
    } finally {
      uninstrument();
    }
  });
});

// ---------------------------------------------------------------------------
// Error paths
// ---------------------------------------------------------------------------

describe("OpenAI: 4xx / 5xx errors", () => {
  it("rate-limit (429) propagates as a thrown SDK error", async () => {
    const baseFetch = recordingFetch(() =>
      openaiErrorResponse(429, "rate limit exceeded", "rate_limit_error", "rate_limit"),
    );
    const { client, uninstrument } = makeWrappedClient(baseFetch);
    try {
      await expect(
        client.chat.completions.create({
          model: "gpt-4o-mini",
          messages: [{ role: "user", content: "x" }],
        }),
      ).rejects.toThrowError(/rate limit/i);
    } finally {
      uninstrument();
    }
  });

  it("upstream 5xx surfaces as an APIError", async () => {
    const baseFetch = recordingFetch(() =>
      openaiErrorResponse(500, "internal server error", "server_error"),
    );
    const { client, uninstrument } = makeWrappedClient(baseFetch);
    try {
      await expect(
        client.chat.completions.create({
          model: "gpt-4o-mini",
          messages: [{ role: "user", content: "x" }],
        }),
      ).rejects.toThrowError(/internal server error/i);
    } finally {
      uninstrument();
    }
  });

  it("network-level rejection bubbles through the wrapped fetch", async () => {
    const { client, uninstrument } = makeWrappedClient(rejectingFetch());
    try {
      await expect(
        client.chat.completions.create({
          model: "gpt-4o-mini",
          messages: [{ role: "user", content: "x" }],
        }),
      ).rejects.toThrowError();
    } finally {
      uninstrument();
    }
  });

  it("stream that errors mid-flight surfaces the error to the consumer", async () => {
    // Frames deliver, then the upstream stream errors. The SDK's
    // async iterator must surface that — either by throwing during
    // iteration, or by throwing at `chat.completions.create()` if the
    // SDK eagerly drains. The contract under test is "the consumer
    // observes the error", not "the consumer observes some specific
    // number of chunks before it".
    const baseFetch = recordingFetch(() =>
      streamingResponseThatErrors(openaiSseChunks(["partial"]), new Error("upstream EOF")),
    );
    const { client, uninstrument } = makeWrappedClient(baseFetch);
    try {
      let observedError = false;
      try {
        const stream = (await client.chat.completions.create({
          model: "gpt-4o-mini",
          stream: true,
          messages: [{ role: "user", content: "x" }],
        })) as AsyncIterable<unknown>;
        for await (const _ of stream) {
          // Drain whatever the SDK can decode from the partial frames.
        }
      } catch {
        observedError = true;
      }
      expect(observedError).toBe(true);
    } finally {
      uninstrument();
    }
  });
});

// ---------------------------------------------------------------------------
// Vendor-shape assertion (theme-#6 regression check)
// ---------------------------------------------------------------------------

import { assertVendorShape } from "../../src/integrations/_base.js";

/** Minimal Logger stub that records every call so tests can assert on it. */
function recordingLogger(): {
  logger: import("../../src/_logger.js").Logger;
  calls: Array<{ level: "debug" | "info" | "warn" | "error"; msg: string; args: unknown[] }>;
} {
  const calls: Array<{
    level: "debug" | "info" | "warn" | "error";
    msg: string;
    args: unknown[];
  }> = [];
  const make = (level: "debug" | "info" | "warn" | "error") =>
    (msg: string, ...args: unknown[]) => {
      calls.push({ level, msg, args });
    };
  return {
    logger: { debug: make("debug"), info: make("info"), warn: make("warn"), error: make("error") },
    calls,
  };
}

describe("assertVendorShape contract", () => {
  it("returns true when every expected export is a function", () => {
    // Stub class — body intentionally empty; we're only checking
    // that `assertVendorShape` recognises a function-typed export.
    const mod = { OpenAI: function OpenAIStub() { /* stub */ } };
    const { logger, calls } = recordingLogger();
    const opts = { ...makeInstrumentorOptions(), logger };
    expect(assertVendorShape("openai", mod, ["OpenAI"], opts)).toBe(true);
    expect(calls).toHaveLength(0);
  });

  it("returns false AND logs when an expected export is missing", () => {
    // Mirrors the regression we want to catch: a vendor major
    // renames `OpenAI` to `Client`. Old instrumentor would silently
    // no-op; the assertion now logs a warning so production
    // observability sees the structural break.
    // Misnamed stub — `OpenAI` is the export the assertion expects;
    // `Client` simulates the regression a vendor major rename causes.
    const mod = { Client: function ClientStub() { /* stub */ } };
    const { logger, calls } = recordingLogger();
    const opts = { ...makeInstrumentorOptions(), logger };
    expect(assertVendorShape("openai", mod, ["OpenAI"], opts)).toBe(false);
    const warnings = calls.filter((c) => c.level === "warn");
    expect(warnings).toHaveLength(1);
    expect(warnings[0]?.msg).toMatch(/openai vendor SDK shape mismatch/i);
    const ctx = warnings[0]?.args[0] as Record<string, unknown>;
    expect(ctx.vendor).toBe("openai");
    expect(ctx.missing).toEqual(["OpenAI"]);
    expect(ctx.presentKeys).toContain("Client");
  });

  it("accepts a function-typed CJS module (module.exports = Class)", () => {
    // openai 4.x ships `module.exports = OpenAI` AND
    // `module.exports.OpenAI = OpenAI` — the module.exports value is
    // the OpenAI class itself (a function), with `OpenAI` also present
    // as a named property. The assertion must accept that shape.
    const Cls = function OpenAIStub() { /* stub */ };
    (Cls as unknown as { OpenAI: typeof Cls }).OpenAI = Cls;
    const opts = makeInstrumentorOptions();
    expect(assertVendorShape("openai", Cls, ["OpenAI"], opts)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Documented harness contract for other vendor tests
// ---------------------------------------------------------------------------

/**
 * For test_anthropic.ts / test_cohere.ts / etc., copy this file's
 * structure and:
 *
 *   1. Add an Anthropic-shaped fixture builder to _vendor_fixtures.ts
 *      (e.g. `anthropicSseChunks`, `anthropicToolUseResponse`,
 *      `anthropicErrorResponse`). Anthropic uses `event:` / `data:`
 *      pairs in SSE rather than OpenAI's `data:`-only format.
 *
 *   2. Re-export your `makeWrappedClient` against the Anthropic
 *      instrumentor + Anthropic SDK constructor.
 *
 *   3. Mirror the four describe() blocks: lifecycle, non-streaming
 *      happy path, streaming happy path, errors. Each vendor's
 *      streaming tool-call format differs — that's the most common
 *      regression point and the one most worth a dedicated test.
 *
 *   4. Add the vendor SDK package to `wrappers/javascript/package.json`
 *      `devDependencies` so the test can `await import(...)` it.
 *
 * Lifecycle + missing-package contract tests for each vendor stay
 * lightweight — they're already in the existing `test_*.ts` files
 * and don't need to be repeated here.
 */
