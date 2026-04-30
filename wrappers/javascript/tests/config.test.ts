import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, describe, expect, it } from "vitest";

import { loadConfig } from "../src/config.js";
import { CheckrdInitError } from "../src/exceptions.js";

const tmp = mkdtempSync(join(tmpdir(), "checkrd-config-"));
afterAll(() => {
  rmSync(tmp, { recursive: true, force: true });
});

const SAMPLE_POLICY = { agent: "test", default: "allow", rules: [] };

describe("loadConfig", () => {
  it("accepts a plain object and returns canonical JSON", () => {
    const json = loadConfig(SAMPLE_POLICY);
    expect(JSON.parse(json)).toEqual(SAMPLE_POLICY);
  });

  it("accepts a JSON string", () => {
    const json = loadConfig(JSON.stringify(SAMPLE_POLICY));
    expect(JSON.parse(json)).toEqual(SAMPLE_POLICY);
  });

  it("accepts a YAML string", () => {
    const yaml = "agent: test\ndefault: allow\nrules: []\n";
    const json = loadConfig(yaml);
    expect(JSON.parse(json)).toEqual(SAMPLE_POLICY);
  });

  it("accepts a YAML file path", () => {
    const path = join(tmp, "policy.yaml");
    writeFileSync(path, "agent: test\ndefault: allow\nrules: []\n");
    const json = loadConfig(path);
    expect(JSON.parse(json)).toEqual(SAMPLE_POLICY);
  });

  it("accepts a JSON file path", () => {
    const path = join(tmp, "policy.json");
    writeFileSync(path, JSON.stringify(SAMPLE_POLICY));
    const json = loadConfig(path);
    expect(JSON.parse(json)).toEqual(SAMPLE_POLICY);
  });

  it("throws CheckrdInitError for missing file path", () => {
    expect(() => loadConfig(join(tmp, "missing.yaml"))).toThrow(CheckrdInitError);
  });

  it("throws CheckrdInitError for malformed YAML content", () => {
    expect(() => loadConfig("agent: test\n  bad:\nindent\n")).toThrow(CheckrdInitError);
  });

  it("throws CheckrdInitError when no policy is configured", () => {
    delete process.env["CHECKRD_POLICY_FILE"];
    expect(() => loadConfig(null)).toThrow(CheckrdInitError);
  });
});
