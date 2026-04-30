<!--
Thanks for contributing. A few quick notes:

- Keep the PR focused. Unrelated cleanup belongs in a separate PR.
- Run the relevant test suites locally before pushing (see CONTRIBUTING.md).
- If this changes observable SDK behaviour, update the relevant CHANGELOG.
- For security-relevant fixes, use a private advisory instead — see SECURITY.md.
-->

## Summary

<!-- What does this change do? One or two sentences. -->

## Motivation

<!-- Why this change? Link to an issue or describe the problem. -->

## Verification

<!-- How did you test this? Note any platforms / runtimes / matrix cells covered. -->

- [ ] `cargo test --workspace` (if `crates/` changed)
- [ ] `pytest` in `wrappers/python/` (if Python changed)
- [ ] `npm test` in `wrappers/javascript/` (if JS changed)
- [ ] Manual repro / integration test (describe below)

## Notes for reviewers

<!-- Anything subtle worth flagging — load-bearing tests, perf considerations, follow-ups. -->
