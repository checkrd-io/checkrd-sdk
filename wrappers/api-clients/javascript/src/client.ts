/**
 * Checkrd Control Plane API client.
 *
 * Single :class:`Checkrd` class with field-attached resources
 * (``client.agents``, etc.). Every method returns a Promise; both
 * sync and async callers use ``await`` (or ``.then(...)``).
 * Mirrors the OpenAI / Anthropic / Stripe TS SDK shape.
 *
 * The class is *thin*. It owns:
 *
 * - The base URL, auth credentials, default timeout, default
 *   retry budget, ``Checkrd-Version`` pin.
 * - The retry loop with exponential backoff.
 * - A few low-level dispatch methods (``_get``, ``_post``, …)
 *   that resource classes call into.
 *
 * Resources are attached as fields so ``client.agents.list()``
 * autocompletes; lazy instantiation avoids the import cost when
 * the user only touches one resource.
 */
import {
  APIConnectionError,
  APITimeoutError,
  makeStatusError,
} from "./errors.js";
import { Page, PagePromise, type PaginatedBody } from "./pagination.js";
import { Agents } from "./resources/agents.js";

/** Default base URL for production. Override via ``baseURL`` or ``CHECKRD_BASE_URL``. */
export const DEFAULT_BASE_URL = "https://api.checkrd.io";

/** Default per-attempt timeout in milliseconds. Stripe / OpenAI use 60s; we match. */
export const DEFAULT_TIMEOUT_MS = 60_000;

/** Default retry budget. Mirrors OpenAI's 2 — enough for transient 5xx without doubling p99. */
export const DEFAULT_MAX_RETRIES = 2;

/** Pinned API version. Matches the ``Checkrd-Version`` header the version registry expects. */
export const DEFAULT_API_VERSION = "2026-04-15";

/** Per-call options passed as the second argument to every method. */
export interface RequestOptions {
  /** Per-call header overrides. Auth + version are injected separately and cannot be overridden. */
  headers?: Record<string, string>;
  /** Per-call timeout. Defaults to the client's ``timeout`` setting. */
  timeoutMs?: number;
  /** Per-call retry override. Defaults to the client's ``maxRetries``. */
  maxRetries?: number;
  /** AbortSignal — propagates user-side cancellation into the fetch. */
  signal?: AbortSignal;
}

/** Constructor options for :class:`Checkrd`. */
export interface CheckrdOptions {
  /** API key (``ck_live_…`` or ``ck_test_…``). Falls back to ``CHECKRD_API_KEY`` env. */
  apiKey?: string;
  /** Bearer JWT, alternative to ``apiKey`` for short-lived sessions. */
  bearerToken?: string;
  /** Override the base URL. Falls back to ``CHECKRD_BASE_URL`` env, then ``DEFAULT_BASE_URL``. */
  baseURL?: string;
  /** Default per-attempt timeout in milliseconds. */
  timeoutMs?: number;
  /** Default retry budget per call. */
  maxRetries?: number;
  /** Pinned API version date. */
  apiVersion?: string;
  /** Default header overrides applied to every request. */
  defaultHeaders?: Record<string, string>;
  /** Override the global ``fetch``. Useful for tests or custom transports. */
  fetch?: typeof fetch;
  /** SDK identifier appended to the User-Agent. Defaults to ``checkrd-api-js/<version>``. */
  userAgent?: string;
}

/** Read package version once at module load. Embedded into User-Agent. */
const SDK_VERSION = "0.1.0";

/**
 * Synchronous-feeling control-plane client. Every method returns a
 * Promise — same surface for sync (``await``) and async contexts.
 *
 * @example
 * ```ts
 * import { Checkrd } from "@checkrd/api";
 *
 * const client = new Checkrd({ apiKey: process.env.CHECKRD_API_KEY });
 * for await (const agent of client.agents.list()) {
 *   console.log(agent.name, agent.kill_switch_active);
 * }
 * ```
 */
export class Checkrd {
  readonly baseURL: string;
  readonly apiKey: string | undefined;
  readonly bearerToken: string | undefined;
  readonly timeoutMs: number;
  readonly maxRetries: number;
  readonly apiVersion: string;
  readonly defaultHeaders: Record<string, string>;
  readonly userAgent: string;

  private readonly fetchImpl: typeof fetch;

