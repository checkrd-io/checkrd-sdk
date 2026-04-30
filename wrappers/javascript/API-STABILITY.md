# API stability policy

The Checkrd JavaScript SDK follows
[Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html). This
document defines what counts as the public API, what we promise across
versions, and how we deprecate things you depend on.

## Public surface

The public API is exactly what is **exported from the package's
`exports` map** in `package.json`. That includes:

- The default entry: `import { ... } from "checkrd"`.
- Per-vendor subpath entries: `import { OpenAIInstrumentor } from "checkrd/openai"` (and the analogous Anthropic, Cohere, Groq, Mistral, Together, Google GenAI subpaths).
- The Vercel AI SDK middleware: `import { checkrdMiddleware } from "checkrd/ai-sdk"`.
- Any other documented subpath listed in the README's "Subpath exports" section.

Anything else — symbols whose names start with `_`, files whose paths
start with `_`, or modules under `dist/` not referenced by `exports` —
is **internal**. We reserve the right to change, rename, or delete
internal symbols without a major version bump.

If you're importing an underscore-prefixed symbol or a non-exported
file path, you're depending on internals. Open an
[issue](https://github.com/checkrd-io/checkrd-sdk/issues) so we can promote
it to public, or add the API you need.

## SemVer commitments

For every public symbol:

| Change | Version bump |
|---|---|
| Add a new public symbol or option | minor |
| Add a new optional parameter | minor |
| Add a new error subclass | minor |
| Tighten validation in a way that rejects previously-accepted input | major |
| Rename or remove a public symbol | major |
| Change a public function's required parameter list | major |
| Change a public type alias in a non-additive way | major |
| Change runtime behaviour in a way a reasonable consumer would notice | major |
| Bump the minimum Node version | major |
| Fix a documented bug | patch |
| Improve internal performance with no API change | patch |

## SemVer exceptions

We follow the OpenAI and Anthropic SDK conventions on three deliberate
exceptions, allowed in minor versions when the practical impact is
small:

1. **Type-only changes that do not affect runtime.** Tightening a
   TypeScript signature to be more correct (for example, narrowing a
   union, removing a redundant `undefined`) may land in a minor.
2. **Internal symbols whose names happen to be exported.** If we
   discover that a symbol intended as internal is reachable through a
   public path, we may rename or remove it in a minor — but please
   open an issue first; we will usually find a way to keep it.
3. **Behaviour fixes for documented-broken edges.** If the
   documentation says "X works this way" and the implementation has
   never matched that, the fix lands in a minor.

We take backwards compatibility seriously. Practical impact is the
controlling concern: if a change is going to break real consumer code
in real codebases, it is a major regardless of which exception above
might technically apply.

## Deprecation policy

When a public symbol or option is replaced, the original behaviour
continues to work for at least **one full minor cycle plus the next
major** before removal. Concretely:

- Deprecation announced in `0.5.x` → still works in `0.6.x` → may be
  removed in `1.0.0`.
- Deprecation announced in `1.4.x` → still works in `1.5.x` → may be
  removed in `2.0.0`.

Deprecated symbols emit a one-time warning via the configured logger
(`logger.warn`) on first use per process. The warning includes:

- The symbol that was deprecated.
- The version it was deprecated in.
- The replacement (if any).
- A link to the [deprecation guide](https://checkrd.io/docs/javascript/deprecations).

To suppress all deprecation warnings, set `CHECKRD_SUPPRESS_DEPRECATIONS=1`.

Subscribe to [release notes](https://github.com/checkrd-io/checkrd-sdk/releases) to
discover deprecations before they become removals.

## Supported runtimes

| Runtime | Status | Notes |
|---|---|---|
| Node 18 | Supported | LTS until 2025-04. We will drop support no earlier than the next major after Node 18 is EOL. |
| Node 20 | Supported | Active LTS. |
| Node 22 | Supported | Active LTS. |
| Node 24 | Supported | Once released as LTS. |
| Bun (latest) | Supported | Tested in CI; no Node-only API leaks. |
| Deno (latest) | Supported | Tested in CI; via the async/edge path. |
| Cloudflare Workers | Supported | Use `initAsync` / `wrapAsync`; see README "Edge runtimes". |
| Vercel Edge | Supported | Same as Cloudflare. |
| Browser | **Unsupported** | The control-plane API key and Ed25519 private key live in process memory; shipping them to browsers exposes them to end users. |

Dropping a major-version-marked runtime is a major version bump.
Dropping any other support is a minor.

## Major-version support window

Once a major version is released, the previous major receives security
fixes for **6 months**. Bug fixes and new features land only on the
current major. Two-major support windows are evaluated case-by-case
for enterprise contracts.

## How to depend on internals safely (if you must)

If you have a hard requirement to import an underscore-prefixed symbol
or a non-exported file:

1. Open an issue describing the need so we can promote the API.
2. Pin the package to an exact version (`"checkrd": "1.4.2"`, not
   `"^1.4.2"`).
3. Plan to revisit on every minor upgrade.

## Reporting a stability concern

If a change you didn't expect breaks your code, open an
[issue](https://github.com/checkrd-io/checkrd-sdk/issues) tagged
`api-stability`. We treat unexpected breaks as bugs — even when the
change technically falls within an exception above — and will either
revert, ship a fix, or give you a migration path.
