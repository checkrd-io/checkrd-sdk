import { describe, expect, it, vi, type Mock } from "vitest";

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

type AnyFn = (...args: never[]) => unknown;

function makeEngine(): ControlEngine & {
  setKillSwitch: Mock<AnyFn>;
  reloadPolicy: Mock<AnyFn>;
} {
  return {
    setKillSwitch: vi.fn<AnyFn>(),
    reloadPolicy: vi.fn<AnyFn>(),
  };
}

type LogMethod = (message: string, ...args: unknown[]) => void;

function makeLogger(): ControlLogger & {
  warn: Mock<LogMethod>;
  error: Mock<LogMethod>;
} {
  return {
    warn: vi.fn<LogMethod>(),
    error: vi.fn<LogMethod>(),
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

describe("handleControlEvent — pricing_updated (fail-open cost metering)", () => {
  it("logs a warning and installs nothing without a pricing installer wired", () => {
    const engine = makeEngine();
    const logger = makeLogger();
    const handled = handleControlEvent(
      engine,
      "pricing_updated",
      JSON.stringify({ version: 3, pricing_envelope: { foo: "bar" } }),
      logger,
    );
    // Recognized event ⇒ true, but nothing installed and a warning fired.
    expect(handled).toBe(true);
    expect(logger.warn).toHaveBeenCalled();
  });

  it("calls reloadPricingSigned with the PRICING trusted-keys JSON when wired", async () => {
    const reloadPricing = vi.fn();
    const engine = {
      ...makeEngine(),
      reloadPricingSigned: reloadPricing,
      getActivePricingVersion: vi.fn(() => 7),
    };
    const onInstalled = vi.fn();
    handleControlEvent(
      engine,
      "pricing_updated",
      JSON.stringify({ pricing_envelope: { signatures: ["x"] } }),
      makeLogger(),
      undefined, // no policyUpdate
      {
        loadTrustedKeys: () => "{\"pricing_keys\":[]}",
        maxAgeSecs: 900,
        nowUnixSecs: () => 1_700_000_000,
        onInstalled,
      },
    );
    // installSignedPricing is fire-and-forget; wait a tick.
    await new Promise((r) => setTimeout(r, 10));
    expect(reloadPricing).toHaveBeenCalledWith(
      expect.objectContaining({
        maxAgeSecs: 900,
        nowUnixSecs: 1_700_000_000,
        trustedKeysJson: "{\"pricing_keys\":[]}",
      }),
    );
    // No `hash` / `active_pricing_hash` in the event, so the server-trusted
    // hash is null — the SDK never synthesizes one (SHA-256 of the DSSE
    // payload is not the server's SHA-256 of the canonical bundle).
    expect(onInstalled).toHaveBeenCalledWith(7, null);
  });

  it("does NOT touch the policy installer (isolation of the two paths)", async () => {
    const reloadPolicy = vi.fn();
    const reloadPricing = vi.fn();
    const engine = {
      ...makeEngine(),
      reloadPolicySigned: reloadPolicy,
      reloadPricingSigned: reloadPricing,
      getActivePricingVersion: vi.fn(() => 1),
    };
    handleControlEvent(
      engine,
      "pricing_updated",
      JSON.stringify({ pricing_envelope: { signatures: ["x"] } }),
      makeLogger(),
      { loadTrustedKeys: () => "POLICY_KEYS" },
      { loadTrustedKeys: () => "PRICING_KEYS" },
    );
    await new Promise((r) => setTimeout(r, 10));
    expect(reloadPricing).toHaveBeenCalledWith(
      expect.objectContaining({ trustedKeysJson: "PRICING_KEYS" }),
    );
    // The policy verifier is never called on a pricing event.
    expect(reloadPolicy).not.toHaveBeenCalled();
  });

  it("FAIL-OPEN: a rejected bundle logs a warning and does NOT throw or rethrow", async () => {
    const { PricingSignatureError } = await import("../src/exceptions.js");
    const reloadPricing = vi.fn(() => {
      throw new PricingSignatureError(-16); // signature_invalid
    });
    const logger = makeLogger();
    const engine = {
      ...makeEngine(),
      reloadPricingSigned: reloadPricing,
    };
    // handleControlEvent itself must not throw synchronously.
    expect(() =>
      handleControlEvent(
        engine,
        "pricing_updated",
        JSON.stringify({ pricing_envelope: { signatures: ["x"] } }),
        logger,
        undefined,
        { loadTrustedKeys: () => "{}" },
      ),
    ).not.toThrow();
    await new Promise((r) => setTimeout(r, 10));
    // Fail-open path logs a WARNING (not error), keeps running, and — the
    // headline — never surfaces the rejection to the host.
    expect(logger.warn).toHaveBeenCalled();
  });

  it("hash-cache: identical re-delivery skips the FFI install", async () => {
    const reloadPricing = vi.fn();
    const engine = {
      ...makeEngine(),
      reloadPricingSigned: reloadPricing,
      getActivePricingVersion: vi.fn(() => 4),
    };
    const HASH = "a".repeat(64);
    handleControlEvent(
      engine,
      "pricing_updated",
      JSON.stringify({ hash: HASH, pricing_envelope: { signatures: ["x"] } }),
      makeLogger(),
      undefined,
      { loadTrustedKeys: () => "{}", getLastHash: () => HASH },
    );
    await new Promise((r) => setTimeout(r, 10));
    // Incoming hash == last-installed hash ⇒ no-op, FFI never called.
    expect(reloadPricing).not.toHaveBeenCalled();
  });

  it("init also installs the pricing bundle when pricing_envelope is present", async () => {
    const reloadPricing = vi.fn();
    const engine = {
      ...makeEngine(),
      reloadPricingSigned: reloadPricing,
      getActivePricingVersion: vi.fn(() => 2),
    };
    handleControlEvent(
      engine,
      "init",
      JSON.stringify({
        kill_switch_active: false,
        pricing_envelope: { signatures: ["x"] },
        active_pricing_hash: "b".repeat(64),
      }),
      makeLogger(),
      undefined,
      { loadTrustedKeys: () => "PRICING_KEYS" },
    );
    await new Promise((r) => setTimeout(r, 10));
    expect(engine.setKillSwitch).toHaveBeenCalledWith(false);
    expect(reloadPricing).toHaveBeenCalledWith(
      expect.objectContaining({ trustedKeysJson: "PRICING_KEYS" }),
    );
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