  /** Lazy resource accessors. Field syntax = autocomplete-friendly; the
   *  module-level ``import`` is cheap because resources don't run any
   *  setup until the first method call. */
  readonly agents: Agents;

  constructor(opts: CheckrdOptions = {}) {
    // Avoid hard dependency on @types/node — read env via a structural
    // typed helper so the package still type-checks on edge runtimes
    // (Cloudflare Workers, Vercel Edge) where ``process`` is absent.
    const env = readEnv();
    this.apiKey = opts.apiKey ?? env("CHECKRD_API_KEY");
    this.bearerToken = opts.bearerToken ?? env("CHECKRD_BEARER_TOKEN");
    this.baseURL = (opts.baseURL ?? env("CHECKRD_BASE_URL") ?? DEFAULT_BASE_URL).replace(/\/+$/, "");
    this.timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.maxRetries = opts.maxRetries ?? DEFAULT_MAX_RETRIES;
    this.apiVersion = opts.apiVersion ?? DEFAULT_API_VERSION;
    this.defaultHeaders = { ...(opts.defaultHeaders ?? {}) };
    this.userAgent = opts.userAgent ?? `checkrd-api-js/${SDK_VERSION}`;
    this.fetchImpl = opts.fetch ?? globalThis.fetch.bind(globalThis);

    this.agents = new Agents(this);
  }

  /**
   * Return a new Checkrd with the given overrides layered on top.
   * Useful for one-off retry-budget bumps or per-call header
   * injection without mutating the long-lived client.
   *
   * @example
   * ```ts
   * await client.withOptions({ maxRetries: 5 }).agents.list();
   * ```
   */
  withOptions(overrides: Partial<CheckrdOptions>): Checkrd {
    return new Checkrd({
      apiKey: overrides.apiKey ?? this.apiKey,
      bearerToken: overrides.bearerToken ?? this.bearerToken,
      baseURL: overrides.baseURL ?? this.baseURL,
      timeoutMs: overrides.timeoutMs ?? this.timeoutMs,
      maxRetries: overrides.maxRetries ?? this.maxRetries,
      apiVersion: overrides.apiVersion ?? this.apiVersion,
      defaultHeaders: { ...this.defaultHeaders, ...(overrides.defaultHeaders ?? {}) },
      userAgent: overrides.userAgent ?? this.userAgent,
      fetch: overrides.fetch ?? this.fetchImpl,
    });
  }

  // -------------------------------------------------------------
  // Low-level dispatch — called by resource classes
  // -------------------------------------------------------------

  /** @internal */
  async _request<T>(
    method: string,
    path: string,
    args: {
      query?: Record<string, unknown>;
      body?: unknown;
    } = {},
    opts: RequestOptions = {},
  ): Promise<T> {
    const url = this.buildUrl(path, args.query);
    const headers = this.buildHeaders(opts.headers);
    const init: RequestInit = {
      method,
      headers,
      signal: opts.signal,
    };
    if (args.body !== undefined) {
      init.body = JSON.stringify(args.body);
    }

    const attempts = (opts.maxRetries ?? this.maxRetries) + 1;
    const timeout = opts.timeoutMs ?? this.timeoutMs;
    let lastError: Error | undefined;
    for (let attempt = 1; attempt <= attempts; attempt++) {
      try {
        const response = await this.fetchWithTimeout(url, init, timeout);
        if (response.status >= 200 && response.status < 300) {
          if (response.status === 204) return undefined as T;
          const ct = response.headers.get("content-type") ?? "";
          if (!ct.includes("json")) return undefined as T;
          return (await response.json()) as T;
        }
        if (this.shouldRetry(response.status) && attempt < attempts) {
          await this.sleep(this.retryDelayMs(attempt));
          continue;
        }
        let body: { error?: { message: string; code?: string; param?: string; type?: string } } | undefined;
        try {
          body = (await response.json()) as typeof body;
        } catch {
          body = undefined;
        }
        throw makeStatusError(response, body);
      } catch (err) {
        if (err instanceof APIConnectionError) {
          lastError = err;
          if (attempt < attempts) {
            await this.sleep(this.retryDelayMs(attempt));
            continue;
          }
          throw err;
        }
        // Non-retriable: rethrow.
        throw err;
      }
    }
    // Unreachable; kept for type-narrowing.
    throw lastError ?? new APIConnectionError("retry budget exhausted");
  }

