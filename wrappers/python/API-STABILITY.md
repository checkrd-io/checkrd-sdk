# API stability policy

The Checkrd Python SDK follows
[Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html). This
document defines what counts as the public API, what we promise across
versions, and how we deprecate things you depend on.

## Public surface

The public API is exactly what is exposed by these import paths:

- `import checkrd` — the umbrella module re-exports `init`, `wrap`,
  `wrap_async`, `instrument`, `shutdown`, `healthy`, the exception
  hierarchy, and the per-vendor `instrument_*()` helpers.
- `from checkrd.identity import LocalIdentity, ExternalIdentity, IdentityProvider` — the identity protocol.
- `from checkrd.sinks import TelemetrySink, ConsoleSink, JsonFileSink, ControlPlaneSink, CompositeSink, OtlpSink` — the sink protocol and shipped implementations.
- `from checkrd.testing import mock_wrap, MockEngine` — the unit-test helpers.
- `from checkrd.exceptions import CheckrdError, CheckrdInitError, CheckrdPolicyDenied, PolicySignatureError` — the exception hierarchy.

Anything else — module attributes whose names start with `_`,
modules whose names start with `_`, classes documented as "internal"
— is **internal**. We reserve the right to change, rename, or delete
internal attributes without a major version bump.

If you find yourself importing an underscore-prefixed symbol, open an
[issue](https://github.com/checkrd/checkrd/issues) so we can promote
it to public, or add the API you need.

## SemVer commitments

For every public symbol:

| Change | Version bump |
|---|---|
| Add a new public symbol, kwarg, or option | minor |
| Add a new optional argument | minor |
| Add a new exception subclass | minor |
| Tighten validation in a way that rejects previously-accepted input | major |
| Rename or remove a public symbol | major |
| Change a public function's required argument list | major |
| Change a Protocol's method signature in a non-additive way | major |
| Change runtime behaviour in a way a reasonable consumer would notice | major |
| Bump the minimum Python version | major |
| Fix a documented bug | patch |
| Improve internal performance with no API change | patch |

## SemVer exceptions

We follow industry conventions on three deliberate exceptions, allowed
in minor versions when the practical impact is small:

1. **Type-annotation-only changes.** Tightening a public type hint to
   be more correct (for example, narrowing `Optional[X]` to `X` where
   `None` was never possible) may land in a minor.
2. **Internal modules whose paths happen to be importable.** If we
   discover that a module intended as internal is being imported
   through a non-`_` path, we may rename or move it in a minor — but
   please open an issue first; we will usually find a way to keep it.
3. **Behaviour fixes for documented-broken edges.** If the
   documentation says "X works this way" and the implementation has
   never matched that, the fix lands in a minor.

We take backwards compatibility seriously. Practical impact is the
controlling concern: if a change is going to break real consumer code
in real codebases, it is a major regardless of which exception above
might technically apply.

## Deprecation policy

When a public symbol or kwarg is replaced, the original behaviour
continues to work for at least **one full minor cycle plus the next
major** before removal:

- Deprecation announced in `0.5.x` → still works in `0.6.x` → may be
  removed in `1.0.0`.
- Deprecation announced in `1.4.x` → still works in `1.5.x` → may be
  removed in `2.0.0`.

Deprecated symbols emit a `DeprecationWarning` via Python's standard
`warnings` module on first use per process. The warning includes:

- The symbol that was deprecated.
- The version it was deprecated in.
- The replacement (if any).
- A link to the [deprecation guide](https://checkrd.io/docs/python/deprecations).

To suppress all deprecation warnings, set `CHECKRD_SUPPRESS_DEPRECATIONS=1`.
Standard `python -W` filters work too.

Subscribe to [release notes](https://github.com/checkrd/checkrd/releases) to
discover deprecations before they become removals.

## Supported runtimes

| Runtime | Status | Notes |
|---|---|---|
| Python 3.9 | Supported | EOL October 2025. We will drop support no earlier than the next major after Python 3.9 is EOL. |
| Python 3.10 | Supported | EOL October 2026. |
| Python 3.11 | Supported | EOL October 2027. |
| Python 3.12 | Supported | EOL October 2028. |
| Python 3.13 | Supported | EOL October 2029. |
| PyPy | Best-effort | Tested informally; report breaks as bugs. |

Dropping a major-version-marked Python is a major version bump.

## Major-version support window

Once a major version is released, the previous major receives security
fixes for **6 months**. Bug fixes and new features land only on the
current major. Two-major support windows are evaluated case-by-case
for enterprise contracts.

## How to depend on internals safely (if you must)

If you have a hard requirement to import an underscore-prefixed module
or attribute:

1. Open an issue describing the need so we can promote the API.
2. Pin the package to an exact version (`checkrd==1.4.2`, not
   `checkrd>=1.4.2`).
3. Plan to revisit on every minor upgrade.

## Reporting a stability concern

If a change you didn't expect breaks your code, open an
[issue](https://github.com/checkrd/checkrd/issues) tagged
`api-stability`. We treat unexpected breaks as bugs — even when the
change technically falls within an exception above — and will either
revert, ship a fix, or give you a migration path.
