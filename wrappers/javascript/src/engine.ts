/**
 * WASM engine bridge. Mirrors `checkrd/engine.py`.
 *
 * Two responsibilities:
 *   1. Load the WASM core (`checkrd_core.wasm`) with SHA-256 integrity
 *      verification, using `@bjorn3/browser_wasi_shim` for the WASI
 *      imports that Rust's stdlib requires even when no actual I/O is
 *      performed.
 *   2. Marshal data across the FFI boundary — JS strings ↔ WASM linear
 *      memory, and the u64-packed `(ptr << 32) | len` return values that
 *      the core uses for variable-length outputs.
 *
 * JS is single-threaded per isolate, so unlike the Python wrapper we do
 * not need a lock around evaluate(): synchronous WASM calls cannot
 * interleave within a single event loop turn.
 */
import { File, OpenFile, WASI } from "@bjorn3/browser_wasi_shim";

import { CheckrdInitError, PolicySignatureError } from "./exceptions.js";
import { EXPECTED_SHA256 } from "./_wasm_integrity.js";

// ---------------------------------------------------------------------------
// Node-only primitives, accessed lazily
// ---------------------------------------------------------------------------
//
// The synchronous engine construction path requires `readFileSync` +
// `createHash` + `fileURLToPath` to locate and hash `checkrd_core.wasm`
// before `WebAssembly.Module(...)` is called. Those modules do not
// exist on Cloudflare Workers, Vercel Edge, Deno's `--no-node-compat`
// paths, or the browser. Importing them at the top of the module would
// make the whole SDK fail to load on those runtimes even for callers
// who never construct an engine there.
//
// Instead we resolve them lazily via `require("node:fs")` at first
// use. The synchronous constructor works on Node and Bun (both expose
// a CommonJS-compatible require). On edge runtimes a loud, directional
// error surfaces pointing to the async `WasmEngine.create()` path —
// the correct answer there is `WebAssembly.instantiateStreaming` fed
// by `fetch(new URL('./checkrd_core.wasm', import.meta.url))`, which
// is the v1.1 work item.
interface NodeFsShim {
  readFileSync(path: string): Uint8Array;
}
interface NodeCryptoShim {
  createHash(algo: string): {
    update(data: Uint8Array): { digest(enc: string): string };
  };
}
interface NodeUrlShim {
  fileURLToPath(url: URL | string): string;
}

function loadNodeFs(): NodeFsShim {
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports -- sync load on Node
    return require("node:fs") as NodeFsShim;
  } catch {
    throw new CheckrdInitError(
      "node:fs is not available in this runtime. The synchronous WasmEngine " +
        "constructor runs only on Node / Bun. For Cloudflare Workers / " +
        "Vercel Edge / Deno / browser, use `await WasmEngine.create()` " +
        "(shipping in v1.1).",
    );
  }
}

function loadNodeCrypto(): NodeCryptoShim {
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports -- sync load on Node
    return require("node:crypto") as NodeCryptoShim;
  } catch {
    throw new CheckrdInitError(
      "node:crypto is not available in this runtime. See loadNodeFs " +
        "note — use `await WasmEngine.create()` on edge runtimes.",
    );
  }
}

function loadNodeUrl(): NodeUrlShim {
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports -- sync load on Node
    return require("node:url") as NodeUrlShim;
  } catch {
    throw new CheckrdInitError(
      "node:url is not available in this runtime. See loadNodeFs " +
        "note — use `await WasmEngine.create()` on edge runtimes.",
    );
  }
}

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/** Structured result from {@link WasmEngine.evaluate}. */
export interface EvalResult {
  /** True if policy allowed the request. */
  allowed: boolean;
  /** Human-readable reason, present only when {@link allowed} is false. */
  deny_reason?: string;
  /** Canonical-JSON telemetry event (enriched by the transport before enqueue). */
  telemetry_json: string;
  /** Correlation ID, echoed from input. */
  request_id: string;
}

/** Input shape for {@link WasmEngine.evaluate}. Mirrors the WASM-side JSON. */
export interface EvaluateRequest {
  /** Correlation ID; echoed back in the result. */
  request_id: string;
  /** HTTP method (canonical upper-case). */
  method: string;
  /** Full request URL. */
  url: string;
  /** Header name/value pairs. Header names are matched case-insensitively. */
  headers: [string, string][];
  /** Request body as a UTF-8 string, or null for bodyless requests. */
  body: string | null;
  /** ISO-8601 timestamp (for event records). */
  timestamp: string;
  /** Millisecond-precision Unix timestamp (for rate-limit windows). */
  timestamp_ms: number;
  /** W3C trace-context trace ID, if present. */
  trace_id?: string;
  /** W3C trace-context span ID. */
  span_id?: string;
  /** W3C trace-context parent span ID. */
  parent_span_id?: string;
}

