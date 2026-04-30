/**
 * Raw-response wrappers for power-user access to control-plane responses.
 *
 * Mirrors the OpenAI / Anthropic ``withResponse`` /
 * ``withStreamingResponse`` pattern. Every future user-facing
 * control-plane method on the Checkrd class (e.g. ``alerts.create``,
 * ``policies.list``) MUST expose both variants so observability
 * tooling can read ``X-Request-Id``, rate-limit headers, raw bytes,
 * etc.
 *
 * Usage shape (planned)::
 *
 *   const response = await client.alerts.withResponse.create(...);
 *   // response is APIResponse<Alert>
 *   console.log(response.requestId);
 *   console.log(response.headers["x-rate-limit-remaining"]);
 *   const alert = await response.parse();
 *   const raw = await response.bytes();
 *
 * Streaming variant::
 *
 *   await using stream = await client.events.withStreamingResponse.list();
 *   for await (const chunk of stream.iterBytes()) { ... }
 *
 * This module currently defines the wrapper types only. The Checkrd
 * SDK does not yet expose any user-facing control-plane endpoints
 * that return parsed bodies — the helper methods (``wrap``,
 * ``instrument*``) are fire-and-forget. When the first such endpoint
 * is added it will go through these wrappers; defining them now
 * prevents the kind of "we'll add raw-response later" drift that
 * forces breaking changes in OSS SDKs.
 */

/**
 * Buffered raw response. ``parse()`` is async because the underlying
 * fetch ``Response.text()`` is async; callers can await it once and
 * reuse the resolved value.
 */
export class APIResponse<T> {
  /** The underlying fetch ``Response``. */
  readonly response: Response;
  /** HTTP status code. */
  readonly status: number;
  /** Lower-cased response headers as a plain object. */
  readonly headers: Record<string, string>;
  /** Server-issued request ID for support tickets. */
  readonly requestId: string | undefined;

  private parsedPromise: Promise<T> | null = null;
  private bytesPromise: Promise<Uint8Array> | null = null;
  private readonly parseFn: (bytes: Uint8Array) => T | Promise<T>;

  constructor(
    response: Response,
    parse: (bytes: Uint8Array) => T | Promise<T>,
  ) {
    this.response = response;
    this.status = response.status;
    this.headers = collectHeaders(response.headers);
    this.requestId =
      this.headers["checkrd-request-id"] ??
      this.headers["x-request-id"] ??
      undefined;
    this.parseFn = parse;
  }

  /** Read the raw body bytes (cached). */
  bytes(): Promise<Uint8Array> {
    this.bytesPromise ??= this.response
      .arrayBuffer()
      .then((buf) => new Uint8Array(buf));
    return this.bytesPromise;
  }

  /** Read the raw body as a UTF-8 string. */
  async text(): Promise<string> {
    const bytes = await this.bytes();
    return new TextDecoder("utf-8").decode(bytes);
  }

  /** Parse the body into the typed shape. Cached. */
  parse(): Promise<T> {
    this.parsedPromise ??= this.bytes().then((b) => this.parseFn(b));
    return this.parsedPromise;
  }
}

/**
 * Streaming raw response. The underlying body is NOT buffered — the
 * caller must consume the stream and close it (use ``await using`` or
 * call ``close()`` explicitly).
 *
 * Mirrors the shape of Anthropic's `Stream<Item>` (`src/core/streaming.ts`):
 *   - `consumed` guard prevents double-iteration of the same stream.
 *   - `tee()` splits the body into two independently readable cursors
 *     for replay / fanout — required when one consumer needs to log
 *     the raw bytes while another parses them.
 *   - `toReadableStream()` exposes the underlying web `ReadableStream`
 *     for handoff to bundler-native sinks (`Response.body`,
 *     `WritableStream.pipeThrough`, etc.).
 */
export class StreamingAPIResponse<_T> implements AsyncDisposable {
  readonly response: Response;
  readonly status: number;
  readonly headers: Record<string, string>;
  readonly requestId: string | undefined;

