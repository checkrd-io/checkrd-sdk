/**
 * Graceful shutdown registry. Tracks disposables registered by `init()`
 * so a single {@link shutdownAll} call can drain them in order.
 *
 * On Node, we also wire a one-shot SIGTERM + SIGINT listener and a
 * `beforeExit` hook so containerized deployments get a best-effort
 * flush before the process dies. Other runtimes (Cloudflare Workers,
 * Vercel Edge, the browser) either don't have long-running processes
 * (Workers end with the request) or expose `ctx.waitUntil` — which the
 * caller orchestrates, since we can't hook it globally.
 */

/** Anything with an async `close()` method qualifies as a disposable. */
export interface Disposable {
  close(): Promise<void>;
}

const disposables = new Set<Disposable>();
let handlersInstalled = false;
let inFlightShutdown: Promise<void> | null = null;

/**
 * Register a disposable. The shutdown routine will close it in reverse
 * registration order, mirroring Python's `atexit.register` LIFO.
 */
export function registerDisposable(d: Disposable): void {
  disposables.add(d);
  installProcessHandlersOnce();
}

/** Drop a disposable from the registry (e.g., because it already closed). */
export function unregisterDisposable(d: Disposable): void {
  disposables.delete(d);
}

/**
 * Drain every registered disposable with a bounded total timeout.
 * Repeated calls coalesce onto the same promise — safe under concurrent
 * SIGTERM + user-initiated shutdown.
 */
export function shutdownAll(totalTimeoutMs = 5_000): Promise<void> {
  if (inFlightShutdown) return inFlightShutdown;
  inFlightShutdown = drain(totalTimeoutMs).finally(() => {
    inFlightShutdown = null;
  });
  return inFlightShutdown;
}

/** Clear the registry without closing anything. Test-only. */
export function _resetShutdownForTests(): void {
  disposables.clear();
  inFlightShutdown = null;
}

async function drain(totalTimeoutMs: number): Promise<void> {
  const items = Array.from(disposables).reverse();
  disposables.clear();
  const deadline = Date.now() + totalTimeoutMs;
  const tasks = items.map(async (d) => {
    const remaining = Math.max(0, deadline - Date.now());
    try {
      await withTimeout(d.close(), remaining);
    } catch {
      // Best-effort shutdown — we never throw out of here because the
      // typical caller is a signal handler and the process is exiting
      // anyway.
    }
  });
  await Promise.all(tasks);
}

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  if (ms <= 0) return promise;
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new Error(`shutdown timeout after ${ms.toString()}ms`));
    }, ms);
    promise.then(
      (v) => {
        clearTimeout(timer);
        resolve(v);
      },
      (err: unknown) => {
        clearTimeout(timer);
        reject(err instanceof Error ? err : new Error(String(err)));
      },
    );
  });
}

function installProcessHandlersOnce(): void {
  if (handlersInstalled) return;
  handlersInstalled = true;
  // Only run on Node. `process.on` exists in Node and Bun; not Cloudflare.
  const proc = (globalThis as unknown as { process?: NodeJS.Process }).process;
  if (!proc || typeof proc.on !== "function") return;

  const handler = (): void => {
    void shutdownAll().catch(() => {
      // swallow — we're exiting
    });
  };
  // `once` avoids leaking listeners if the user re-inits the SDK in tests.
  proc.once("SIGTERM", handler);
  proc.once("SIGINT", handler);
  proc.once("beforeExit", handler);
}
