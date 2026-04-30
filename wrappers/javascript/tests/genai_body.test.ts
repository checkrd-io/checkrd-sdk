import { describe, expect, it } from "vitest";

import {
  extractRequestAttrs,
  extractResponseAttrs,
} from "../src/_genai_body.js";

const enc = (obj: unknown): Uint8Array =>
  new TextEncoder().encode(JSON.stringify(obj));

describe("extractRequestAttrs — OpenAI", () => {
  it("extracts model and stream", () => {
    const body = enc({
      model: "gpt-4o-mini",
      messages: [{ role: "user", content: "hi" }],
      stream: true,
    });
    expect(extractRequestAttrs("openai", body)).toEqual({
      "gen_ai.request.model": "gpt-4o-mini",
      "gen_ai.request.stream": true,
    });
  });

  it("handles missing stream", () => {
    expect(
      extractRequestAttrs("openai", enc({ model: "gpt-4o", messages: [] })),
    ).toEqual({ "gen_ai.request.model": "gpt-4o" });
  });

  it("routes azure.openai through the same shape", () => {
    expect(
      extractRequestAttrs("azure.openai", enc({ model: "gpt-4o", stream: false })),
    ).toEqual({
      "gen_ai.request.model": "gpt-4o",
      "gen_ai.request.stream": false,
    });
  });
});

describe("extractResponseAttrs — OpenAI", () => {
  it("extracts model + usage", () => {
    const body = enc({
      model: "gpt-4o-mini-2024-07-18",
      usage: {
        prompt_tokens: 10,
        completion_tokens: 25,
        total_tokens: 35,
      },
    });
    expect(extractResponseAttrs("openai", body)).toEqual({
      "gen_ai.response.model": "gpt-4o-mini-2024-07-18",
      "gen_ai.usage.input_tokens": 10,
      "gen_ai.usage.output_tokens": 25,
    });
  });

  it("skips non-integer usage fields", () => {
    const body = enc({
      model: "gpt-4o",
      usage: { prompt_tokens: "ten", completion_tokens: 5 },
    });
    const attrs = extractResponseAttrs("openai", body);
    expect(attrs["gen_ai.usage.input_tokens"]).toBeUndefined();
    expect(attrs["gen_ai.usage.output_tokens"]).toBe(5);
  });
});

describe("extractRequestAttrs — Anthropic", () => {
  it("extracts model and stream", () => {
    const body = enc({
      model: "claude-3-haiku-20240307",
      messages: [{ role: "user", content: "hi" }],
      stream: false,
    });
    expect(extractRequestAttrs("anthropic", body)).toEqual({
      "gen_ai.request.model": "claude-3-haiku-20240307",
      "gen_ai.request.stream": false,
    });
  });

  it("routes aws.bedrock through the same shape", () => {
    const body = enc({
      model: "anthropic.claude-3-haiku-20240307-v1:0",
      stream: true,
    });
    expect(extractRequestAttrs("aws.bedrock", body)).toEqual({
      "gen_ai.request.model": "anthropic.claude-3-haiku-20240307-v1:0",
      "gen_ai.request.stream": true,
    });
  });
});

describe("extractResponseAttrs — Anthropic", () => {
  it("extracts model + usage with anthropic naming", () => {
    const body = enc({
      type: "message",
      model: "claude-3-haiku-20240307",
      usage: { input_tokens: 12, output_tokens: 38 },
    });
    expect(extractResponseAttrs("anthropic", body)).toEqual({
      "gen_ai.response.model": "claude-3-haiku-20240307",
      "gen_ai.usage.input_tokens": 12,
      "gen_ai.usage.output_tokens": 38,
    });
  });
});

describe("safety properties", () => {
  it("returns empty for unknown provider", () => {
    const body = enc({ model: "gpt-4o" });
    expect(extractRequestAttrs("perplexity", body)).toEqual({});
    expect(extractResponseAttrs("perplexity", body)).toEqual({});
  });

  it("returns empty for undefined provider", () => {
    expect(extractRequestAttrs(undefined, enc({}))).toEqual({});
  });

  it("returns empty for empty / undefined body", () => {
    expect(extractRequestAttrs("openai", undefined)).toEqual({});
    expect(extractRequestAttrs("openai", new Uint8Array(0))).toEqual({});
    expect(extractRequestAttrs("openai", "")).toEqual({});
  });

  it("returns empty for invalid JSON", () => {
    expect(extractRequestAttrs("openai", "not json")).toEqual({});
    expect(extractResponseAttrs("openai", "{incomplete")).toEqual({});
  });

  it("returns empty for non-UTF-8 binary", () => {
    expect(
      extractResponseAttrs("openai", new Uint8Array([0xff, 0xfe, 0xfd])),
    ).toEqual({});
  });

  it("returns empty for oversized body", () => {
    const huge = new Uint8Array(1_100_000);
    expect(extractRequestAttrs("openai", huge)).toEqual({});
  });

  it("returns empty for non-object JSON root", () => {
    expect(extractRequestAttrs("openai", "[1, 2, 3]")).toEqual({});
  });
});
