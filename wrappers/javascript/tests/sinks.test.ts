import { describe, expect, it, vi } from "vitest";

import {
  CompositeSink,
  ConsoleSink,
  ControlPlaneSink,
  type TelemetrySink,
} from "../src/sinks.js";

describe("ConsoleSink", () => {
  it("forwards events to the provided logger at the configured level", () => {
    const logger = {
      debug: vi.fn(),
      info: vi.fn(),
      warn: vi.fn(),
      error: vi.fn(),
    };
    const sink = new ConsoleSink({ logger, level: "debug" });
    sink.enqueue({ hello: "world" });
    expect(logger.debug).toHaveBeenCalledWith("checkrd event", { hello: "world" });
  });

  it("close() is a no-op that resolves immediately", async () => {
    const sink = new ConsoleSink({
      logger: {
        debug: vi.fn(),
        info: vi.fn(),
        warn: vi.fn(),
        error: vi.fn(),
      },
    });
    await expect(sink.close()).resolves.toBeUndefined();
  });
});

describe("CompositeSink", () => {
  it("broadcasts each event to every underlying sink", () => {
    const a: TelemetrySink = {
      enqueue: vi.fn(),
      close: vi.fn(async () => undefined),
    };
    const b: TelemetrySink = {
      enqueue: vi.fn(),
      close: vi.fn(async () => undefined),
    };
    const sink = new CompositeSink([a, b]);
    sink.enqueue({ n: 1 });
    expect(a.enqueue).toHaveBeenCalledWith({ n: 1 });
    expect(b.enqueue).toHaveBeenCalledWith({ n: 1 });
  });

  it("close() closes every underlying sink (even if one rejects)", async () => {
    const a: TelemetrySink = {
      enqueue: vi.fn(),
      close: vi.fn(async () => { throw new Error("a failed"); }),
    };
    const b: TelemetrySink = {
      enqueue: vi.fn(),
      close: vi.fn(async () => undefined),
    };
    const sink = new CompositeSink([a, b]);
    await sink.close();
    expect(a.close).toHaveBeenCalled();
    expect(b.close).toHaveBeenCalled();
  });
});

describe("ControlPlaneSink", () => {
  it("delegates enqueue + close to the batcher", async () => {
    const batcher = {
      enqueue: vi.fn(),
      stop: vi.fn(async () => undefined),
    };
    const sink = new ControlPlaneSink(batcher as unknown as import("../src/batcher.js").TelemetryBatcher);
    sink.enqueue({ n: 1 });
    await sink.close();
    expect(batcher.enqueue).toHaveBeenCalledWith({ n: 1 });
    expect(batcher.stop).toHaveBeenCalled();
  });
});
