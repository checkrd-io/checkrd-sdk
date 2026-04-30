import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { resolve } from "../src/_settings.js";

const ENV_KEYS = [
  "CHECKRD_AGENT_ID",
  "CHECKRD_BASE_URL",
  "CHECKRD_API_KEY",
  "CHECKRD_ENFORCE",
  "CHECKRD_DISABLED",
  "CHECKRD_DEBUG",
  "CHECKRD_SECURITY_MODE",
  "VERCEL_URL",
  "CF_PAGES_URL",
  "FLY_APP_NAME",
  "K_SERVICE",
  "AWS_LAMBDA_FUNCTION_NAME",
  "KUBERNETES_POD_NAME",
];

const saved: Record<string, string | undefined> = {};

beforeEach(() => {
  for (const k of ENV_KEYS) {
    saved[k] = process.env[k];
    delete process.env[k];
  }
});
afterEach(() => {
  for (const [k, v] of Object.entries(saved)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
});

describe("resolve()", () => {
  it("uses explicit agentId when provided", () => {
    const s = resolve({ agentId: "my-agent" });
    expect(s.agentId).toBe("my-agent");
  });

  it("falls back to CHECKRD_AGENT_ID env var", () => {
    process.env["CHECKRD_AGENT_ID"] = "env-agent";
    expect(resolve().agentId).toBe("env-agent");
  });

  it("derives from platform env vars when no explicit agent is set", () => {
    process.env["VERCEL_URL"] = "my-vercel-app";
    expect(resolve().agentId).toBe("my-vercel-app");
  });

  it("Lambda env takes precedence over script+host fallback", () => {
    process.env["AWS_LAMBDA_FUNCTION_NAME"] = "my-lambda";
    expect(resolve().agentId).toBe("my-lambda");
  });

  it("detects strict security mode as default", () => {
    expect(resolve().securityMode).toBe("strict");
  });

  it("honors CHECKRD_SECURITY_MODE=permissive", () => {
    process.env["CHECKRD_SECURITY_MODE"] = "permissive";
    expect(resolve().securityMode).toBe("permissive");
  });

  it("CHECKRD_DISABLED=1 flags the SDK as disabled", () => {
    process.env["CHECKRD_DISABLED"] = "1";
    expect(resolve().disabled).toBe(true);
  });

  it("CHECKRD_ENFORCE=1 sets explicit enforce=true", () => {
    process.env["CHECKRD_ENFORCE"] = "1";
    expect(resolve().enforceOverride).toBe(true);
  });

  it("CHECKRD_ENFORCE=0 sets explicit enforce=false", () => {
    process.env["CHECKRD_ENFORCE"] = "0";
    expect(resolve().enforceOverride).toBe(false);
  });

  it("hasControlPlane requires both url and key", () => {
    expect(resolve().hasControlPlane).toBe(false);
    process.env["CHECKRD_BASE_URL"] = "http://localhost:8080";
    expect(resolve().hasControlPlane).toBe(false);
    process.env["CHECKRD_API_KEY"] = "ck_test_x";
    expect(resolve().hasControlPlane).toBe(true);
  });
});

describe("control-plane URL validation (SSRF defense)", () => {
  // The global test setup sets `CHECKRD_ALLOW_INSECURE_HTTP=1` so other
  // suites can exercise `http://localhost` control-plane URLs. Override
  // here because the validation-behavior we want to test is the
  // production default.
  let savedAllowInsecure: string | undefined;
  beforeEach(() => {
    savedAllowInsecure = process.env["CHECKRD_ALLOW_INSECURE_HTTP"];
    delete process.env["CHECKRD_ALLOW_INSECURE_HTTP"];
  });
  afterEach(() => {
    if (savedAllowInsecure !== undefined) {
      process.env["CHECKRD_ALLOW_INSECURE_HTTP"] = savedAllowInsecure;
    } else {
      delete process.env["CHECKRD_ALLOW_INSECURE_HTTP"];
    }
  });

  it("accepts a normal https URL and strips trailing slash", () => {
    const s = resolve({ controlPlaneUrl: "https://api.checkrd.io/", apiKey: "k" });
    expect(s.controlPlaneUrl).toBe("https://api.checkrd.io");
    expect(s.dashboardUrl).toBe("https://api.checkrd.io");
  });

  it("rejects non-http(s) schemes (file, javascript, gopher)", () => {
    for (const url of [
      "file:///etc/passwd",
      "javascript:alert(1)",
      "gopher://evil.example.com",
    ]) {
      expect(() => resolve({ controlPlaneUrl: url, apiKey: "k" })).toThrow();
    }
  });

  it("rejects http:// by default", () => {
    expect(() => resolve({ controlPlaneUrl: "http://api.checkrd.io", apiKey: "k" }))
      .toThrow(/must use https/);
  });

  it("accepts http:// when CHECKRD_ALLOW_INSECURE_HTTP=1", () => {
    process.env["CHECKRD_ALLOW_INSECURE_HTTP"] = "1";
    const s = resolve({ controlPlaneUrl: "http://localhost:8080", apiKey: "k" });
    expect(s.controlPlaneUrl).toBe("http://localhost:8080");
  });

  it("rejects SSRF-adjacent hosts by default (loopback, metadata service)", () => {
    for (const url of [
      "https://127.0.0.1",
      "https://localhost",
      "https://169.254.169.254/latest/meta-data/",
    ]) {
      expect(() => resolve({ controlPlaneUrl: url, apiKey: "k" })).toThrow();
    }
  });

  it("rejects malformed URL strings", () => {
    expect(() => resolve({ controlPlaneUrl: "not a url", apiKey: "k" }))
      .toThrow(/not a valid URL/);
  });
});
