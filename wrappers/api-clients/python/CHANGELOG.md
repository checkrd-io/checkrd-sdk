# Changelog

All notable changes to the `checkrd-api` Python client will be
documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.1.1 (2026-04-27)

### Added

- Industry-standard alignment across the full stack: shared error
  envelope, Stripe-pattern idempotency keys, date-pinned
  `Checkrd-Version` header, and `X-Checkrd-SDK-*` platform headers.

### Fixed

- Resolved residual CI failures from the initial publish workflow.

## 0.1.0 (2026-04-25)

### Added

- Initial release. Generated `Checkrd` and `AsyncCheckrd` clients
  covering the full `/v1/*` Control Plane REST API surface.
- Type-checked request and response models for every endpoint.
- Sync + async parity, auto-retry with exponential backoff, and
  cursor-based auto-pagination.