  /** @internal */
  async _get<T>(path: string, args: { query?: Record<string, unknown> } = {}, opts?: RequestOptions): Promise<T> {
    return this._request<T>("GET", path, args, opts);
  }

  /** @internal */
  async _post<T>(path: string, args: { body?: unknown; query?: Record<string, unknown> } = {}, opts?: RequestOptions): Promise<T> {
    return this._request<T>("POST", path, args, opts);
  }

  /** @internal */
  async _put<T>(path: string, args: { body?: unknown; query?: Record<string, unknown> } = {}, opts?: RequestOptions): Promise<T> {
    return this._request<T>("PUT", path, args, opts);
  }

  /** @internal */
  async _delete<T>(path: string, args: { query?: Record<string, unknown> } = {}, opts?: RequestOptions): Promise<T> {
    return this._request<T>("DELETE", path, args, opts);
  }

  /**
   * Issue a GET to a list endpoint and wrap the response in a
   * {@link Page} that knows how to fetch subsequent pages on
   * demand. Resource ``list()`` methods build a {@link PagePromise}
   * around this so callers can ``for await`` directly.
   *
   * @internal
   */
  async _getApiList<T>(path: string, query: Record<string, unknown>, opts?: RequestOptions): Promise<Page<T>> {
    const body = await this._get<PaginatedBody<T>>(path, { query }, opts);
    return new Page<T>(this, body, { path, params: query, decode: (x) => x });
  }

  // -------------------------------------------------------------
  // Internal helpers
  // -------------------------------------------------------------

  private buildUrl(path: string, query?: Record<string, unknown>): string {
    let url = this.baseURL + path;
    if (query) {
      const params = new URLSearchParams();
      for (const [k, v] of Object.entries(query)) {
        if (v !== undefined && v !== null) params.append(k, String(v));
      }
      const qs = params.toString();
      if (qs) url += "?" + qs;
    }
    return url;
  }

  private buildHeaders(extra?: Record<string, string>): Record<string, string> {
    const h: Record<string, string> = {
      Accept: "application/json",
      "Content-Type": "application/json",
      "User-Agent": this.userAgent,
      "Checkrd-Version": this.apiVersion,
      ...this.defaultHeaders,
      ...(extra ?? {}),
    };
    // Auth is injected last so callers can't accidentally clobber it.
    if (this.apiKey) h["X-API-Key"] = this.apiKey;
    else if (this.bearerToken) h["Authorization"] = `Bearer ${this.bearerToken}`;
    return h;
  }

  private async fetchWithTimeout(
    url: string,
    init: RequestInit,
    timeoutMs: number,
  ): Promise<Response> {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      controller.abort();
    }, timeoutMs);
    // If the caller supplied a signal, propagate its abort to ours.
    if (init.signal) {
      init.signal.addEventListener("abort", () => {
        controller.abort();
      });
    }
    try {
      return await this.fetchImpl(url, { ...init, signal: controller.signal });
    } catch (err) {
      if (err instanceof Error && err.name === "AbortError") {
        throw new APITimeoutError(`request timed out after ${timeoutMs.toString()}ms`);
      }
      throw new APIConnectionError(err instanceof Error ? err.message : String(err));
    } finally {
      clearTimeout(timer);
    }
  }

  private shouldRetry(status: number): boolean {
    return status === 408 || status === 409 || status === 429 || status >= 500;
  }

  private retryDelayMs(attempt: number): number {
    // Exponential backoff with full jitter, capped at 8s. Same
    // shape as the Python sibling.
    const base = Math.min(500 * 2 ** (attempt - 1), 8000);
    return base * (0.5 + Math.random() * 0.5);
  }

  private async sleep(ms: number): Promise<void> {
    await new Promise<void>((r) => {
      setTimeout(r, ms);
    });
  }
}

/**
 * Edge-runtime-safe env getter. Returns a function that reads
 * ``process.env[name]`` on Node / Bun / Deno and ``undefined``
 * everywhere else. Avoids importing ``@types/node``, which would
 * couple the package to a Node lib it doesn't otherwise need.
 */
function readEnv(): (name: string) => string | undefined {
  const proc = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process;
  const env = proc?.env;
  if (!env) return () => undefined;
  return (name: string) => env[name];
}
