/**
 * Logger contract. Mirrors Python's `logging.getLogger("checkrd")` in
 * spirit: the SDK logs to a structured sink, the sink is swappable, and
 * sensitive headers never leave redaction.
 *
 * The default implementation writes to `console` with the same level
 * semantics every JS logger (pino, winston, bunyan, roarr) exposes —
 * users who already run one of those can pass it through directly.
 */

/** Numeric log levels. Low number = more verbose. `silent` = disabled. */
export type LogLevel = "debug" | "info" | "warn" | "error" | "silent";

/** Structured log attributes attached to a message (child-logger-friendly). */
export type LogAttributes = Record<string, unknown>;

/**
 * Minimal logger surface. Any logger in the JS ecosystem (pino, winston,
 * bunyan, roarr, the built-in `console`) already implements these four
 * methods, so users can pass their existing logger straight through.
 *
 * The variadic `...args` channel carries structured attributes — exactly
 * how pino expects them. Consumers that want plain-text logging can
 * ignore the extra args.
 */
export interface Logger {
  debug(message: string, ...args: unknown[]): void;
  info(message: string, ...args: unknown[]): void;
  warn(message: string, ...args: unknown[]): void;
  error(message: string, ...args: unknown[]): void;
}

const LEVEL_RANK: Record<LogLevel, number> = {
  debug: 10,
  info: 20,
  warn: 30,
  error: 40,
  silent: 100,
};

// Re-exported for backwards compatibility — the canonical definition now
// lives in `_sensitive.ts` so telemetry sinks can reuse it without
// pulling in the logger module. External callers that imported
// `redactSensitive` from here continue to work unchanged.
export { redactSensitive } from "./_sensitive.js";
import { readEnv } from "./_env.js";
import { redactSensitive } from "./_sensitive.js";

/**
 * Operator-facing PII banner emitted the first time Checkrd observes
 * ``CHECKRD_DEBUG=1`` / ``debug: true``. Mirrors the Python SDK's
 * ``warn_debug_pii_risk``.
 *
 * Checkrd sits in the request path for LLM agent traffic, so debug
 * logging can surface prompt payloads — customer data operators do
 * not expect in their log aggregator. The banner is a cheap safety
 * net against accidents like "left CHECKRD_DEBUG=1 in the CI env".
 *
 * Writes directly to ``stderr`` (when available) rather than through
 * the SDK logger because a user-configured logger may route to a
 * destination the operator is not actively watching; stderr lands in
 * the terminal, journald, or the container's log stream where a loud
 * banner will actually be seen. On runtimes without ``process.stderr``
 * (Cloudflare Workers, some Edge runtimes) falls back to
 * ``console.warn`` with the same text.
 */
const DEBUG_PII_WARNING =
  "checkrd: DEBUG logging is enabled.\n" +
  "  Request/response bodies and prompt payloads MAY appear in logs.\n" +
  "  Checkrd's own code redacts credential-bearing headers, but HTTP\n" +
  "  client libraries do not redact request/response bodies at DEBUG\n" +
  "  level. For an LLM agent SDK, that means prompts and completions —\n" +
  "  which typically contain customer data — can end up in stdout/stderr,\n" +
  "  log files, and any log-shipping pipeline (journald, CloudWatch,\n" +
  "  Datadog, Loki, etc.).\n" +
  "\n" +
  "  DO NOT enable CHECKRD_DEBUG=1 or debug: true in production.\n" +
  "  Use it during local development for a single request, then\n" +
  "  turn it off. See https://checkrd.io/docs/debug-logging";

let _debugWarningEmitted = false;

/**
 * Options for {@link warnDebugPiiRisk}.
 */
export interface WarnDebugPiiRiskOptions {
  /** When true (default) fires at most once per process. */
  once?: boolean;
  /** Stderr sink override for tests. Default: ``process.stderr.write``. */
  writeStderr?: (chunk: string) => void;
}

/**
 * Emit the one-time stderr banner warning about PII risk in debug logs.
 * Call whenever an entry point observes ``debug=true`` or the
 * ``CHECKRD_DEBUG=1`` env var.
 *
 * Idempotent across repeated calls with ``once=true`` (default) — a
 * process that constructs many clients should see the banner once,
 * not once per client. Tests that want to verify repeated emission
 * can pass ``once: false``.
 */
export function warnDebugPiiRisk(opts: WarnDebugPiiRiskOptions = {}): void {
  const once = opts.once ?? true;
  if (once && _debugWarningEmitted) return;
  if (once) _debugWarningEmitted = true;

  const write = opts.writeStderr ?? defaultStderrWriter();
  write(DEBUG_PII_WARNING + "\n");
}

function defaultStderrWriter(): (chunk: string) => void {
  // `process.stderr.write` exists on Node and Bun. Cloudflare Workers
  // / Vercel Edge / browsers don't expose it — fall back to
  // `console.warn` so the message still lands somewhere visible.
  const proc = (globalThis as unknown as {
    process?: { stderr?: { write?: (chunk: string) => boolean } };
  }).process;
  const stderr = proc?.stderr;
  const stderrWrite = stderr?.write;
  if (stderr !== undefined && typeof stderrWrite === "function") {
    return (chunk: string): void => { stderrWrite.call(stderr, chunk); };
  }

  return (chunk: string): void => { console.warn(chunk); };
}

