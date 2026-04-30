# Package layout — single package, subpath exports

## Decision

Checkrd ships as one npm package — `checkrd` — with subpath exports
for every framework and vendor integration. We are **not** splitting
into per-framework packages (`@checkrd/nextjs`, `@checkrd/cloudflare`,
`@checkrd/vercel-ai`) at this stage.

## Why not split now

Sentry famously runs ~12 packages today (`@sentry/node`,
`@sentry/browser`, `@sentry/nextjs`, `@sentry/bun`, `@sentry/deno`,
`@sentry/cloudflare`, `@sentry/aws-serverless`, etc.). They got there
across **eight years** of API stabilization. Splitting earlier would
have forced version-locking pain on every internal refactor.

We're at v0.2 with no published releases. The cost of a split now:

- **Cross-package version lock**: every framework package would pin
  the core (`@checkrd/core`) to a specific minor; a single bug in
  the core forces 12 simultaneous releases.
- **Independent test matrices**: every framework package needs its
  own CI lane, lockfile, publint check, attw check, and size budget.
  ~5 minutes of CI per package, multiplied by however many packages
  exist.
- **Documentation surface**: every framework page now points to
  ``npm install @checkrd/nextjs`` instead of the unified
  ``npm install checkrd`` — onboarding gets harder, not easier.
- **Internal API drift**: framework adapters today share helper code
  via direct relative imports (``../engine.js``,
  ``../transports/fetch.js``). Splitting forces those to become
  public API on ``@checkrd/core``, slowing every internal refactor.

The benefit a split would bring (smaller per-framework bundles)
**we already get** through subpath exports + tree-shaking +
``"sideEffects": false``. A consumer who only imports
``checkrd/cloudflare`` ships only the Cloudflare adapter and the
core engine — no Next.js, no MCP, no Mastra.

## When we will split

Three signals will make a split worth its cost:

1. **Framework adapters need framework-specific peer deps**. Today
   every adapter is structurally typed against the framework — no
   ``peerDependencies`` entry exists for ``next`` or ``hono``. When
   that changes (e.g. an adapter starts importing a Next.js-only
   helper at runtime), peer-dep declarations need their own package
   so adapters that the consumer doesn't use don't add deps to
   their lockfile.
2. **Independent release cadences**. If the Cloudflare adapter
   ships fixes 4× as often as the Next.js adapter, the version
   churn forces a split.
3. **Bundle-size complaints**. We track ``size-limit`` budgets per
   subpath today; if any adapter consistently fails its budget
   because of imports it can't tree-shake out, splitting becomes
   net-positive.

We will split when those signals fire — not on a schedule. The
restructure is mechanical (move files into ``packages/<name>/``,
add per-package package.json, wire turbo / nx) and reversible.

## What this means for users

Today: ``npm install checkrd`` installs everything.
``import { ... } from "checkrd"`` for the umbrella.
``import { ... } from "checkrd/openai"`` (or ``/anthropic``,
``/next``, ``/cloudflare``, ``/hono``, ``/mcp``, ``/mastra``,
``/ai-sdk``, etc.) for the per-integration entry point.
``import { ... } from "checkrd/quick"`` for the curated 10-symbol
beginner subset. ``import { ... } from "checkrd/_retry"`` (or any
internal module) when you need power-user access.

Tomorrow (post-split, when the signals fire): ``npm install
@checkrd/core @checkrd/nextjs``. The codemod for the migration will
ship with that release.
