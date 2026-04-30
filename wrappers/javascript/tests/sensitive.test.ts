/**
 * Unit tests for the shared sensitive-headers / secret-keys module.
 * Anything that consumes header redaction or log redaction shares one
 * source of truth here — drift caused real bugs before the consolidation,
 * so the contract gets a dedicated test file.
 */
import { describe, expect, it } from "vitest";

import {
  isSensitiveHeader,
  REDACTED,
  SENSITIVE_HEADER_NAMES,
  SENSITIVE_KEY_NAMES,
} from "../src/_sensitive.js";

describe("SENSITIVE_HEADER_NAMES", () => {
  it("contains the standard auth + cookie headers", () => {
    expect(SENSITIVE_HEADER_NAMES.has("authorization")).toBe(true);
    expect(SENSITIVE_HEADER_NAMES.has("proxy-authorization")).toBe(true);
    expect(SENSITIVE_HEADER_NAMES.has("cookie")).toBe(true);
    expect(SENSITIVE_HEADER_NAMES.has("set-cookie")).toBe(true);
  });

  it("covers every vendor SDK auth header the wrappers patch", () => {
    expect(SENSITIVE_HEADER_NAMES.has("anthropic-api-key")).toBe(true);
    expect(SENSITIVE_HEADER_NAMES.has("openai-api-key")).toBe(true);
    expect(SENSITIVE_HEADER_NAMES.has("openai-organization")).toBe(true);
    expect(SENSITIVE_HEADER_NAMES.has("x-goog-api-key")).toBe(true);
  });

  it("covers Checkrd's own auth header in both casings", () => {
    expect(SENSITIVE_HEADER_NAMES.has("checkrd-api-key")).toBe(true);
    expect(SENSITIVE_HEADER_NAMES.has("x-checkrd-api-key")).toBe(true);
  });

  it("stores every name in lowercase", () => {
    for (const name of SENSITIVE_HEADER_NAMES) {
      expect(name).toBe(name.toLowerCase());
    }
  });
});

describe("isSensitiveHeader", () => {
  it("returns true for sensitive headers regardless of casing", () => {
    expect(isSensitiveHeader("Authorization")).toBe(true);
    expect(isSensitiveHeader("AUTHORIZATION")).toBe(true);
    expect(isSensitiveHeader("authorization")).toBe(true);
  });

  it("returns false for non-sensitive headers", () => {
    expect(isSensitiveHeader("Content-Type")).toBe(false);
    expect(isSensitiveHeader("Accept")).toBe(false);
    expect(isSensitiveHeader("User-Agent")).toBe(false);
  });

  it("returns false for empty string", () => {
    expect(isSensitiveHeader("")).toBe(false);
  });
});

describe("SENSITIVE_KEY_NAMES", () => {
  it("covers common JS/Python casings of secret object keys", () => {
    // JS conventions
    expect(SENSITIVE_KEY_NAMES.has("apiKey")).toBe(true);
    expect(SENSITIVE_KEY_NAMES.has("privateKey")).toBe(true);
    // Python conventions
    expect(SENSITIVE_KEY_NAMES.has("api_key")).toBe(true);
    expect(SENSITIVE_KEY_NAMES.has("private_key")).toBe(true);
    // Generic
    expect(SENSITIVE_KEY_NAMES.has("token")).toBe(true);
    expect(SENSITIVE_KEY_NAMES.has("password")).toBe(true);
    expect(SENSITIVE_KEY_NAMES.has("secret")).toBe(true);
    expect(SENSITIVE_KEY_NAMES.has("bearer")).toBe(true);
  });
});

describe("REDACTED", () => {
  it("is the canonical replacement string consumers can grep for", () => {
    expect(REDACTED).toBe("[REDACTED]");
  });
});
