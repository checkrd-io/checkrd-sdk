# Contributing

Thank you for considering a contribution. This repo tracks both the
WASM core (closed-source, in a private repository) and the
language-wrapper SDKs. Wrapper changes flow through this repository;
core changes flow through the private repository and land here as a
binary update.

## Getting set up

```bash
git clone https://github.com/checkrd/checkrd.git
cd checkrd/wrappers/python
pip install -e ".[test]"
```

## Running the test suite

```bash
ruff check .
ruff format --check .
mypy --strict src/
pytest
pytest --cov=checkrd --cov-fail-under=80
```

The test suite uses `pytest-xdist`; pass `-n auto` for parallel
runs. `pytest-randomly` is enabled by default to catch ordering
dependencies.

## Coding style

- Python 3.9+ compatibility. Use `from __future__ import annotations`
  in new modules.
- Type hints on every public function (checked by `mypy --strict`).
- Docstrings on every public module, class, and function
  (80% coverage gate via `interrogate`).
- No new runtime dependencies without discussion.
- Security-relevant changes must be called out in the PR
  description so they can be flagged under a `[security]` tag in
  the changelog.

## Filing a pull request

1. One focused change per PR. Refactors land separately from
   behavior changes.
2. Update `CHANGELOG.md` under `## [Unreleased]` with a one-line
   summary.
3. If your change affects the FFI surface, the telemetry event
   schema, or the policy schema, update `WASM-CORE.md` at the
   same time.
4. If your change changes the threat model (new asset, new actor,
   new mitigation), update `THREAT-MODEL.md`.
5. CI must be green before review.

## Reporting a security issue

**Do not open a public GitHub issue.** See
[SECURITY.md](./SECURITY.md) for the coordinated-disclosure
process.

## License

By contributing, you agree that your contributions will be licensed
under the Apache License 2.0.
