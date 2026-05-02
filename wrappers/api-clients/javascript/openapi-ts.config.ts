// Generation config for @hey-api/openapi-ts (0.96+).
//
// Inputs the committed schemas/api/openapi.json. Generator output
// is the private engine under `src/_generated/`; the public surface
// (the `Checkrd` class with `client.agents.list()` etc.) is hand-
// written in `src/index.ts` and `src/resources/*.ts`. Same pattern
// as the Python sibling and as Stainless-generated SDKs (OpenAI,
// Anthropic) — codegen produces low-level call sites, humans wrap
// them in a polished resource-based facade.
import { defineConfig } from "@hey-api/openapi-ts";

export default defineConfig({
  input: "../../../schemas/api/openapi.json",
  output: {
    path: "src/_generated",
    postProcess: [],
  },
  plugins: [
    "@hey-api/client-fetch",
    {
      name: "@hey-api/sdk",
      operations: { strategy: "byTags" },
    },
    {
      name: "@hey-api/typescript",
      enums: "typescript",
    },
  ],
});
