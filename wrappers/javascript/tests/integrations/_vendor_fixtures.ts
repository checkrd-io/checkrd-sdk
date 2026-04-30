/**
 * Shared vendor-test fixtures.
 *
 * The vendor instrumentor tests (`test_openai.ts`, `test_anthropic.ts`,
 * etc.) share a small surface area: they all need to construct
 * realistic vendor responses (streaming SSE, tool calls, error
 * payloads) and feed them to the wrapped `fetch` chain that the
 * Instrumentor injects into the SDK constructor. This file is the
 * single source of truth for those response builders so a regression
 * in one vendor's test exercises the same fixture a future Anthropic /
 * Cohere / Mistral test will use.
 *
 * Design:
 * - Every builder returns a `Response` (the standard Web type), not a
 *   vendor-specific wrapper. The OpenAI / Anthropic SDKs all consume
 *   `Response` from `fetch`, so a single builder works everywhere.
 * - Streaming uses a `ReadableStream<Uint8Array>` to mirror what real
 *   `fetch` produces. Vendor SDKs decode the SSE format themselves.
 * - Tool calls are *response shape* fixtures — they don't simulate
 *   the SDK's tool-execution loop, only that the wrapped fetch passes
 *   the structured response through unmodified.
 * - Errors are real `Response` objects with the right status + body.
 *   The wrapped fetch must not swallow them; the SDK will turn them
 *   into thrown errors via its own logic.
 *
 * If you add a new builder here, document it. Each builder must:
 *   - Be deterministic (no Date.now() in payloads).
 *   - Set `content-type` correctly (`text/event-stream` for streams,
 *     `application/json` for everything else).
 *   - Return a fresh `Response` per call (Response bodies are
 *     single-use).
 */

// ---------------------------------------------------------------------------
// JSON response (non-streaming)
// ---------------------------------------------------------------------------

/** Build a `Response` carrying a JSON body with the given status. */
export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

// ---------------------------------------------------------------------------
// Streaming SSE responses
// ---------------------------------------------------------------------------

/**
 * Build the OpenAI-style SSE chunks from a list of token deltas.
 * Each delta becomes one `data: {...}\n\n` frame plus a terminal
 * `data: [DONE]\n\n`. Mirrors the real chat.completions stream wire
 * format the OpenAI SDK consumes.
 */
export function openaiSseChunks(deltas: string[]): Uint8Array[] {
  const encoder = new TextEncoder();
  return [
    ...deltas.map((d, i) =>
      encoder.encode(
        `data: ${JSON.stringify({
          id: `chatcmpl-${String(i)}`,
          object: "chat.completion.chunk",
          created: 1700000000,
          model: "gpt-4o-mini",
          choices: [{ delta: { content: d }, index: 0, finish_reason: null }],
        })}\n\n`,
      ),
    ),
    encoder.encode("data: [DONE]\n\n"),
  ];
}

/**
 * Build OpenAI-style SSE chunks for a streaming tool-call response.
 * The first chunk emits the tool-call header (function name + id),
 * the subsequent chunks stream the JSON arguments delta-by-delta,
 * mirroring how the real API breaks `arguments` across frames.
 *
 * `argFragments` should reassemble into a valid JSON string when
 * concatenated — the SDK depends on this.
 */
export function openaiSseToolCallChunks(
  toolName: string,
  argFragments: string[],
  toolCallId = "call_abc123",
): Uint8Array[] {
  const encoder = new TextEncoder();
  const head = encoder.encode(
    `data: ${JSON.stringify({
      id: "chatcmpl-tc",
      object: "chat.completion.chunk",
      created: 1700000000,
      model: "gpt-4o-mini",
      choices: [
        {
          delta: {
            tool_calls: [
              {
                index: 0,
                id: toolCallId,
                type: "function",
                function: { name: toolName, arguments: "" },
              },
            ],
          },
          index: 0,
          finish_reason: null,
        },
      ],
    })}\n\n`,
  );
  const argChunks = argFragments.map((frag) =>
    encoder.encode(
      `data: ${JSON.stringify({
        id: "chatcmpl-tc",
        object: "chat.completion.chunk",
        created: 1700000000,
        model: "gpt-4o-mini",
        choices: [
          {
            delta: {
              tool_calls: [{ index: 0, function: { arguments: frag } }],
            },
            index: 0,
            finish_reason: null,
          },
        ],
      })}\n\n`,
    ),
  );
  const tail = encoder.encode(
    `data: ${JSON.stringify({
      id: "chatcmpl-tc",
      object: "chat.completion.chunk",
      created: 1700000000,
      model: "gpt-4o-mini",
      choices: [{ delta: {}, index: 0, finish_reason: "tool_calls" }],
    })}\n\ndata: [DONE]\n\n`,
  );
  return [head, ...argChunks, tail];
}

