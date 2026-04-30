import { mkdtempSync, writeFileSync, rmSync, utimesSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PolicyFileWatcher, KillSwitchFileWatcher } from "../src/watchers.js";

let workdir: string;

beforeEach(() => {
  workdir = mkdtempSync(join(tmpdir(), "checkrd-watchers-"));
});
afterEach(() => {
  rmSync(workdir, { recursive: true, force: true });
});

describe("PolicyFileWatcher", () => {
  it("reloads the engine when mtime changes", async () => {
    const path = join(workdir, "policy.yaml");
    writeFileSync(path, "default: allow\nrules: []\n");
    const engine = { reloadPolicy: vi.fn(), setKillSwitch: vi.fn() };
    const watcher = new PolicyFileWatcher({
      path,
      engine,
      intervalMs: 10,
      loadConfig: (text) => JSON.stringify({ text }),
    });
    await watcher.start();
    try {
      // Bump mtime without changing content.
      writeFileSync(path, "default: deny\nrules: []\n");
      const future = Date.now() / 1000 + 60;
      utimesSync(path, future, future);
      for (let i = 0; i < 50; i++) {
        if (engine.reloadPolicy.mock.calls.length > 0) break;
        await new Promise((r) => setTimeout(r, 20));
      }
      expect(engine.reloadPolicy).toHaveBeenCalled();
    } finally {
      await watcher.stop();
    }
  });

  it("does NOT reload on the initial poll (primes mtime only)", async () => {
    const path = join(workdir, "policy.yaml");
    writeFileSync(path, "default: allow\nrules: []\n");
    const engine = { reloadPolicy: vi.fn(), setKillSwitch: vi.fn() };
    const watcher = new PolicyFileWatcher({
      path,
      engine,
      intervalMs: 100,
      loadConfig: (text) => text,
    });
    await watcher.start();
    try {
      await new Promise((r) => setTimeout(r, 30));
      expect(engine.reloadPolicy).not.toHaveBeenCalled();
    } finally {
      await watcher.stop();
    }
  });

  it("keeps previous policy if the new file fails to parse", async () => {
    const path = join(workdir, "policy.yaml");
    writeFileSync(path, "default: allow\nrules: []\n");
    const engine = { reloadPolicy: vi.fn(), setKillSwitch: vi.fn() };
    const watcher = new PolicyFileWatcher({
      path,
      engine,
      intervalMs: 10,
      loadConfig: () => { throw new Error("parse failure"); },
    });
    await watcher.start();
    try {
      writeFileSync(path, "garbled { { {");
      const future = Date.now() / 1000 + 60;
      utimesSync(path, future, future);
      await new Promise((r) => setTimeout(r, 80));
      expect(engine.reloadPolicy).not.toHaveBeenCalled();
    } finally {
      await watcher.stop();
    }
  });
});

describe("KillSwitchFileWatcher", () => {
  it("activates when the file exists and deactivates when removed", async () => {
    const path = join(workdir, "ks");
    const engine = { reloadPolicy: vi.fn(), setKillSwitch: vi.fn() };
    const watcher = new KillSwitchFileWatcher({
      path,
      engine,
      intervalMs: 10,
    });
    await watcher.start();
    try {
      writeFileSync(path, "");
      for (let i = 0; i < 50; i++) {
        if (engine.setKillSwitch.mock.calls.some((c: unknown[]) => c[0] === true)) break;
        await new Promise((r) => setTimeout(r, 20));
      }
      expect(engine.setKillSwitch).toHaveBeenCalledWith(true);
      unlinkSync(path);
      let sawDeactivate = false;
      for (let i = 0; i < 50; i++) {
        const calls = engine.setKillSwitch.mock.calls.map((c: unknown[]) => c[0]);
        if (calls.includes(false)) {
          sawDeactivate = true;
          break;
        }
        await new Promise((r) => setTimeout(r, 20));
      }
      expect(sawDeactivate).toBe(true);
    } finally {
      await watcher.stop();
    }
  });
});