/** Ed25519 keypair returned from {@link WasmEngine.generateKeypair}. */
export interface Keypair {
  /** 32-byte Ed25519 private key. */
  privateKey: Uint8Array;
  /** 32-byte Ed25519 public key. */
  publicKey: Uint8Array;
}

/** Options for the {@link WasmEngine} constructor. */
export interface WasmEngineOptions {
  /** 32-byte Ed25519 private key; omit (or pass an empty array) for anonymous mode. */
  privateKeyBytes?: Uint8Array;
  /** Explicit instance ID (only used when private key is anonymous, e.g. KMS). */
  instanceId?: string;
}

/**
 * Options for {@link WasmEngine.create}. Extends {@link WasmEngineOptions}
 * with an optional `wasm` override for bundlers that do not resolve
 * `new URL('../checkrd_core.wasm', import.meta.url)` to a fetchable
 * asset URL (some Cloudflare Workers / Vercel Edge configurations).
 */
export interface WasmEngineCreateOptions extends WasmEngineOptions {
  /**
   * Explicit source for the WASM binary. Accepts a URL, a URL string, a
   * `Response` (for example, one already being constructed by the
   * runtime's asset binding), raw bytes, or a pre-compiled
   * {@link WebAssembly.Module}. Passing a `WebAssembly.Module` skips the
   * integrity check — use only when the build pipeline already verifies
   * the bytes (for example, Wrangler's `wasm_modules` binding signs
   * and caches the asset server-side).
   */
  wasm?: WasmSource;
}

/** Signed telemetry envelope produced by {@link WasmEngine.signTelemetryBatch}. */
export interface SignedBatch {
  content_digest: string;
  signature_input: string;
  signature: string;
  dsse_envelope: string;
  instance_id: string;
  expires: number;
}

// ---------------------------------------------------------------------------
// Internal FFI contract
// ---------------------------------------------------------------------------

interface WasmExports {
  memory: WebAssembly.Memory;
  alloc: (len: number) => number;
  dealloc: (ptr: number, len: number) => void;
  init: (
    policyPtr: number,
    policyLen: number,
    agentPtr: number,
    agentLen: number,
    keyPtr: number,
    keyLen: number,
    instancePtr: number,
    instanceLen: number,
  ) => number;
  evaluate_request: (ptr: number, len: number) => bigint;
  set_kill_switch: (active: number) => void;
  generate_keypair: () => bigint;
  derive_public_key: (ptr: number, len: number) => bigint;
  sign: (ptr: number, len: number) => bigint;
  sign_telemetry_batch: (
    batchPtr: number,
    batchLen: number,
    uriPtr: number,
    uriLen: number,
    agentPtr: number,
    agentLen: number,
    noncePtr: number,
    nonceLen: number,
    created: bigint,
    expires: bigint,
  ) => bigint;
  reload_policy: (ptr: number, len: number) => number;
  reload_policy_signed: (
    envelopePtr: number,
    envelopeLen: number,
    keysPtr: number,
    keysLen: number,
    nowUnixSecs: bigint,
    maxAgeSecs: bigint,
  ) => number;
  get_active_policy_version: () => bigint;
  set_initial_policy_version: (version: bigint) => number;
  _initialize?: () => void;
  _start?: () => void;
}

// Module-level cache: WebAssembly.Module compilation is expensive (~50ms
// for a 1.4 MB module) and the compiled artifact is safe to share across
// instances — each `new WebAssembly.Instance(module, ...)` gets its own
// linear memory. Same discipline as the Python wrapper's `_cached_module`.
let _cachedModule: WebAssembly.Module | null = null;
let _cachedUtilExports: WasmExports | null = null;

const encoder = new TextEncoder();
const decoder = new TextDecoder("utf-8", { fatal: true });

// ---------------------------------------------------------------------------
// WASM loader + integrity check
// ---------------------------------------------------------------------------

function resolveWasmPath(): string {
  // `../checkrd_core.wasm` works in both ESM (via import.meta.url +
  // fileURLToPath) and CJS (via tsup's import.meta.url shim). The WASM
  // sits at the package root; `dist/engine.js` is one level deeper.
  const url = new URL("../checkrd_core.wasm", import.meta.url);
  return loadNodeUrl().fileURLToPath(url);
}

/**
 * Env-var names checked (in order) to detect a production-looking
 * deployment. Mirrors the Python SDK's
 * ``_PRODUCTION_SIGNAL_ENVS``. Every framework spells "production"
 * differently, so we accept any one of these flagging it.
 */
