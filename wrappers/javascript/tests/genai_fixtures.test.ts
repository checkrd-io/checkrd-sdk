/**
 * Golden-fixture parity test for the GenAI body extractor.
 *
 * Loads every case in `schemas/genai-fixtures/*.json` and asserts the
 * TS extractor reproduces each `expected_request_attrs` /
 * `expected_response_attrs` attribute-for-attribute. The Python SDK
 * runs the *same* fixtures (`wrappers/python/tests/...`), so a field
 * that drifts between the two runtimes fails here in at least one of
 * them — this file IS the cross-runtime parity contract.
 *
 * Bodies are fed as `Uint8Array` of the JSON to exercise the byte
 * path (the real telemetry path sees buffered bytes); one case is
 * additionally fed as a string to cover that input shape too.
 */
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
  extractRequestAttrs,
  extractResponseAttrs,
} from "../src/_genai_body.js";

const here = dirname(fileURLToPath(import.meta.url));
// Fixtures live at repo-root `schemas/genai-fixtures/`; from
// `wrappers/javascript/tests` that is `../../../schemas/...`.
const FIXTURE_DIR = resolve(here, "..", "..", "..", "schemas", "genai-fixtures");

interface FixtureCase {
  name: string;
  provider: string;
  request?: { body: unknown };
  response?: { body?: unknown; headers?: Record<string, string> };
  expected_request_attrs?: Record<string, string | number | boolean>;
  expected_response_attrs?: Record<string, string | number | boolean>;
}

const enc = (obj: unknown): Uint8Array =>
  new TextEncoder().encode(JSON.stringify(obj));

function loadFixtures(file: string): FixtureCase[] {
  const raw = readFileSync(join(FIXTURE_DIR, file), "utf-8");
  const parsed: unknown = JSON.parse(raw);
  if (!Array.isArray(parsed)) {
    throw new Error(`fixture ${file} is not a JSON array`);
  }
  return parsed as FixtureCase[];
}

const fixtureFiles = readdirSync(FIXTURE_DIR)
  .filter((f) => f.endsWith(".json"))
  .sort();

// Guard against a silently empty fixture dir (path typo, bad checkout)
// — an empty sweep would make this whole suite vacuously green.
describe("genai fixtures discovery", () => {
  it("found the fixture files", () => {
    expect(fixtureFiles).toEqual([
      "anthropic.json",
      "bedrock.json",
      "cohere.json",
      "gemini.json",
      "openai.json",
    ]);
  });
});

for (const file of fixtureFiles) {
  const cases = loadFixtures(file);
  describe(`genai fixtures — ${file}`, () => {
    it("declares at least one case", () => {
      expect(cases.length).toBeGreaterThan(0);
    });

    for (const c of cases) {
      it(c.name, () => {
        if (c.expected_request_attrs !== undefined) {
          expect(c.request, `${c.name}: expected_request_attrs without request`).toBeDefined();
          const body = enc(c.request?.body);
          expect(extractRequestAttrs(c.provider, body)).toEqual(
            c.expected_request_attrs,
          );
        }
        if (c.expected_response_attrs !== undefined) {
          // body may legitimately be absent (Bedrock cases are
          // headers-only); pass undefined through in that case.
          const body =
            c.response?.body === undefined ? undefined : enc(c.response.body);
          const headers = c.response?.headers;
          expect(
            extractResponseAttrs(c.provider, body, headers),
          ).toEqual(c.expected_response_attrs);
        }
      });
    }
  });
}

// ---------------------------------------------------------------------------
// String-input parity: the extractor accepts a JSON string as well as
// bytes. Re-run a representative case from each provider that has a
// body to prove the string path produces the identical result.
// ---------------------------------------------------------------------------

describe("genai fixtures — string-input path", () => {
  const withBody = (file: string, name: string): FixtureCase => {
    const found = loadFixtures(file).find((c) => c.name === name);
    if (!found) throw new Error(`fixture case ${name} not found in ${file}`);
    return found;
  };

  it("OpenAI request via string body matches the bytes path", () => {
    const c = withBody("openai.json", "openai_chat_request");
    const json = JSON.stringify(c.request?.body);
    expect(extractRequestAttrs(c.provider, json)).toEqual(
      c.expected_request_attrs,
    );
  });

  it("Anthropic response via string body matches the bytes path", () => {
    const c = withBody(
      "anthropic.json",
      "anthropic_response_cache_excluded_normalized_to_inclusive",
    );
    const json = JSON.stringify(c.response?.body);
    expect(extractResponseAttrs(c.provider, json)).toEqual(
      c.expected_response_attrs,
    );
  });

  it("Gemini response via string body matches the bytes path", () => {
    const c = withBody(
      "gemini.json",
      "gemini_generate_content_response_with_cache_and_thoughts",
    );
    const json = JSON.stringify(c.response?.body);
    expect(extractResponseAttrs(c.provider, json)).toEqual(
      c.expected_response_attrs,
    );
  });
});

// ---------------------------------------------------------------------------
// Bedrock header-source parity: the same case must produce the same
// result whether headers arrive as a plain object or a WHATWG
// `Headers` instance (the two shapes the SDK sees across runtimes).
// ---------------------------------------------------------------------------

describe("genai fixtures — Bedrock Headers-instance parity", () => {
  const bedrockCases = loadFixtures("bedrock.json").filter(
    (c) => c.response?.headers !== undefined && c.expected_response_attrs,
  );

  for (const c of bedrockCases) {
    it(`${c.name} (Headers instance)`, () => {
      const h = new Headers(c.response?.headers);
      expect(extractResponseAttrs(c.provider, undefined, h)).toEqual(
        c.expected_response_attrs,
      );
    });
  }
});