  /**
   * Set to `true` after the first call to `iterBytes()` / `iterText()`.
   * A second call throws — there is only one underlying body cursor and
   * silently re-reading would yield zero chunks. Callers who need
   * fanout must call {@link tee} first.
   */
  private _consumed = false;

  constructor(response: Response) {
    this.response = response;
    this.status = response.status;
    this.headers = collectHeaders(response.headers);
    this.requestId =
      this.headers["checkrd-request-id"] ??
      this.headers["x-request-id"] ??
      undefined;
  }

  /** Whether the stream has been consumed (or `tee()`'d). */
  get consumed(): boolean {
    return this._consumed;
  }

  private markConsumed(): void {
    if (this._consumed) {
      throw new Error(
        "StreamingAPIResponse can only be consumed once. Call .tee() " +
          "before the first iteration to split into independent cursors.",
      );
    }
    this._consumed = true;
  }

  /** Yield raw response chunks. Throws if the stream was already consumed. */
  async *iterBytes(): AsyncIterable<Uint8Array> {
    this.markConsumed();
    const body = this.response.body;
    if (body === null) return;
    const reader = body.getReader();
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) return;
        // ReadableStreamDefaultReader narrows: when ``done`` is false,
        // ``value`` is always defined.
        yield value;
      }
    } finally {
      try {
        reader.releaseLock();
      } catch {
        // releaseLock can throw if the stream was cancelled — ignore.
      }
    }
  }

  /**
   * Yield decoded response chunks. Throws if the stream was already
   * consumed. Backed by a stateful `TextDecoder` so multi-byte UTF-8
   * sequences split across chunk boundaries decode correctly.
   */
  async *iterText(): AsyncIterable<string> {
    const decoder = new TextDecoder("utf-8");
    // `iterBytes` calls markConsumed for us — don't double-mark.
    for await (const chunk of this.iterBytes()) {
      yield decoder.decode(chunk, { stream: true });
    }
    // Flush trailing multi-byte sequences.
    yield decoder.decode();
  }

  /**
   * Split the stream into two independently consumable cursors.
   * After calling, the original stream is marked consumed; callers
   * must use the returned cursors. Mirrors Anthropic's
   * `Stream.tee()` and the standard `ReadableStream.tee()`.
   *
   * Memory: until both cursors drain, the runtime buffers any chunks
   * that were read by one cursor but not yet by the other. Long
   * divergence between cursors WILL grow memory.
   */
  tee(): [StreamingAPIResponse<_T>, StreamingAPIResponse<_T>] {
    this.markConsumed();
    const body = this.response.body;
    if (body === null) {
      // Empty body — both forks behave like an empty stream.
      const empty = (): StreamingAPIResponse<_T> => {
        const r = new Response(null, {
          status: this.status,
          headers: this.response.headers,
        });
        return new StreamingAPIResponse<_T>(r);
      };
      return [empty(), empty()];
    }
    const [a, b] = body.tee();
    const make = (stream: ReadableStream<Uint8Array>): StreamingAPIResponse<_T> =>
      new StreamingAPIResponse<_T>(
        new Response(stream, {
          status: this.status,
          headers: this.response.headers,
        }),
      );
    return [make(a), make(b)];
  }

  /**
   * Hand off the underlying body as a web `ReadableStream`. Marks
   * the stream consumed because the returned stream owns the cursor.
   * Returns `null` for bodyless responses (HEAD, 204, etc.).
   */
  toReadableStream(): ReadableStream<Uint8Array> | null {
    this.markConsumed();
    return this.response.body;
  }

  /** Close the underlying stream. Idempotent. Safe to call after consumption. */
  async close(): Promise<void> {
    try {
      await this.response.body?.cancel();
    } catch {
      // Stream may already be closed.
    }
  }

  /** Async-disposable hook for ``await using`` syntax (TS 5.2+). */
  async [Symbol.asyncDispose](): Promise<void> {
    await this.close();
  }
}

function collectHeaders(headers: Headers): Record<string, string> {
  const out: Record<string, string> = {};
  headers.forEach((value, key) => {
    out[key.toLowerCase()] = value;
  });
  return out;
}