const PRODUCTION_SIGNAL_ENVS: readonly string[] = [
  "CHECKRD_ENV",
  "CHECKRD_ENVIRONMENT",
  "ENVIRONMENT",
  "ENV",
  "APP_ENV",
  "NODE_ENV",
  "RAILS_ENV",
  "DJANGO_ENV",
  "FLASK_ENV",
  "PYTHON_ENV",
  "DEPLOYMENT_ENVIRONMENT",
];

/** Values that flag production on any of {@link PRODUCTION_SIGNAL_ENVS}. */
const PRODUCTION_ENV_VALUES: ReadonlySet<string> = new Set([
  "production",
  "prod",
  "canary",
  "live",
]);

/** Break-glass acknowledgment for the WASM integrity bypass in prod. */
const ACK_WASM_RISK_ENV = "CHECKRD_I_UNDERSTAND_WASM_INTEGRITY_RISK";
const ACK_WASM_RISK_VALUE = "i-understand-the-risk";

/**
 * Return the [envName, rawValue] pair of the first production signal
 * found, or null. Reads defensively — `process` may not exist on
 * Cloudflare Workers / Deno without compat flags.
 */
function productionSignal(): [string, string] | null {
  const proc = (globalThis as unknown as {
    process?: { env?: Record<string, string | undefined> };
  }).process;
  const env = proc?.env;
  if (!env) return null;
  for (const name of PRODUCTION_SIGNAL_ENVS) {
    const raw = env[name];
    if (raw === undefined) continue;
    const trimmed = raw.trim().toLowerCase();
    if (PRODUCTION_ENV_VALUES.has(trimmed)) return [name, raw.trim()];
  }
  return null;
}

/**
 * True iff the operator typed the break-glass acknowledgment phrase
 * exactly. Exact-match (no trim, no case-fold) — see the matching
 * Python helper ``_wasm_risk_acknowledged`` for the rationale.
 */
function wasmRiskAcknowledged(): boolean {
  const proc = (globalThis as unknown as {
    process?: { env?: Record<string, string | undefined> };
  }).process;
  return proc?.env?.[ACK_WASM_RISK_ENV] === ACK_WASM_RISK_VALUE;
}

/**
 * Resolve whether the WASM integrity check should be skipped. Honors the
 * ``CHECKRD_SKIP_WASM_INTEGRITY`` flag, but refuses to do so in
 * production-like environments unless the break-glass acknowledgment is
 * set. Surfaces the reason as a {@link CheckrdInitError} so the init
 * path can show an actionable error.
 *
 * Exported with a double-underscore prefix for the test suite. Not part
 * of the public API — the name is a convention mirroring Python's
 * ``_private`` leading-underscore signal.
 */
export function __shouldSkipIntegrity(): boolean {
  return shouldSkipIntegrity();
}

function shouldSkipIntegrity(): boolean {
  const proc = (globalThis as unknown as {
    process?: { env?: Record<string, string | undefined> };
  }).process;
  const requested = proc?.env?.CHECKRD_SKIP_WASM_INTEGRITY === "1";
  if (!requested) return false;
  const signal = productionSignal();
  if (signal !== null && !wasmRiskAcknowledged()) {
    const [name, value] = signal;
    throw new CheckrdInitError(
      "CHECKRD_SKIP_WASM_INTEGRITY is set in a production-looking " +
        `environment (${name}=${JSON.stringify(value)}). This would ` +
        "disable the only supply-chain defense on the WASM binary. " +
        "Either:\n" +
        "  1. Unset CHECKRD_SKIP_WASM_INTEGRITY (recommended — the " +
        "hash file ships with the npm package), OR\n" +
        `  2. Set ${ACK_WASM_RISK_ENV}=${JSON.stringify(ACK_WASM_RISK_VALUE)} ` +
        "if this is an intentional emergency debugging session.\n" +
        "See https://checkrd.io/errors/wasm_integrity_skip_in_prod",
    );
  }
  return true;
}

function verifyIntegrity(bytes: Uint8Array): void {
  const skip = shouldSkipIntegrity();
  if (EXPECTED_SHA256.length === 0) {
    if (skip) return;
    throw new CheckrdInitError(
      "WASM integrity file missing or empty. This is the only supply-chain " +
        "defense on the WASM binary. Set CHECKRD_SKIP_WASM_INTEGRITY=1 to " +
        "bypass (dev only).",
    );
  }
  const actual = loadNodeCrypto().createHash("sha256").update(bytes).digest("hex");
  if (actual !== EXPECTED_SHA256) {
    if (skip) return;
    throw new CheckrdInitError(
      `WASM integrity check failed (tampered binary?). ` +
        `expected=${EXPECTED_SHA256} actual=${actual}`,
    );
  }
}

