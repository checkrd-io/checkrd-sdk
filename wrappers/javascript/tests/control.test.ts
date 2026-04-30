import { describe, expect, it, vi } from "vitest";

import {
  DEFAULT_DENY_POLICY_JSON,
  handleControlEvent,
  type ControlEngine,
  type ControlLogger,
} from "../src/control.js";
import { CheckrdInitError } from "../src/exceptions.js";

// The dispatcher contract MUST stay in sync with
// `wrappers/python/src/checkrd/control.py::ControlReceiver._handle_event`.
// If you change behavior here, update the Python wrapper and its tests too.

function makeEngine(): ControlEngine & {
  setKillSwitch: ReturnType<typeof vi.fn>;
  reloadPolicy: ReturnType<typeof vi.fn>;
} {
  return {
    setKillSwitch: vi.fn(),
    reloadPolicy: vi.fn(),
  };
}

function makeLogger(): ControlLogger & {
  warn: ReturnType<typeof vi.fn>;
  error: ReturnType<typeof vi.fn>;
} {
  return {
    warn: vi.fn(),
    error: vi.fn(),
  };
}

describe("handleControlEvent — kill_switch", () => {
  it("toggles the engine on when active=true", () => {
    const engine = makeEngine();
    const handled = handleControlEvent(
      engine,
      "kill_switch",
      JSON.stringify({ active: true }),
      makeLogger(),
    );
    expect(handled).toBe(true);
    expect(engine.setKillSwitch).toHaveBeenCalledWith(true);
  });

  it("toggles the engine off when active=false", () => {
    const engine = makeEngine();
    handleControlEvent(
      engine,
      "kill_switch",
      JSON.stringify({ active: false }),
      makeLogger(),
    );
    expect(engine.setKillSwitch).toHaveBeenCalledWith(false);
  });

  it("drops the event with a warning when `active` is missing", () => {
    const engine = makeEngine();
    const logger = makeLogger();
    handleControlEvent(
      engine,
      "kill_switch",
      JSON.stringify({ wrong_field: true }),
      logger,
    );
    expect(engine.setKillSwitch).not.toHaveBeenCalled();
    expect(logger.warn).toHaveBeenCalled();
  });
});

describe("handleControlEvent — init", () => {
  it("applies the kill switch from initial state", () => {
    const engine = makeEngine();
    handleControlEvent(
      engine,
      "init",
      JSON.stringify({ kill_switch_active: true }),
      makeLogger(),
    );
    expect(engine.setKillSwitch).toHaveBeenCalledWith(true);
  });

  it("treats missing kill_switch_active as false", () => {
    const engine = makeEngine();
    handleControlEvent(engine, "init", JSON.stringify({}), makeLogger());
    expect(engine.setKillSwitch).toHaveBeenCalledWith(false);
  });
});

describe("handleControlEvent — policy_updated", () => {
  it("logs a warning and does NOT install anything without PolicyUpdateOptions", () => {
    const engine = makeEngine();
    const logger = makeLogger();
    handleControlEvent(
      engine,
      "policy_updated",
      JSON.stringify({ version: 3, policy_envelope: { foo: "bar" } }),
      logger,
    );
    expect(engine.reloadPolicy).not.toHaveBeenCalled();
    expect(engine.setKillSwitch).not.toHaveBeenCalled();
    expect(logger.warn).toHaveBeenCalled();
  });

  it("calls reloadPolicySigned with the trusted-keys JSON when wired up", async () => {
    const reloadSigned = vi.fn();
    const engine = {
      ...makeEngine(),
      reloadPolicySigned: reloadSigned,
      getActivePolicyVersion: vi.fn(() => 42),
    };
    const onInstalled = vi.fn();
    handleControlEvent(
      engine,
      "policy_updated",
      JSON.stringify({ policy_envelope: { signatures: ["x"] } }),
      makeLogger(),
      {
        loadTrustedKeys: () => "{\"keys\":[]}",
        maxAgeSecs: 600,
        nowUnixSecs: () => 1_700_000_000,
        onInstalled,
      },
    );
    // installSignedPolicy is fire-and-forget; wait a tick.
    await new Promise((r) => setTimeout(r, 10));
    expect(reloadSigned).toHaveBeenCalledWith(
      expect.objectContaining({
        maxAgeSecs: 600,
        nowUnixSecs: 1_700_000_000,
        trustedKeysJson: "{\"keys\":[]}",
      }),
    );
    // No `hash` / `active_policy_hash` in this event payload, so the
    // server-trusted hash is null. The SDK does not synthesize one
    // (SHA-256 of the DSSE payload is not the same digest as the
    // server's SHA-256 of the source YAML).
    expect(onInstalled).toHaveBeenCalledWith(42, null);
  });

  it("logs PolicySignatureError without throwing when the bundle is rejected", async () => {
    const { PolicySignatureError } = await import("../src/exceptions.js");
    const reloadSigned = vi.fn(() => {
      throw new PolicySignatureError(-5); // signature_invalid
    });
    const logger = makeLogger();
    const engine = {
      ...makeEngine(),
      reloadPolicySigned: reloadSigned,
    };
    handleControlEvent(
      engine,
      "policy_updated",
      JSON.stringify({ policy_envelope: { signatures: ["x"] } }),
      logger,
      { loadTrustedKeys: () => "{}" },
    );
    await new Promise((r) => setTimeout(r, 10));
    expect(logger.error).toHaveBeenCalled();
  });
});