/** Test-only helper to reset the one-shot guard. Not part of the public API. */
export function __resetDebugWarningForTesting(): void {
  _debugWarningEmitted = false;
}

/**
 * One-time banner fired when ``dangerouslyAllowBrowser: true`` is set
 * AND the runtime actually looks like a real browser. Mirrors
 * {@link warnDebugPiiRisk}'s idempotency / stderr-first semantics.
 *
 * The warning calls out the specific attack that makes browser use
 * worse for Checkrd than for a typical AI SDK: the agent signing key
 * that ships with the bundle lets ANYONE who views the source forge
 * telemetry batches. A stolen OpenAI API key is a billing problem; a
 * stolen agent signing key is a policy-evasion and audit-forgery
 * problem.
 */
const REAL_BROWSER_WARNING =
  "checkrd: `dangerouslyAllowBrowser: true` is set AND a real browser\n" +
  "  environment was detected. This ships BOTH the control-plane API\n" +
  "  key AND the Ed25519 agent signing key to every end user viewing\n" +
  "  the page.\n" +
  "\n" +
  "  The signing key is the dangerous one. Anyone who sees it can:\n" +
  "    - forge telemetry batches that look like they came from your\n" +
  "      agent (poisoning dashboards and audit trails),\n" +
  "    - impersonate your agent to any downstream consumer that\n" +
  "      trusts its signatures.\n" +
  "\n" +
  "  This is NOT equivalent to shipping an OpenAI/Anthropic API key\n" +
  "  in a browser (which is merely a billing/abuse problem). Rotate\n" +
  "  the key the moment the bundle is no longer controlled.\n" +
  "\n" +
  "  See https://checkrd.io/docs/browser-use for safer alternatives\n" +
  "  (proxy the agent through your own backend).";

let _browserWarningEmitted = false;

/**
 * Emit a one-time stderr warning when the SDK detects it's running in a
 * real browser context. Highlights that browser use exposes API keys and
 * points to the recommended backend-proxy pattern.
 */
export function warnRealBrowserUse(opts: WarnDebugPiiRiskOptions = {}): void {
  const once = opts.once ?? true;
  if (once && _browserWarningEmitted) return;
  if (once) _browserWarningEmitted = true;

  const write = opts.writeStderr ?? defaultStderrWriter();
  write(REAL_BROWSER_WARNING + "\n");
}

/** Test-only. Pair with ``__resetDebugWarningForTesting`` for the pattern. */
export function __resetBrowserWarningForTesting(): void {
  _browserWarningEmitted = false;
}

/**
 * Null logger — discards every message. Use as the default for
 * library-embedded deployments where logging would be noise.
 */
export const noopLogger: Logger = {
  debug: () => undefined,
  info: () => undefined,
  warn: () => undefined,
  error: () => undefined,
};

/**
 * Create a console-backed logger at the given level. Each message is
 * prefixed with `checkrd:` so mixed-SDK logs are easy to grep.
 */
export function createConsoleLogger(level: LogLevel = "info"): Logger {
  const threshold = LEVEL_RANK[level];
  const log = (ownLevel: LogLevel, fn: (...args: unknown[]) => void) =>
    (message: string, ...args: unknown[]): void => {
      if (LEVEL_RANK[ownLevel] < threshold) return;
      const redacted = args.map((a) => redactSensitive(a));
      fn(`checkrd: ${message}`, ...redacted);
    };
  return {
     
    debug: log("debug", console.debug.bind(console)),
     
    info: log("info", console.info.bind(console)),
     
    warn: log("warn", console.warn.bind(console)),
     
    error: log("error", console.error.bind(console)),
  };
}

/**
 * Wrap an existing logger so that every argument is scrubbed through
 * {@link redactSensitive} before reaching the underlying sink. Use this
 * whenever a caller passes their own logger — you cannot assume a
 * third-party logger knows what to redact.
 */
export function wrapWithRedaction(inner: Logger): Logger {
  const scrub = (fn: (msg: string, ...args: unknown[]) => void) =>
    (message: string, ...args: unknown[]): void => {
      fn(message, ...args.map((a) => redactSensitive(a)));
    };
  return {
    debug: scrub(inner.debug.bind(inner)),
    info: scrub(inner.info.bind(inner)),
    warn: scrub(inner.warn.bind(inner)),
    error: scrub(inner.error.bind(inner)),
  };
}

/**
 * Resolve the logger to use, given user options and env. Precedence:
 * explicit `logger` > `logLevel` > `CHECKRD_LOG_LEVEL` env > `info`.
 */
export function resolveLogger(opts: {
  logger?: Logger | undefined;
  logLevel?: LogLevel | undefined;
  debug?: boolean | undefined;
}): Logger {
  if (opts.logger) return wrapWithRedaction(opts.logger);
  const envLevel = readEnv("CHECKRD_LOG_LEVEL");
  const effective: LogLevel =
    opts.logLevel ??
    (isLogLevel(envLevel) ? envLevel : undefined) ??
    (opts.debug ? "debug" : "info");
  return createConsoleLogger(effective);
}

function isLogLevel(v: string | undefined): v is LogLevel {
  return v !== undefined && (v in LEVEL_RANK);
}