function loadWasmModule(): WebAssembly.Module {
  if (_cachedModule) return _cachedModule;
  let raw: Uint8Array;
  try {
    raw = loadNodeFs().readFileSync(resolveWasmPath());
  } catch (err) {
    if (err instanceof CheckrdInitError) throw err;
    throw new CheckrdInitError(
      `checkrd_core.wasm not found. Packaging error? (${
        err instanceof Error ? err.message : String(err)
      })`,
    );
  }
  // Read the binary once and hand the same bytes to both the hash
  // check and the WASM module constructor. Rereading would open a
  // TOCTOU window where an attacker with write access to the file
  // could swap contents between the two reads.
  const bytes = new Uint8Array(raw.byteLength);
  bytes.set(raw);
  verifyIntegrity(bytes);
  _cachedModule = new WebAssembly.Module(bytes);
  return _cachedModule;
}

// ---------------------------------------------------------------------------
// Async loader — runtime-agnostic
// ---------------------------------------------------------------------------
//
// Uses `fetch(new URL(...))` + `WebAssembly.compileStreaming` when the
// runtime supports it, falling back to `arrayBuffer()` +
// `WebAssembly.compile`. Integrity verification uses Web Crypto's
// `crypto.subtle.digest`, which is present on every modern runtime
// (Node 20+, Bun, Deno, Cloudflare Workers, Vercel Edge, browsers).
//
// None of the `node:*` helpers are touched on this path, so importing
// `checkrd` followed by `await WasmEngine.create(...)` works on
// Cloudflare Workers / Vercel Edge / Deno / browser without any
// further configuration.

/**
 * Optional override for the WASM source passed to
 * {@link WasmEngine.create}. Callers that run under a bundler whose
 * asset-resolution story doesn't match `new URL(..., import.meta.url)`
 * (Cloudflare Workers with a WASM binding, Vercel Edge with `?module`
 * imports, etc.) can hand the bytes in directly.
 */
export type WasmSource =
  | URL
  | string
  | Response
  | Uint8Array
  | ArrayBuffer
  | WebAssembly.Module;

/**
 * Read the WASM bytes from whichever source the runtime supports.
 * Accepts `URL` / `string` (fetched), an already-executing `Response`
 * (awaited via `arrayBuffer()`), raw bytes, or a pre-compiled
 * `WebAssembly.Module` which skips the integrity check entirely (the
 * caller has taken responsibility).
 */
async function readWasmBytes(source: WasmSource | undefined): Promise<Uint8Array | WebAssembly.Module> {
  if (source instanceof WebAssembly.Module) return source;
  if (source instanceof Uint8Array) return source;
  if (source instanceof ArrayBuffer) return new Uint8Array(source);
  const fetchUrl =
    source instanceof URL
      ? source
      : typeof source === "string"
        ? new URL(source, (globalThis as unknown as { location?: { href?: string } }).location?.href ?? "http://localhost/")
        : source instanceof Response
          ? null
          : new URL("../checkrd_core.wasm", import.meta.url);
  try {
    let response: Response;
    if (source instanceof Response) {
      response = source;
    } else {
      if (fetchUrl === null) {
        // Defensive: the only branch that produces `null` is the
        // Response branch handled above.
        throw new CheckrdInitError("unreachable: fetchUrl must be a URL");
      }
      // Node's ``fetch`` (>=18) does not support ``file:`` URLs. The
      // default fallback URL (``new URL('../checkrd_core.wasm',
      // import.meta.url)``) resolves to a ``file://`` URL when the
      // SDK runs on Node ESM, so we read it via ``fs.readFile``
      // here. Edge runtimes never see ``file:`` so they keep going
      // through the standard ``fetch`` path.
      if (fetchUrl.protocol === "file:") {
        const { readFile } = await import("node:fs/promises");
        const { fileURLToPath } = await import("node:url");
        const buf = await readFile(fileURLToPath(fetchUrl));
        return new Uint8Array(buf);
      }
      response = await fetch(fetchUrl);
    }
    if (!response.ok) {
      throw new CheckrdInitError(
        `Fetched WASM returned HTTP ${response.status.toString()} (${response.statusText})`,
      );
    }
    const buf = await response.arrayBuffer();
    return new Uint8Array(buf);
  } catch (err) {
    if (err instanceof CheckrdInitError) throw err;
    throw new CheckrdInitError(
      "Failed to fetch checkrd_core.wasm. On Cloudflare Workers / " +
        "Vercel Edge, pass `wasm: <URL | Response | bytes>` explicitly " +
        "to WasmEngine.create() or initAsync() — the default " +
        "`new URL('../checkrd_core.wasm', import.meta.url)` relies on " +
        "the bundler resolving an asset URL, which some edge runtimes " +
        `do not. (${err instanceof Error ? err.message : String(err)})`,
    );
  }
}

