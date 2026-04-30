/**
 * Wire `pagehide` / `beforeunload` listeners that trigger
 * {@link TelemetryBatcher.urgentFlush} so in-flight telemetry survives
 * a browser navigation, tab close, or Safari bfcache transition.
 *
 * # Why both events
 *
 *   - **`pagehide`** is the modern, reliable signal: it fires for
 *     ordinary navigation, tab close, AND when Safari/Chrome push the
 *     page into bfcache. iOS Safari does NOT fire `beforeunload`
 *     reliably, so without `pagehide` mobile users would silently
 *     lose their final batch.
 *   - **`beforeunload`** is a belt-and-suspenders for older browsers
 *     and for desktop refresh shortcuts that some browsers route
 *     through this event ahead of `pagehide`. Idempotency-Key on the
 *     POST means double-firing is harmless.
 *
 * Sentry's `BrowserClient`, PostHog, and Datadog RUM all wire these
 * two events; we follow the same pattern.
 *
 * # Why not `visibilitychange`
 *
 * `visibilitychange` fires on every tab-switch and minimization, not
 * just unload. Wiring `urgentFlush` to it would burn keepalive budget
 * on routine background/foreground transitions. The cost outweighs
 * the recovery benefit on typical agent workloads, where users are
 * not the ones triggering the request stream.
 *
 * # Idempotency considerations
 *
 * Each `urgentFlush` call generates its own request body and (via the
 * batcher's normal path) its own `Idempotency-Key`. The batcher's
 * regular `flush` could also fire a request milliseconds before the
 * unload handler — both are independent requests with independent
 * keys. The control plane dedupes on the SDK-level `event_id` inside
 * each event, so two parallel POSTs that share events still result in
 * one stored copy each.
 */

import type { TelemetryBatcher } from "./batcher.js";
import type { Logger } from "./_logger.js";

/** Result of {@link attachBrowserUnloadFlush}. Calling it removes the listeners. */
export type DetachBrowserFlush = () => void;

/** Options for {@link attachBrowserUnloadFlush}. */
export interface AttachBrowserFlushOptions {
  /**
   * Override the global `window` used for listener registration.
   * Test-only; production callers always use the default.
   */
  target?: EventTarget;
  /** Logger sink — only fires once at attach time, no per-event spam. */
  logger?: Logger;
}

/**
 * Attach `pagehide` + `beforeunload` listeners that trigger
 * {@link TelemetryBatcher.urgentFlush}. Returns a detach function.
 *
 * Calling this on a runtime without an EventTarget-shaped global
 * (Cloudflare Workers without `addEventListener`, Deno script mode,
 * etc.) is a no-op that returns a no-op detach function — safe to
 * unconditionally call from runtime-detection code.
 */
export function attachBrowserUnloadFlush(
  batcher: TelemetryBatcher,
  opts: AttachBrowserFlushOptions = {},
): DetachBrowserFlush {
  const target =
    opts.target ??
    ((globalThis as unknown as { window?: EventTarget }).window ?? null);
  if (target === null) {
    // No window-like global: silently treat as a no-op. This makes
    // the helper safe to call from `init()` without runtime branches.
    return () => undefined;
  }

  // Single shared listener so both events share the same closure —
  // de-duplicates if the browser fires both for one navigation
  // (some Chrome flows do). The batcher itself is responsible for
  // ignoring the second call when its queue is already empty.
  const handler = (): void => {
    try {
      batcher.urgentFlush();
    } catch (err) {
      // The unload path has no error channel left, but we still log
      // because dev-tools may show one final console line before the
      // page goes away.
      opts.logger?.warn("checkrd: urgent flush threw on unload", { err });
    }
  };

  target.addEventListener("pagehide", handler);
  target.addEventListener("beforeunload", handler);
  opts.logger?.debug(
    "checkrd: browser unload flush attached (pagehide + beforeunload)",
  );

  let detached = false;
  return (): void => {
    if (detached) return;
    detached = true;
    target.removeEventListener("pagehide", handler);
    target.removeEventListener("beforeunload", handler);
  };
}
