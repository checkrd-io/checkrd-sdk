import eslint from "@eslint/js";
import tseslint from "typescript-eslint";
import jsdoc from "eslint-plugin-jsdoc";

/**
 * Flat config mirrors the Python wrapper's Ruff + mypy strict combo:
 *   - typescript-eslint strict-type-checked = runtime correctness + type safety
 *   - eslint-plugin-jsdoc on exported symbols = interrogate-equivalent
 *     docstring coverage. We enforce it on the public surface only so
 *     internal helpers don't carry tax.
 */
export default tseslint.config(
  {
    // Standalone runtime smoke scripts in `tests/runtime/{deno,bun}_smoke.ts`
    // are executed by the real Deno / Bun binaries (not vitest), use
    // those runtimes' globals (`Deno.readFile`, `Bun.file`), and
    // import the published dist bundle as `unknown`. They are
    // correct-by-execution under their target runtime; the Node-
    // context ESLint pass doesn't have the right type info to reason
    // about them and reports tens of false positives. CI runs each
    // script under its own runtime where real type errors surface.
    ignores: [
      "dist/",
      "node_modules/",
      "coverage/",
      "artifacts/",
      "*.config.*",
      "tests/runtime/deno_smoke.ts",
      "tests/runtime/bun_smoke.ts",
    ],
  },
  eslint.configs.recommended,
  ...tseslint.configs.strictTypeChecked,
  ...tseslint.configs.stylisticTypeChecked,
  {
    files: ["src/**/*.ts"],
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    plugins: { jsdoc },
    rules: {
      "jsdoc/require-jsdoc": [
        "error",
        {
          publicOnly: true,
          require: {
            ArrowFunctionExpression: false,
            ClassDeclaration: true,
            ClassExpression: true,
            FunctionDeclaration: true,
            FunctionExpression: false,
            MethodDefinition: false,
          },
          contexts: [
            "TSInterfaceDeclaration",
            "TSTypeAliasDeclaration",
            "TSEnumDeclaration",
          ],
        },
      ],
      "jsdoc/require-description": ["error", { contexts: ["any"] }],
      // Allow unused args prefixed with _ (convention used across wrappers).
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
      // We re-throw async work under our own error types.
      "@typescript-eslint/only-throw-error": "off",
    },
  },
  {
    files: ["tests/**/*.ts"],
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      // Tests need to exercise the same bracket-notation env access
      // pattern as production code; the dot-notation rule adds noise
      // without safety value here.
      "@typescript-eslint/dot-notation": "off",
      "@typescript-eslint/no-dynamic-delete": "off",
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
      "@typescript-eslint/no-unnecessary-type-assertion": "off",
      "@typescript-eslint/require-await": "off",
      "@typescript-eslint/no-misused-spread": "off",
      "jsdoc/require-jsdoc": "off",
      "jsdoc/require-description": "off",
      "@typescript-eslint/no-non-null-assertion": "off",
      "@typescript-eslint/no-explicit-any": "off",
      "@typescript-eslint/array-type": "off",
      // Vitest mocks return ``vi.fn()`` references; ``expect(spy.method)``
      // is the canonical pattern and the function isn't actually unbound
      // — it's a static ``Mock`` reference. Match the override OpenAI /
      // Anthropic / Stripe SDKs use in their test trees.
      "@typescript-eslint/unbound-method": "off",
      // ``vi.fn()`` infers as ``Mock<any, any>``; tests legitimately
      // assign that result into ``Foo``-shaped fixtures.
      "@typescript-eslint/no-unsafe-assignment": "off",
      // ``vi.fn(() => {})`` is a deliberate empty stub.
      "@typescript-eslint/no-empty-function": "off",
      // Tests deliberately exercise defensive runtime checks that
      // mypy/TypeScript can already prove are dead. Letting them stand
      // documents the intent; the production rule still applies.
      "@typescript-eslint/no-unnecessary-condition": "off",
      // ``RequestInit.body`` is a ``BodyInit | null`` union; tests
      // stringify it for assertions and the resulting ``object``
      // branch is acceptable in test scope.
      "@typescript-eslint/no-base-to-string": "off",
      "@typescript-eslint/restrict-template-expressions": "off",
    },
  },
);