function bytesToHex(bytes: Uint8Array): string {
  let s = "";
  for (const byte of bytes) {
    s += byte.toString(16).padStart(2, "0");
  }
  return s;
}

async function verifyIntegrityAsync(bytes: Uint8Array): Promise<void> {
  // `shouldSkipIntegrity` reads `globalThis.process?.env` defensively —
  // safe on Node, Bun, workerd (nodejs_compat), and Vercel Edge, and
  // returns false when the env surface isn't present (Deno no-compat,
  // browser). It throws synchronously in production-like envs that lack
  // the break-glass acknowledgment; we let that propagate so the init
  // path surfaces the same actionable error as the sync path.
  const skip = shouldSkipIntegrity();
  if (EXPECTED_SHA256.length === 0) {
    if (skip) return;
    throw new CheckrdInitError(
      "WASM integrity file missing or empty. This is the only supply-chain " +
        "defense on the WASM binary. Set CHECKRD_SKIP_WASM_INTEGRITY=1 to " +
        "bypass (dev only).",
    );
  }
  // Pass the Uint8Array directly — it's a TypedArray, which is a
  // valid BufferSource and avoids a needless copy. (Earlier code
  // copied into a fresh ArrayBuffer to side-step a TypeScript narrowing
  // issue with ``Uint8Array<ArrayBufferLike>``; the cleaner fix below
  // works under strict mode and survives cross-realm checks in
  // edge-runtime sandboxes.)
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  const actual = bytesToHex(new Uint8Array(digest));
  if (actual !== EXPECTED_SHA256) {
    if (skip) return;
    throw new CheckrdInitError(
      `WASM integrity check failed (tampered binary?). ` +
        `expected=${EXPECTED_SHA256} actual=${actual}`,
    );
  }
}

async function loadWasmModuleAsync(
  source?: WasmSource,
): Promise<WebAssembly.Module> {
  if (source === undefined && _cachedModule) return _cachedModule;

  const raw = await readWasmBytes(source);
  if (raw instanceof WebAssembly.Module) {
    // Caller passed a pre-compiled module. Trust their build pipeline;
    // we can't re-hash a compiled module without the source bytes.
    if (source === undefined) _cachedModule = raw;
    return raw;
  }
  // Copy into a fresh Uint8Array so the hash and the compile operate
  // on the same bytes (closes the same TOCTOU window the sync path
  // already guards against).
  const bytes = new Uint8Array(raw.byteLength);
  bytes.set(raw);
  await verifyIntegrityAsync(bytes);
  const module = await WebAssembly.compile(bytes);
  if (source === undefined) _cachedModule = module;
  return module;
}

function instantiate(module: WebAssembly.Module): WasmExports {
  const wasi = new WASI(
    [],
    [],
    [
      new OpenFile(new File([])),
      new OpenFile(new File([])),
      new OpenFile(new File([])),
    ],
  );
  const instance = new WebAssembly.Instance(module, {
    wasi_snapshot_preview1: wasi.wasiImport,
  });
  const exports = instance.exports as unknown as WasmExports;
  // The WASI shim binds `this.inst = instance` inside start/initialize —
  // without this, any WASI call from WASM (e.g., random_get for entropy
  // during keypair generation) will throw "cannot read exports of
  // undefined". We call one of them unconditionally so the binding
  // happens regardless of whether the cdylib was built with _initialize.
  type WasiInitInstance = Parameters<typeof wasi.initialize>[0];
  type WasiStartInstance = Parameters<typeof wasi.start>[0];
  if (typeof exports._start === "function") {
    wasi.start(instance as unknown as WasiStartInstance);
  } else {
    // initialize() is a no-op when _initialize is missing but still
    // sets wasi.inst — the behavior we need for cdylib crates.
    wasi.initialize(instance as unknown as WasiInitInstance);
  }
  return exports;
}

function getUtilExports(): WasmExports {
  if (_cachedUtilExports) return _cachedUtilExports;
  _cachedUtilExports = instantiate(loadWasmModule());
  return _cachedUtilExports;
}

// Test-only reset used by the property tests. Not exported from the
// public API. Setting via a hidden symbol keeps this off the public
// docs surface while still reachable from tests.
/**
 * Reset the module cache. Internal — used only from tests to verify
 * WASM integrity failure paths. Not part of the public API.
 * @internal
 */
export function __resetWasmModuleCache(): void {
  _cachedModule = null;
  _cachedUtilExports = null;
}

// ---------------------------------------------------------------------------
// FFI marshalling helpers
// ---------------------------------------------------------------------------

function unpack(packed: bigint): [ptr: number, len: number] {
  // Upper 32 bits = pointer, lower 32 = length. Both fit in Number safely
  // because each is bounded by 2^32 ≤ 2^53 (JS safe integer range).
  const ptr = Number((packed >> 32n) & 0xFFFFFFFFn);
  const len = Number(packed & 0xFFFFFFFFn);
  return [ptr, len];
}

