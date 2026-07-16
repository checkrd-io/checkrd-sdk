import { defineConfig } from "vitest/config";

/**
 * Vitest config mirrors the Python wrapper's coverage floor (80%) so both
 * SDKs publish to the same correctness bar. `forks` pool isolates each
 * test file in its own worker — necessary because the WASM module cache
 * is global and a poorly-contained test could otherwise leak state.
 */
export default defineConfig({
  test: {
    environment: "node",
    pool: "forks",
    setupFiles: ["./tests/setup.ts"],
    // Two naming conventions coexist in this repo. ``*.test.ts`` is the
    // dominant pattern for top-level tests; ``tests/integrations/test_*.ts``
    // mirrors the Python wrapper's ``tests/integrations/test_*.py`` layout
    // so both SDKs feel identical to anyone moving between them.
    include: ["tests/**/*.test.ts", "tests/integrations/test_*.ts"],
    testTimeout: 30_000,
    coverage: {
      provider: "v8",
      reporter: ["text", "lcov"],
      include: ["src/**/*.ts"],
      exclude: [
        "src/_wasm_integrity.ts",
        "src/_version.ts",
        "src/**/*.d.ts",
        // Barrel files — re-exports only, no runtime logic to cover.
        "src/transports/index.ts",
        "src/integrations/index.ts",
        // Type-only module (pure interfaces). Covered by consumer tests.
        "src/hooks.ts",
      ],
      thresholds: {
        lines: 80,
        statements: 80,
        // Branch coverage was lowered from 75 → 65 after the vitest
        // 2 → 4 bump (v8's branch detector got more thorough — it
        // counts implicit-else and nullish-coalescing as separate
        // branches — so the same source reported ~5pp lower). Raised
        // back to 74 once the _cohere/_groq/_together/_google_genai
        // adapters gained REAL instrumentation tests (construct-trap
        // fetch-injection + telemetry + shape-guard + sealed-export
        // paths), which moved those files from ~0% to ~80% branch
        // coverage. Actual is ~75%; the floor keeps a small margin
        // below it, matching the lines/statements/functions floors.
        branches: 74,
        functions: 80,
      },
    },
  },
});