/** Wrap a list of pre-encoded chunks in an SSE-headed `Response`. */
export function streamingResponse(chunks: Uint8Array[]): Response {
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
 * Stream that injects an error mid-flight. The first `chunks.length`
 * frames deliver normally, then `controller.error()` aborts the
 * stream — mirroring a real network drop. Useful for verifying that
 * the wrapped fetch surfaces the abort to the SDK rather than silently
 * truncating the response.
 */
export function streamingResponseThatErrors(
  chunks: Uint8Array[],
  error = new Error("simulated upstream stream abort"),
): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk);
      controller.error(error);
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

// ---------------------------------------------------------------------------
// Tool-call (non-streaming) response fixture
// ---------------------------------------------------------------------------

/**
 * Build an OpenAI chat.completions.create non-streaming response that
 * carries a single `tool_calls` entry. The vendor SDK turns this
 * into `choice.message.tool_calls`; the wrapped fetch must pass it
 * through byte-identically.
 */
export function openaiToolCallResponse(
  toolName: string,
  toolArguments: Record<string, unknown>,
  toolCallId = "call_abc123",
): Response {
  return jsonResponse({
    id: "chatcmpl-tool",
    object: "chat.completion",
    created: 1700000000,
    model: "gpt-4o-mini",
    choices: [
      {
        index: 0,
        message: {
          role: "assistant",
          content: null,
          tool_calls: [
            {
              id: toolCallId,
              type: "function",
              function: {
                name: toolName,
                arguments: JSON.stringify(toolArguments),
              },
            },
          ],
        },
        finish_reason: "tool_calls",
      },
    ],
    usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 },
  });
}

/** Plain text completion response — the simplest happy-path fixture. */
export function openaiPlainTextResponse(content: string): Response {
  return jsonResponse({
    id: "chatcmpl-plain",
    object: "chat.completion",
    created: 1700000000,
    model: "gpt-4o-mini",
    choices: [
      {
        index: 0,
        message: { role: "assistant", content },
        finish_reason: "stop",
      },
    ],
    usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 },
  });
}

// ---------------------------------------------------------------------------
// Error responses
// ---------------------------------------------------------------------------

/** OpenAI-style structured error response (4xx / 5xx). */
export function openaiErrorResponse(
  status: number,
  message: string,
  type = "invalid_request_error",
  code: string | null = null,
): Response {
  return jsonResponse(
    {
      error: { message, type, param: null, code },
    },
    status,
  );
}

/**
 * A `fetch` implementation that rejects with a network-level error.
 * Use as `baseFetch` to verify the wrapped fetch propagates it as
 * a thrown error rather than swallowing it into a 5xx Response.
 */
export function rejectingFetch(error: Error = new TypeError("network error")): typeof fetch {
  return (async () => {
    throw error;
  }) as unknown as typeof fetch;
}

// ---------------------------------------------------------------------------
// Drain helper (used to verify byte fidelity of streamed bodies)
// ---------------------------------------------------------------------------

/** Concatenate every chunk a `ReadableStream` yields into one Uint8Array. */
export async function drainStream(body: ReadableStream<Uint8Array>): Promise<Uint8Array> {
  const reader = body.getReader();
  const parts: Uint8Array[] = [];
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    parts.push(value);
  }
  const total = parts.reduce((n, c) => n + c.byteLength, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const c of parts) {
    out.set(c, offset);
    offset += c.byteLength;
  }
  return out;
}

/** Decode a streamed body to UTF-8 text. */
export async function drainStreamText(body: ReadableStream<Uint8Array>): Promise<string> {
  return new TextDecoder().decode(await drainStream(body));
}