describe("handleControlEvent — policy_deactivated", () => {
  it("installs the locally constructed default-deny policy", () => {
    const engine = makeEngine();
    handleControlEvent(engine, "policy_deactivated", "{}", makeLogger());
    expect(engine.reloadPolicy).toHaveBeenCalledTimes(1);
    expect(engine.reloadPolicy).toHaveBeenCalledWith(DEFAULT_DENY_POLICY_JSON);
  });

  it("ignores the event payload (forwards-compat with future fields)", () => {
    const engine = makeEngine();
    handleControlEvent(
      engine,
      "policy_deactivated",
      JSON.stringify({ version: 7, extra: "ignored" }),
      makeLogger(),
    );
    expect(engine.reloadPolicy).toHaveBeenCalledWith(DEFAULT_DENY_POLICY_JSON);
  });

  it("logs and continues when the engine throws CheckrdInitError", () => {
    const engine = makeEngine();
    engine.reloadPolicy.mockImplementation(() => {
      throw new CheckrdInitError("engine broken");
    });
    const logger = makeLogger();

    expect(() =>
      handleControlEvent(engine, "policy_deactivated", "{}", logger),
    ).not.toThrow();
    expect(logger.error).toHaveBeenCalled();
  });

  it("re-raises non-CheckrdInitError exceptions (caller bug, not engine state)", () => {
    const engine = makeEngine();
    engine.reloadPolicy.mockImplementation(() => {
      throw new TypeError("programmer error");
    });
    expect(() =>
      handleControlEvent(engine, "policy_deactivated", "{}", makeLogger()),
    ).toThrow(TypeError);
  });

  it("guards against the worst regression — default-deny payload must say deny", () => {
    // If someone accidentally flips the JSON to `default: "allow"` with
    // empty rules, every retired policy would silently allow all traffic
    // until the next signed bundle. Hard-code the assertion.
    const installed = JSON.parse(DEFAULT_DENY_POLICY_JSON) as { default: string; rules: unknown[] };
    expect(installed.default).toBe("deny");
    expect(installed.rules).toEqual([]);
  });
});

describe("handleControlEvent — unknown events", () => {
  it("returns false for unknown event names without touching the engine", () => {
    const engine = makeEngine();
    const handled = handleControlEvent(engine, "future_event", "{}", makeLogger());
    expect(handled).toBe(false);
    expect(engine.setKillSwitch).not.toHaveBeenCalled();
    expect(engine.reloadPolicy).not.toHaveBeenCalled();
  });

  it("treats heartbeats (event=message) as unknown", () => {
    const engine = makeEngine();
    const handled = handleControlEvent(engine, "message", "heartbeat", makeLogger());
    expect(handled).toBe(false);
  });
});

describe("handleControlEvent — malformed JSON", () => {
  it("does not crash and does not mutate engine state", () => {
    const engine = makeEngine();
    const logger = makeLogger();
    expect(() =>
      handleControlEvent(engine, "kill_switch", "not json {{{", logger),
    ).not.toThrow();
    expect(engine.setKillSwitch).not.toHaveBeenCalled();
    expect(logger.warn).toHaveBeenCalled();
  });
});
