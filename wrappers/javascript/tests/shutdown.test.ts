import { afterEach, describe, expect, it, vi } from "vitest";

import {
  _resetShutdownForTests,
  registerDisposable,
  shutdownAll,
  unregisterDisposable,
  type Disposable,
} from "../src/_shutdown.js";

afterEach(() => {
  _resetShutdownForTests();
});

describe("registerDisposable + shutdownAll", () => {
  it("closes every registered disposable", async () => {
    const a: Disposable = { close: vi.fn(async () => undefined) };
    const b: Disposable = { close: vi.fn(async () => undefined) };
    registerDisposable(a);
    registerDisposable(b);
    await shutdownAll();
    expect(a.close).toHaveBeenCalled();
    expect(b.close).toHaveBeenCalled();
  });

  it("continues when one disposable throws", async () => {
    const a: Disposable = { close: vi.fn(async () => { throw new Error("bad"); }) };
    const b: Disposable = { close: vi.fn(async () => undefined) };
    registerDisposable(a);
    registerDisposable(b);
    await expect(shutdownAll()).resolves.toBeUndefined();
    expect(b.close).toHaveBeenCalled();
  });

  it("respects the overall timeout", async () => {
    const slow: Disposable = {
      close: (): Promise<void> => new Promise<void>(() => undefined), // never resolves
    };
    registerDisposable(slow);
    const start = Date.now();
    await shutdownAll(100);
    expect(Date.now() - start).toBeLessThan(500);
  });

  it("unregisterDisposable removes an item from the registry", async () => {
    const a: Disposable = { close: vi.fn(async () => undefined) };
    registerDisposable(a);
    unregisterDisposable(a);
    await shutdownAll();
    expect(a.close).not.toHaveBeenCalled();
  });

  it("concurrent calls return the same in-flight promise", async () => {
    const close = vi.fn(async (): Promise<void> => {
      await new Promise<void>((r) => setTimeout(r, 20));
    });
    const a: Disposable = { close };
    registerDisposable(a);
    const [r1, r2] = await Promise.all([shutdownAll(), shutdownAll()]);
    expect(r1).toBeUndefined();
    expect(r2).toBeUndefined();
    expect(close).toHaveBeenCalledOnce();
  });
});
