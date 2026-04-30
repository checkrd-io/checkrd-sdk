import { describe, expect, it, vi } from "vitest";

import {
  createConsoleLogger,
  noopLogger,
  redactSensitive,
  resolveLogger,
  wrapWithRedaction,
  type Logger,
} from "../src/_logger.js";

describe("redactSensitive", () => {
  it("redacts sensitive header values in tuple lists", () => {
    const input: [string, string][] = [
      ["authorization", "Bearer sk-secret"],
      ["x-api-key", "secret-key"],
      ["content-type", "application/json"],
    ];
    const output = redactSensitive(input) as [string, string][];
    expect(output[0]).toEqual(["authorization", "[REDACTED]"]);
    expect(output[1]).toEqual(["x-api-key", "[REDACTED]"]);
    expect(output[2]).toEqual(["content-type", "application/json"]);
  });

  it("redacts sensitive keys inside objects recursively", () => {
    const input = {
      request: {
        headers: { Authorization: "Bearer secret" },
        apiKey: "sk-abc",
        nested: { password: "hunter2", safe: "ok" },
      },
    };
    const output = redactSensitive(input) as Record<string, unknown>;
    const req = output["request"] as Record<string, unknown>;
    expect((req["headers"] as Record<string, unknown>)["Authorization"]).toBe("[REDACTED]");
    expect(req["apiKey"]).toBe("[REDACTED]");
    const nested = req["nested"] as Record<string, unknown>;
    expect(nested["password"]).toBe("[REDACTED]");
    expect(nested["safe"]).toBe("ok");
  });

  it("preserves primitives unchanged", () => {
    expect(redactSensitive("hello")).toBe("hello");
    expect(redactSensitive(42)).toBe(42);
    expect(redactSensitive(null)).toBe(null);
  });

  it("stops at depth 4 to prevent unbounded recursion", () => {
    // Create a deeply nested object; deep values should still pass through
    // (no crash). The recursion-limit is a DoS safeguard, not a security
    // property — we only ensure it terminates.
    let deep: Record<string, unknown> = { password: "redact-me" };
    for (let i = 0; i < 20; i++) deep = { inner: deep };
    const output = redactSensitive(deep);
    expect(output).toBeTruthy();
  });
});

describe("createConsoleLogger", () => {
  it("suppresses messages below the threshold", () => {
    const spy = vi.spyOn(console, "debug").mockImplementation(() => undefined);
    const logger = createConsoleLogger("warn");
    logger.debug("should not show");
    expect(spy).not.toHaveBeenCalled();
    spy.mockRestore();
  });

  it("emits messages at or above the threshold", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const logger = createConsoleLogger("info");
    logger.error("boom", { requestId: "req-1" });
    expect(spy).toHaveBeenCalled();
    spy.mockRestore();
  });

  it("redacts sensitive attributes before reaching console", () => {
    const spy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const logger = createConsoleLogger("debug");
    logger.warn("denied", { headers: [["Authorization", "Bearer secret"]] });
    const call = spy.mock.calls[0];
    expect(JSON.stringify(call)).not.toContain("secret");
    expect(JSON.stringify(call)).toContain("REDACTED");
    spy.mockRestore();
  });
});

describe("wrapWithRedaction", () => {
  it("runs user-supplied logger payloads through redactSensitive", () => {
    const inner: Logger = {
      debug: vi.fn(),
      info: vi.fn(),
      warn: vi.fn(),
      error: vi.fn(),
    };
    const wrapped = wrapWithRedaction(inner);
    wrapped.info("audit", { api_key: "secret123" });
    const firstCall = (inner.info as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.stringify(firstCall)).not.toContain("secret123");
  });
});

describe("resolveLogger", () => {
  it("prefers explicit logger > logLevel > debug", () => {
    const custom: Logger = {
      debug: vi.fn(),
      info: vi.fn(),
      warn: vi.fn(),
      error: vi.fn(),
    };
    const logger = resolveLogger({ logger: custom });
    logger.info("test");
    expect(custom.info).toHaveBeenCalled();
  });

  it("respects CHECKRD_LOG_LEVEL env var", () => {
    process.env["CHECKRD_LOG_LEVEL"] = "error";
    try {
      const logger = resolveLogger({});
      const spy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
      logger.warn("ignored");
      expect(spy).not.toHaveBeenCalled();
      spy.mockRestore();
    } finally {
      delete process.env["CHECKRD_LOG_LEVEL"];
    }
  });
});

describe("noopLogger", () => {
  it("does nothing on every call", () => {
    expect(() => {
      noopLogger.debug("x");
      noopLogger.info("x");
      noopLogger.warn("x");
      noopLogger.error("x");
    }).not.toThrow();
  });
});