// ---------------------------------------------------------------------------
// Public class
// ---------------------------------------------------------------------------

/**
 * Per-agent WASM engine instance. Each instance owns its own linear
 * memory, rate-limit counters, kill switch state, and identity key —
 * mirroring the Python `WasmEngine` isolation guarantee.
 */
export class WasmEngine {
  private readonly exports: WasmExports;
  private readonly memory: WebAssembly.Memory;

  /**
   * Synchronous constructor — Node and Bun only. Fails with a
   * directional error on runtimes that lack `node:fs` / `node:crypto` /
   * `node:url` (Cloudflare Workers, Vercel Edge, Deno, browser). For
   * those runtimes use `await WasmEngine.create(...)` instead.
   *
   * The fourth parameter is intentionally undocumented: it lets the
   * async factory hand a precompiled module to the constructor,
   * reusing the instance-init code path without duplicating it.
   */
  constructor(
    policyJson: string,
    agentId: string,
    options: WasmEngineOptions = {},
    _precompiled?: WebAssembly.Module,
  ) {
    const module = _precompiled ?? loadWasmModule();
    this.exports = instantiate(module);
    this.memory = this.exports.memory;

    const privateKey = options.privateKeyBytes ?? new Uint8Array();
    const instanceId = options.instanceId ?? "";

    const rc = this._callInit(policyJson, agentId, privateKey, instanceId);
    if (rc !== 0) {
      throw new CheckrdInitError(
        `engine init failed (ffi code ${rc.toString()})`,
      );
    }
  }

  /**
   * Asynchronous factory. Runtime-agnostic: works on Node (18+), Bun,
   * Deno, Cloudflare Workers, Vercel Edge, and modern browsers.
   *
   * Loads the WASM binary via `fetch(new URL('../checkrd_core.wasm',
   * import.meta.url))`, verifies the SHA-256 via `crypto.subtle`, and
   * compiles via `WebAssembly.compile`. Pass `options.wasm` when the
   * host bundler does not resolve `import.meta.url`-relative WASM
   * assets at runtime.
   *
   *     // Node: identical to `new WasmEngine(...)` apart from being async.
   *     const engine = await WasmEngine.create(policyJson, "my-agent");
   *
   *     // Cloudflare Workers: bind the WASM as a module and pass it.
   *     import wasm from "./checkrd_core.wasm";
   *     const engine = await WasmEngine.create(policyJson, "my-agent", { wasm });
   *
   *     // Vercel Edge: pass the URL resolved by the bundler.
   *     import wasmUrl from "./checkrd_core.wasm?url";
   *     const engine = await WasmEngine.create(policyJson, "my-agent", { wasm: wasmUrl });
   */
  static async create(
    policyJson: string,
    agentId: string,
    options: WasmEngineCreateOptions = {},
  ): Promise<WasmEngine> {
    const module = await loadWasmModuleAsync(options.wasm);
    const { wasm: _wasm, ...engineOpts } = options;
    void _wasm;
    return new WasmEngine(policyJson, agentId, engineOpts, module);
  }

  /**
   * Eagerly compile and cache the WASM module without constructing an
   * engine. Useful at process startup on hot-path services where the
   * first `evaluate()` call must not pay the compile cost.
   */
  static async prewarm(source?: WasmSource): Promise<void> {
    await loadWasmModuleAsync(source);
  }

  // -------------------------------------------------------------------
  // Memory marshalling
  // -------------------------------------------------------------------

  private writeString(s: string): [ptr: number, len: number] {
    const bytes = encoder.encode(s);
    if (bytes.length === 0) return [0, 0];
    const ptr = this.exports.alloc(bytes.length);
    new Uint8Array(this.memory.buffer, ptr, bytes.length).set(bytes);
    return [ptr, bytes.length];
  }

  private writeBytes(bytes: Uint8Array): [ptr: number, len: number] {
    if (bytes.length === 0) return [0, 0];
    const ptr = this.exports.alloc(bytes.length);
    new Uint8Array(this.memory.buffer, ptr, bytes.length).set(bytes);
    return [ptr, bytes.length];
  }

  private readString(ptr: number, len: number): string {
    return decoder.decode(new Uint8Array(this.memory.buffer, ptr, len));
  }

  private readBytes(ptr: number, len: number): Uint8Array {
    // .slice() copies out of WASM memory into a fresh JS-owned buffer so
    // the result remains valid after subsequent allocations that might
    // detach or grow the underlying ArrayBuffer.
    return new Uint8Array(this.memory.buffer, ptr, len).slice();
  }

  private dealloc(ptr: number, len: number): void {
    if (len > 0) this.exports.dealloc(ptr, len);
  }

