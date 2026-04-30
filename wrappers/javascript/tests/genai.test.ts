import { describe, expect, it } from "vitest";

import {
  attributesForUrl,
  detectOperation,
  detectProvider,
} from "../src/_genai.js";

describe("detectProvider", () => {
  const cases: ReadonlyArray<readonly [string, string]> = [
    ["api.openai.com", "openai"],
    ["api.anthropic.com", "anthropic"],
    ["api.cohere.com", "cohere"],
    ["api.cohere.ai", "cohere"],
    ["api.groq.com", "groq"],
    ["api.mistral.ai", "mistral.ai"],
    ["api.together.xyz", "together.ai"],
    ["api.together.ai", "together.ai"],
    ["generativelanguage.googleapis.com", "google.gemini"],
    ["api.perplexity.ai", "perplexity"],
    ["api.fireworks.ai", "fireworks"],
    ["api.x.ai", "xai"],
  ];
  for (const [host, expected] of cases) {
    it(`maps ${host} → ${expected}`, () => {
      expect(detectProvider(host)).toBe(expected);
    });
  }

  const suffixCases: ReadonlyArray<readonly [string, string]> = [
    ["bedrock-runtime.us-east-1.amazonaws.com", "aws.bedrock"],
    ["bedrock-runtime.eu-west-1.amazonaws.com", "aws.bedrock"],
    ["bedrock.us-west-2.amazonaws.com", "aws.bedrock"],
    ["my-deployment.openai.azure.com", "azure.openai"],
    ["us-central1-aiplatform.googleapis.com", "google.vertex_ai"],
  ];
  for (const [host, expected] of suffixCases) {
    it(`maps suffix ${host} → ${expected}`, () => {
      expect(detectProvider(host)).toBe(expected);
    });
  }

  it("is case-insensitive", () => {
    expect(detectProvider("API.OPENAI.COM")).toBe("openai");
  });

  it("returns undefined for unknown hosts", () => {
    expect(detectProvider("api.example.com")).toBeUndefined();
  });

  it("returns undefined for empty input", () => {
    expect(detectProvider("")).toBeUndefined();
  });
});

describe("detectOperation", () => {
  const cases: ReadonlyArray<readonly [string, string]> = [
    ["/v1/chat/completions", "chat"],
    ["/v1/messages", "chat"],
    ["/v1/converse", "chat"],
    ["/v1/responses", "chat"],
    ["/v1/embeddings", "embeddings"],
    ["/v1/embed", "embeddings"],
    ["/v1/completions", "text_completion"],
    ["/v1beta/models/gemini-pro:generateContent", "generate_content"],
    ["/v1beta/models/gemini-pro:streamGenerateContent", "generate_content"],
    ["/v1/images/generations", "image.generation"],
    ["/v1/audio/speech", "audio.speech"],
    ["/v1/audio/transcriptions", "audio.transcription"],
  ];
  for (const [path, expected] of cases) {
    it(`maps ${path} → ${expected}`, () => {
      expect(detectOperation(path)).toBe(expected);
    });
  }

  it("prefers /chat/completions → 'chat' over /completions → 'text_completion'", () => {
    // Order matters in the pattern table: the more specific
    // /chat/completions rule must win.
    expect(detectOperation("/v1/chat/completions")).toBe("chat");
  });

  it("returns undefined for non-LLM paths", () => {
    expect(detectOperation("/v1/random/path")).toBeUndefined();
  });
});

describe("attributesForUrl", () => {
  it("returns both attributes for OpenAI chat", () => {
    expect(attributesForUrl("api.openai.com", "/v1/chat/completions")).toEqual({
      "gen_ai.provider.name": "openai",
      "gen_ai.operation.name": "chat",
    });
  });

  it("returns both attributes for Bedrock Converse", () => {
    expect(
      attributesForUrl(
        "bedrock-runtime.us-east-1.amazonaws.com",
        "/model/anthropic.claude-3-haiku-20240307-v1:0/converse",
      ),
    ).toEqual({
      "gen_ai.provider.name": "aws.bedrock",
      "gen_ai.operation.name": "chat",
    });
  });

  it("returns empty object for non-LLM URL", () => {
    expect(attributesForUrl("api.example.com", "/random")).toEqual({});
  });

  it("returns provider-only when path is unmapped", () => {
    expect(attributesForUrl("api.openai.com", "/v1/files")).toEqual({
      "gen_ai.provider.name": "openai",
    });
  });

  it("returns operation-only when provider is unmapped", () => {
    expect(
      attributesForUrl("internal-proxy.example.com", "/chat/completions"),
    ).toEqual({ "gen_ai.operation.name": "chat" });
  });
});
