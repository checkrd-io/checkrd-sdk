/**
 * One-time stderr banner fired when debug logging is enabled.
 *
 * When ``CHECKRD_DEBUG=1`` or ``debug: true`` is passed to any Checkrd
 * entry point, the SDK emits a loud stderr banner the first time per
 * process. Checkrd sits in the request path for LLM agent traffic, so
 * debug logs here can contain prompt payloads — customer data that
 * most teams don't expect to find in their log aggregator.
 *
 * Parallel to the Python SDK's
 * ``tests/test_debug_pii_warning.py`` — when the rule changes in one,
 * it changes in both.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  __resetDebugWarningForTesting,
  warnDebugPiiRisk,
} from "../src/_logger.js";

describe("warnDebugPiiRisk", () => {
  let writes: string[];

  beforeEach(() => {
    __resetDebugWarningForTesting();
    writes = [];
  });

  it("writes a message on first call", () => {
    warnDebugPiiRisk({ writeStderr: (s) => writes.push(s) });
    expect(writes.length).toBe(1);
    expect(writes[0]).toContain("DEBUG logging is enabled");
  });

  it("mentions CHECKRD_DEBUG so operators can grep for it", () => {
    warnDebugPiiRisk({ writeStderr: (s) => writes.push(s) });
    expect(writes[0]).toContain("CHECKRD_DEBUG");
  });

  it("mentions production explicitly", () => {
    warnDebugPiiRisk({ writeStderr: (s) => writes.push(s) });
    expect((writes[0] ?? "").toLowerCase()).toContain("production");
  });

  it("fires once per process by default", () => {
    // The guard is the whole point — operators should see the banner
    // once, not on every wrap() call in a loop.
    warnDebugPiiRisk({ writeStderr: (s) => writes.push(s) });
    warnDebugPiiRisk({ writeStderr: (s) => writes.push(s) });
    warnDebugPiiRisk({ writeStderr: (s) => writes.push(s) });
    expect(writes.length).toBe(1);
  });

  it("once=false bypasses the guard (testing escape hatch)", () => {
    warnDebugPiiRisk({ writeStderr: (s) => writes.push(s), once: false });
    warnDebugPiiRisk({ writeStderr: (s) => writes.push(s), once: false });
    expect(writes.length).toBe(2);
  });

  it("__resetDebugWarningForTesting re-arms the guard", () => {
    warnDebugPiiRisk({ writeStderr: (s) => writes.push(s) });
    warnDebugPiiRisk({ writeStderr: (s) => writes.push(s) }); // suppressed
    __resetDebugWarningForTesting();
    warnDebugPiiRisk({ writeStderr: (s) => writes.push(s) }); // fires again
    expect(writes.length).toBe(2);
  });

  it("falls back to console.warn when stderr is unavailable", () => {
    // Simulate a runtime without `process.stderr` (Cloudflare Workers,
    // Vercel Edge). The banner must still land somewhere visible.
    const originalProcess = (globalThis as { process?: unknown }).process;
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    try {
      (globalThis as { process?: unknown }).process = undefined;
      __resetDebugWarningForTesting();
      warnDebugPiiRisk(); // no writeStderr override — forces default path
      expect(warn).toHaveBeenCalled();
      const firstCall = warn.mock.calls[0];
      expect(firstCall).toBeDefined();
      const firstCallArg = firstCall?.[0] as string;
      expect(firstCallArg).toContain("DEBUG logging is enabled");
    } finally {
      (globalThis as { process?: unknown }).process = originalProcess;
      warn.mockRestore();
    }
  });
});

describe("initPrelude debug warning integration", () => {
  let writes: string[];
  let originalWrite: typeof process.stderr.write;

  beforeEach(() => {
    __resetDebugWarningForTesting();
    writes = [];
    // Monkey-patch process.stderr.write so the default sink gets routed
    // into the test's write list. Direct replacement avoids the vitest
    // spy generic-typing issue with the variadic stderr.write overload.
    originalWrite = process.stderr.write.bind(process.stderr);
    process.stderr.write = ((chunk: unknown): boolean => {
      writes.push(String(chunk));
      return true;
    }) as typeof process.stderr.write;
  });

  afterEach(() => {
    process.stderr.write = originalWrite;
    delete process.env.CHECKRD_DEBUG;
    delete process.env.CHECKRD_DISABLED;
  });

  it("fires when init receives debug=true", async () => {
    // `init` is the production entry point. We short-circuit via
    // `CHECKRD_DISABLED=1` so the test doesn't actually try to load
    // the WASM engine — the banner is wired to fire before the
    // disabled check precisely so this path still warns.
    process.env.CHECKRD_DISABLED = "1";
    const { init } = await import("../src/index.js");
    init({ debug: true, agentId: "test" });
    expect(writes.join("")).toContain("DEBUG logging is enabled");
  });

  it("does NOT fire when debug is false", async () => {
    process.env.CHECKRD_DISABLED = "1";
    const { init } = await import("../src/index.js");
    init({ debug: false, agentId: "test" });
    expect(writes.join("")).not.toContain("DEBUG logging is enabled");
  });

  it("fires when CHECKRD_DEBUG=1 is set in the environment", async () => {
    process.env.CHECKRD_DEBUG = "1";
    process.env.CHECKRD_DISABLED = "1";
    const { init } = await import("../src/index.js");
    init({ agentId: "test" }); // no explicit debug — env wins
    expect(writes.join("")).toContain("DEBUG logging is enabled");
  });
});