  private _callInit(
    policyJson: string,
    agentId: string,
    privateKey: Uint8Array,
    instanceId: string,
  ): number {
    const [pPtr, pLen] = this.writeString(policyJson);
    const [aPtr, aLen] = this.writeString(agentId);
    const [kPtr, kLen] = this.writeBytes(privateKey);
    const [iPtr, iLen] = this.writeString(instanceId);
    try {
      return this.exports.init(pPtr, pLen, aPtr, aLen, kPtr, kLen, iPtr, iLen);
    } finally {
      this.dealloc(pPtr, pLen);
      this.dealloc(aPtr, aLen);
      this.dealloc(kPtr, kLen);
      this.dealloc(iPtr, iLen);
    }
  }

  // -------------------------------------------------------------------
  // Evaluation
  // -------------------------------------------------------------------

  /** Evaluate a request against the loaded policy. Synchronous. */
  evaluate(req: EvaluateRequest): EvalResult {
    // Always include trace_id / span_id / parent_span_id as explicit
    // null when absent — the WASM core's serde contract requires them
    // present in every request JSON, unlike JS's usual omit-undefined
    // JSON.stringify behavior.
    const payload = {
      request_id: req.request_id,
      method: req.method,
      url: req.url,
      headers: req.headers,
      body: req.body,
      timestamp: req.timestamp,
      timestamp_ms: req.timestamp_ms,
      trace_id: req.trace_id ?? "",
      span_id: req.span_id ?? "",
      parent_span_id: req.parent_span_id ?? "",
    };
    const json = JSON.stringify(payload);
    const [ptr, len] = this.writeString(json);
    let packed: bigint;
    try {
      packed = this.exports.evaluate_request(ptr, len);
    } finally {
      this.dealloc(ptr, len);
    }
    const [outPtr, outLen] = unpack(packed);
    if (outPtr === 0 || outLen === 0) {
      throw new CheckrdInitError("WASM evaluate_request returned null");
    }
    const resultJson = this.readString(outPtr, outLen);
    this.dealloc(outPtr, outLen);
    // Normalize WASM shape → EvalResult:
    //   - field rename: `log_event_json` (WASM) → `telemetry_json` (JS).
    //   - nullable → optional: serde-null becomes undefined for JS
    //     ergonomics (`.deny_reason` is `string | undefined`, not
    //     `string | null | undefined`).
    let raw: {
      allowed: boolean;
      deny_reason?: string | null;
      log_event_json?: string;
      request_id: string;
    };
    try {
      raw = JSON.parse(resultJson) as typeof raw;
    } catch (err) {
      // Truncate to keep the error message bounded; the original
      // SyntaxError survives via `cause` for full diagnosis.
      const snippet =
        resultJson.length > 200 ? `${resultJson.slice(0, 200)}…` : resultJson;
      throw new CheckrdInitError(
        `WASM evaluate_request returned malformed JSON (${snippet})`,
        { cause: err },
      );
    }
    const result: EvalResult = {
      allowed: raw.allowed,
      telemetry_json: raw.log_event_json ?? "",
      request_id: raw.request_id,
    };
    if (raw.deny_reason !== null && raw.deny_reason !== undefined) {
      result.deny_reason = raw.deny_reason;
    }
    return result;
  }

  // -------------------------------------------------------------------
  // Kill switch
  // -------------------------------------------------------------------

  /** Toggle the kill switch. When active, all requests are denied. */
  setKillSwitch(active: boolean): void {
    this.exports.set_kill_switch(active ? 1 : 0);
  }

  // -------------------------------------------------------------------
  // Signing
  // -------------------------------------------------------------------

  /** Ed25519-sign an arbitrary payload. Returns null in anonymous mode. */
  sign(payload: Uint8Array): Uint8Array | null {
    const [ptr, len] = this.writeBytes(payload);
    let packed: bigint;
    try {
      packed = this.exports.sign(ptr, len);
    } finally {
      this.dealloc(ptr, len);
    }
    if (packed === 0n) return null;
    const [outPtr, outLen] = unpack(packed);
    const sig = this.readBytes(outPtr, outLen);
    this.dealloc(outPtr, outLen);
    return sig;
  }

