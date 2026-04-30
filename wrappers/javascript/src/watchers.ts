/**
 * File watchers for offline (Tier 3) deployments. Mirrors
 * ``wrappers/python/src/checkrd/watchers.py``.
 *
 * Node-only; both watchers lazy-import `node:fs` so the file stays
 * out of edge-runtime bundles that will never use it.
 *
 * Each watcher runs on an interval timer — no OS-native `fs.watch`
 * dependency because its behavior differs across macOS, Linux, and
 * network filesystems (CIFS / NFS). Polling mtime is the lowest-
 * common-denominator strategy.
 */

import type { WasmEngine } from "./engine.js";
import type { Logger } from "./_logger.js";

/** Minimum surface the watchers need from the engine. */
interface WatcherEngine {
  reloadPolicy(policyJson: string): void;
  setKillSwitch(active: boolean): void;
}

/** Options for {@link PolicyFileWatcher}. */
export interface PolicyFileWatcherOptions {
  /** Path to the policy file (YAML or JSON). */
  path: string;
  /** Engine whose policy should be hot-reloaded when the file changes. */
  engine: WatcherEngine;
  /** Polling interval in milliseconds. Default 5_000. */
  intervalMs?: number;
  /** Logger for diagnostic events. */
  logger?: Logger;
  /** Convert YAML/JSON text to canonical policy JSON. */
  loadConfig: (text: string) => string;
}

/**
 * Reloads the active policy when a YAML/JSON file on disk changes.
 *
 * The file is polled for `mtimeMs` changes; when it moves, the new
 * contents are loaded and passed through the supplied `loadConfig`
 * transform before reaching the engine. Malformed policies keep the
 * previous policy in force and log a warning — they never leave the
 * engine in a half-loaded state.
 */
export class PolicyFileWatcher {
  private readonly opts: Required<Pick<PolicyFileWatcherOptions, "intervalMs">> &
    PolicyFileWatcherOptions;
  private timer: ReturnType<typeof setInterval> | null = null;
  private lastMtime: number | null = null;
  private running = false;

  constructor(options: PolicyFileWatcherOptions) {
    this.opts = { intervalMs: 5_000, ...options };
  }

  async start(): Promise<void> {
    if (this.running) return;
    this.running = true;
    await this.pollOnce(); // prime mtime; don't reload on first tick
    this.timer = setInterval(() => {
      this.pollOnce().catch((err: unknown) => {
        this.opts.logger?.warn("policy watcher poll failed", { err });
      });
    }, this.opts.intervalMs);
    const t = this.timer as { unref?: () => void };
    if (typeof t.unref === "function") t.unref();
  }

  stop(): Promise<void> {
    this.running = false;
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
    return Promise.resolve();
  }

  private async pollOnce(): Promise<void> {
    const { stat, readFile } = await import("node:fs/promises");
    let mtimeMs: number;
    try {
      const s = await stat(this.opts.path);
      mtimeMs = s.mtimeMs;
    } catch (err) {
      this.opts.logger?.debug("policy file stat failed", {
        path: this.opts.path,
        err,
      });
      return;
    }
    if (this.lastMtime === null) {
      this.lastMtime = mtimeMs;
      return;
    }
    if (mtimeMs === this.lastMtime) return;
    this.lastMtime = mtimeMs;
    try {
      const text = await readFile(this.opts.path, "utf-8");
      const json = this.opts.loadConfig(text);
      this.opts.engine.reloadPolicy(json);
      this.opts.logger?.info("policy hot-reloaded from file", {
        path: this.opts.path,
      });
    } catch (err) {
      this.opts.logger?.warn(
        "policy hot-reload failed; previous policy remains in effect",
        { path: this.opts.path, err },
      );
    }
  }
}

/** Options for {@link KillSwitchFileWatcher}. */
export interface KillSwitchFileWatcherOptions {
  /** Path to the sentinel file. Present = kill switch active. */
  path: string;
  /** Engine whose kill switch should be toggled. */
  engine: WatcherEngine;
  /** Polling interval in milliseconds. Default 5_000. */
  intervalMs?: number;
  /** Logger for diagnostic events. */
  logger?: Logger;
}

/**
 * Toggles the engine's kill switch based on the existence of a file
 * on disk. Intended for air-gapped deployments where the control-
 * plane SSE stream is unavailable.
 *
 * `touch /var/lib/checkrd/killswitch` → switch active.
 * `rm /var/lib/checkrd/killswitch` → switch cleared.
 */
export class KillSwitchFileWatcher {
  private readonly opts: Required<Pick<KillSwitchFileWatcherOptions, "intervalMs">> &
    KillSwitchFileWatcherOptions;
  private timer: ReturnType<typeof setInterval> | null = null;
  private lastActive: boolean | null = null;
  private running = false;

  constructor(options: KillSwitchFileWatcherOptions) {
    this.opts = { intervalMs: 5_000, ...options };
  }

  async start(): Promise<void> {
    if (this.running) return;
    this.running = true;
    await this.pollOnce(); // apply initial state immediately
    this.timer = setInterval(() => {
      this.pollOnce().catch((err: unknown) => {
        this.opts.logger?.warn("kill-switch watcher poll failed", { err });
      });
    }, this.opts.intervalMs);
    const t = this.timer as { unref?: () => void };
    if (typeof t.unref === "function") t.unref();
  }

  stop(): Promise<void> {
    this.running = false;
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
    return Promise.resolve();
  }

  private async pollOnce(): Promise<void> {
    const { access, constants } = await import("node:fs/promises");
    let active: boolean;
    try {
      await access(this.opts.path, constants.F_OK);
      active = true;
    } catch {
      active = false;
    }
    if (active === this.lastActive) return;
    this.lastActive = active;
    this.opts.engine.setKillSwitch(active);
    this.opts.logger?.info("kill switch toggled via file watcher", {
      path: this.opts.path,
      active,
    });
  }
}

/** Re-export engine type so callers can wire the contract cleanly. */
export type { WasmEngine };
