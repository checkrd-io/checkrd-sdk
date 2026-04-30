/**
 * Streaming-response regression tests for `wrapFetch`.
 *
 * Mirrors the Python SDK's `test_streaming_transport.py`. The #1
 * use case for the JS SDK is wrapping `fetch` for the OpenAI /
 * Anthropic / AI SDK path, all of which ship token-by-token SSE
 * responses. Any bug that buffers the whole stream, drops bytes, or
 * breaks the `ReadableStream` contract makes the SDK unusable for
 * the primary use case.
 *
 * Covers the Week-3 audit gap: "tests don't cover streaming
 * responses — OpenAI streaming, event streams — all untested."
 *
 * The tests use plain `Response` objects with a streaming
 * `ReadableStream` body so we can verify byte-fidelity without an
 * actual network and without spinning up an AI vendor SDK.
 */
import { describe, expect, it } from "vitest";

import { CheckrdPolicyDenied, wrap } from "../src/index.js";

const ALLOW_ALL = { agent: "test", default: "allow", rules: [] };
const DENY_ALL = { agent: "test", default: "deny", rules: [] };

function sseChunks(deltas: string[]): Uint8Array[] {
  const encoder = new TextEncoder();
  return [
    ...deltas.map((d, i) =>
      encoder.encode(
        `data: ${JSON.stringify({
          id: `chatcmpl-${String(i)}`,
          choices: [{ delta: { content: d }, index: 0 }],
        })}\n\n`,
      ),
    ),
    encoder.encode("data: [DONE]\n\n"),
  ];
}

/**
 * Build a `Response` whose body is the chunks as a `ReadableStream`.
 * Mirrors how `fetch` delivers an SSE response — one chunk per
 * `reader.read()` call until the stream closes.
 */
function streamingResponse(chunks: Uint8Array[]): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk);
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

/**
 * Drain an async-iterable `ReadableStream` into a single Uint8Array.
 * Used to verify byte-fidelity: any missing / reordered byte trips
 * the assertion.
 */
async function drainStream(body: ReadableStream<Uint8Array>): Promise<Uint8Array> {
  const reader = body.getReader();
  const chunks: Uint8Array[] = [];
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    if (value) chunks.push(value);
  }
  const total = chunks.reduce((n, c) => n + c.byteLength, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const c of chunks) {
    out.set(c, offset);
    offset += c.byteLength;
  }
  return out;
}

describe("wrapFetch streaming", () => {
  it("passes SSE bytes through unchanged", async () => {
    // Most fundamental contract: if we mangle a single byte, every
    // streaming LLM call corrupts. Assert wire bytes, not parsed
    // content, so any encoding-mangling bug gets caught.
    const chunks = sseChunks(["Hello", " world", "!"]);
    const base = async () => streamingResponse(chunks);
    const wrapped = wrap(base as unknown as typeof fetch, {
      agentId: "t",
      policy: ALLOW_ALL,
    });

    const response = await wrapped("https://api.openai.com/v1/chat");
    expect(response.status).toBe(200);
    expect(response.body).not.toBeNull();

    const received = await drainStream(response.body!);
    const expected = new Uint8Array(
      chunks.reduce((n, c) => n + c.byteLength, 0),
    );
    let off = 0;
    for (const c of chunks) {
      expected.set(c, off);
      off += c.byteLength;
    }
    expect(received).toEqual(expected);
  });

  it("reader.read() yields one chunk at a time (not buffered)", async () => {
    // The whole point of SSE is token-by-token delivery. A bug that
    // buffers the full response before handing it to the caller
    // defeats streaming. We assert we see >1 read() call.
    const chunks = sseChunks(
      Array.from({ length: 5 }, (_, i) => `tok-${String(i)}`),
    );
    const base = async () => streamingResponse(chunks);
    const wrapped = wrap(base as unknown as typeof fetch, {
      agentId: "t",
      policy: ALLOW_ALL,
    });

    const response = await wrapped("https://api.openai.com/v1/chat");
    const reader = response.body!.getReader();
    let readCount = 0;
    for (;;) {
      const { done } = await reader.read();
      if (done) break;
      readCount++;
    }
    // 5 delta chunks + 1 [DONE] chunk = 6. Allow >= 2 as the
    // minimum viable streaming shape in case the underlying
    // ReadableStream implementation coalesces some chunks.
    expect(readCount).toBeGreaterThanOrEqual(2);
  });

  it("can be closed early via reader.cancel() without leaking", async () => {
    // Callers abort streams on cancellation / timeout. The wrapped
    // fetch must propagate cancel() cleanly so the underlying HTTP
    // connection is released.
    const chunks = sseChunks(
      Array.from({ length: 50 }, (_, i) => `tok-${String(i)}`),
    );
    const base = async () => streamingResponse(chunks);
    const wrapped = wrap(base as unknown as typeof fetch, {
      agentId: "t",
      policy: ALLOW_ALL,
    });

    const response = await wrapped("https://api.openai.com/v1/chat");
    const reader = response.body!.getReader();
    // Consume one chunk, then cancel.
    await reader.read();
    await reader.cancel();
    // No assertion beyond "no exception thrown" — a broken
    // cancel path would leak the stream or throw `TypeError:
    // already locked`.
  });

  it("denied streams raise CheckrdPolicyDenied BEFORE upstream fetch", async () => {
    // Policy enforcement must happen BEFORE the vendor API call is
    // made. A regression that lets the fetch go through and only
    // denies on the response would have already billed the customer.
    let fetchCalled = false;
    const base = async () => {
      fetchCalled = true;
      return streamingResponse(sseChunks(["should never be seen"]));
    };
    const wrapped = wrap(base as unknown as typeof fetch, {
      agentId: "t",
      policy: DENY_ALL,
      enforce: true,
    });

    await expect(wrapped("https://api.openai.com/v1/chat")).rejects.toBeInstanceOf(
      CheckrdPolicyDenied,
    );
    expect(fetchCalled).toBe(false);
  });

  it("preserves the content-type header so downstream parsers recognize SSE", async () => {
    // OpenAI / Anthropic SDKs inspect `Content-Type: text/event-stream`
    // to decide whether to parse the body as SSE. If the wrapped
    // transport strips or rewrites the header, the vendor SDK would
    // silently fall back to JSON-parsing the stream (and fail).
    const chunks = sseChunks(["a"]);
    const base = async () => streamingResponse(chunks);
    const wrapped = wrap(base as unknown as typeof fetch, {
      agentId: "t",
      policy: ALLOW_ALL,
    });

    const response = await wrapped("https://api.openai.com/v1/chat");
    expect(response.headers.get("content-type")).toBe("text/event-stream");
  });
});