  /**
   * RFC 9421 + DSSE sign a telemetry batch. Returns null in anonymous mode.
   *
   * `created` and `expires` are Unix-seconds timestamps injected by the
   * caller — the WASM core has no clock of its own. `nonce` is a random
   * hex string for replay protection.
   */
  signTelemetryBatch(opts: {
    batchJson: Uint8Array;
    targetUri: string;
    signerAgent: string;
    nonce: string;
    created: number;
    expires: number;
  }): SignedBatch | null {
    const [bPtr, bLen] = this.writeBytes(opts.batchJson);
    const [uPtr, uLen] = this.writeString(opts.targetUri);
    const [aPtr, aLen] = this.writeString(opts.signerAgent);
    const [nPtr, nLen] = this.writeString(opts.nonce);
    let packed: bigint;
    try {
      packed = this.exports.sign_telemetry_batch(
        bPtr,
        bLen,
        uPtr,
        uLen,
        aPtr,
        aLen,
        nPtr,
        nLen,
        BigInt(opts.created),
        BigInt(opts.expires),
      );
    } finally {
      this.dealloc(bPtr, bLen);
      this.dealloc(uPtr, uLen);
      this.dealloc(aPtr, aLen);
      this.dealloc(nPtr, nLen);
    }
    if (packed === 0n) return null;
    const [outPtr, outLen] = unpack(packed);
    const json = this.readString(outPtr, outLen);
    this.dealloc(outPtr, outLen);
    try {
      return JSON.parse(json) as SignedBatch;
    } catch (err) {
      const snippet = json.length > 200 ? `${json.slice(0, 200)}…` : json;
      throw new CheckrdInitError(
        `WASM sign_telemetry_batch returned malformed JSON (${snippet})`,
        { cause: err },
      );
    }
  }

  // -------------------------------------------------------------------
  // Policy reload
  // -------------------------------------------------------------------

  /** Hot-reload the policy (unsigned path, used by Tier 3 file watchers). */
  reloadPolicy(policyJson: string): void {
    const [ptr, len] = this.writeString(policyJson);
    let rc: number;
    try {
      rc = this.exports.reload_policy(ptr, len);
    } finally {
      this.dealloc(ptr, len);
    }
    if (rc !== 0) {
      throw new CheckrdInitError(`reload_policy failed (${rc.toString()})`);
    }
  }

  /** Hot-reload a DSSE-signed policy bundle with full verification. */
  reloadPolicySigned(opts: {
    envelopeJson: string;
    trustedKeysJson: string;
    nowUnixSecs: number;
    maxAgeSecs: number;
  }): void {
    const [ePtr, eLen] = this.writeString(opts.envelopeJson);
    const [kPtr, kLen] = this.writeString(opts.trustedKeysJson);
    let rc: number;
    try {
      rc = this.exports.reload_policy_signed(
        ePtr,
        eLen,
        kPtr,
        kLen,
        BigInt(opts.nowUnixSecs),
        BigInt(opts.maxAgeSecs),
      );
    } finally {
      this.dealloc(ePtr, eLen);
      this.dealloc(kPtr, kLen);
    }
    if (rc !== 0) throw new PolicySignatureError(rc);
  }

  /** Monotonic policy-version counter. 0 = no signed bundle installed. */
  getActivePolicyVersion(): number {
    return Number(this.exports.get_active_policy_version());
  }

  /** One-shot restore of a persisted policy version across process restarts. */
  setInitialPolicyVersion(version: number): void {
    const rc = this.exports.set_initial_policy_version(BigInt(version));
    if (rc !== 0) throw new PolicySignatureError(rc);
  }

  // -------------------------------------------------------------------
  // Static key utilities
  // -------------------------------------------------------------------

  /** Generate a fresh Ed25519 keypair. Does not require engine init. */
  static generateKeypair(): Keypair {
    const exports = getUtilExports();
    const packed = exports.generate_keypair();
    const [ptr, len] = unpack(packed);
    if (len !== 64) {
      throw new CheckrdInitError(
        `generate_keypair expected 64 bytes, got ${len.toString()}`,
      );
    }
    const both = new Uint8Array(exports.memory.buffer, ptr, 64).slice();
    exports.dealloc(ptr, len);
    return {
      privateKey: both.slice(0, 32),
      publicKey: both.slice(32, 64),
    };
  }

  /** Derive the public key from a 32-byte Ed25519 private key. */
  static derivePublicKey(privateKey: Uint8Array): Uint8Array {
    if (privateKey.length !== 32) {
      throw new CheckrdInitError(
        `derive_public_key expected 32 bytes, got ${privateKey.length.toString()}`,
      );
    }
    const exports = getUtilExports();
    const ptr = exports.alloc(32);
    new Uint8Array(exports.memory.buffer, ptr, 32).set(privateKey);
    let packed: bigint;
    try {
      packed = exports.derive_public_key(ptr, 32);
    } finally {
      exports.dealloc(ptr, 32);
    }
    if (packed === 0n) {
      throw new CheckrdInitError("derive_public_key failed");
    }
    const [outPtr, outLen] = unpack(packed);
    const pub = new Uint8Array(exports.memory.buffer, outPtr, outLen).slice();
    exports.dealloc(outPtr, outLen);
    return pub;
  }
}
