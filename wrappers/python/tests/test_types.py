"""Tests for ``checkrd._types`` — the public TypedDict + Literal surface.

The runtime behavior of TypedDicts is barely-there (they're plain
dicts at runtime), so these tests primarily verify:

  - The intended fields exist on each TypedDict's ``__annotations__``
  - Required vs optional split is honored by ``__required_keys__`` /
    ``__optional_keys__``
  - The Literal aliases enumerate the right strings
  - Re-exports from the top-level ``checkrd`` package work
"""

from __future__ import annotations

import typing
from typing import get_args

import pytest


class TestPolicyTypedDict:
    """The `Policy` TypedDict mirrors the policy schema's required +
    optional split. Static type checkers enforce the contract; these
    tests pin the runtime view so accidental edits to ``_types.py``
    that change required-ness can't slip past CI silently."""

    def test_required_keys_match_schema(self) -> None:
        from checkrd._types import Policy

        # The three fields the WASM core demands at install time.
        assert Policy.__required_keys__ == frozenset(
            {"agent", "default", "rules"},
        )

    def test_optional_keys_are_documented_metadata(self) -> None:
        from checkrd._types import Policy

        assert Policy.__optional_keys__ == frozenset(
            {"schema_version", "description"},
        )

    def test_round_trip_through_loadconfig_json(self) -> None:
        """A Policy dict serializes to JSON the WASM core accepts.
        Smoke test that the TypedDict shape matches the on-the-wire
        format — a divergence here would mean either ``_types.py``
        drifted from the schema or the schema drifted from us."""
        import json

        from checkrd._types import Policy

        policy: Policy = {
            "agent": "test",
            "default": "deny",
            "rules": [
                {
                    "match": {"method": "GET", "url": "https://api.example.com/*"},
                    "action": "allow",
                },
            ],
        }
        # Round-trip — must not lose fields.
        parsed = json.loads(json.dumps(policy))
        assert parsed == policy

    def test_optional_fields_omitted_remain_valid(self) -> None:
        """Omitting `schema_version` and `description` must produce
        a Policy that the type system still recognizes. ``total=False``
        on the parent is what makes that work; a regression would
        force users to spell out keys they don't care about."""
        from checkrd._types import Policy

        minimal: Policy = {
            "agent": "test",
            "default": "allow",
            "rules": [],
        }
        assert "schema_version" not in minimal
        assert "description" not in minimal


class TestPolicyAction:
    """`PolicyAction` is a closed Literal enum — the four valid
    verdicts a rule can return. New verdicts require a SemVer-major
    bump because they break exhaustive `match` statements in user
    code."""

    def test_action_values(self) -> None:
        from checkrd._types import PolicyAction

        assert set(get_args(PolicyAction)) == {"allow", "deny", "rate_limit"}

    def test_default_values(self) -> None:
        from checkrd._types import PolicyDefault

        # `default` is the whole-policy fallback; rate_limit doesn't
        # make sense at this level (it needs a specific rule's window).
        assert set(get_args(PolicyDefault)) == {"allow", "deny"}


class TestHealthCheck:
    """`HealthCheck` is the return type of ``checkrd.healthy()`` —
    K8s probes and dashboards parse this. Accidental field
    removal/rename is a breaking change."""

    def test_required_keys(self) -> None:
        from checkrd._types import HealthCheck

        assert HealthCheck.__required_keys__ == frozenset(
            {
                "status",
                "engine_loaded",
                "control_plane_connected",
                "agent_id",
                "enforce",
                "last_eval_at",
            },
        )

    def test_optional_keys(self) -> None:
        from checkrd._types import HealthCheck

        # ``telemetry`` is optional because batch-less deployments
        # (CHECKRD_DISABLED, no control plane) never construct one.
        # ``degradation_reason`` is optional because the field only
        # populates when status == "degraded"; on healthy / disabled /
        # error paths the SDK omits it rather than carrying ``None``.
        assert HealthCheck.__optional_keys__ == frozenset(
            {"telemetry", "degradation_reason"},
        )

    def test_degradation_reason_values(self) -> None:
        from checkrd._types import DegradationReason

        # Closed enum — adding a value is a SemVer minor bump
        # (additive); removing one is major (callers may have
        # exhaustive ``match`` statements over these tokens).
        assert set(get_args(DegradationReason)) == {
            "wasm_failed",
            "control_plane_unreachable",
            "control_plane_circuit_open",
            "signing_unavailable",
            "telemetry_dropping",
        }

    def test_status_values(self) -> None:
        from checkrd._types import HealthStatus

        assert set(get_args(HealthStatus)) == {
            "healthy",
            "degraded",
            "disabled",
            "error",
        }


class TestTelemetryDiagnostics:
    """`TelemetryDiagnostics` is the shape of the batcher's loss
    counters. Mirrors the JS BatcherDiagnostics interface so
    cross-language dashboards work."""

    def test_field_set(self) -> None:
        from checkrd._types import TelemetryDiagnostics

        assert set(TelemetryDiagnostics.__annotations__.keys()) == {
            "sent",
            "dropped_backpressure",
            "dropped_signing_error",
            "dropped_send_error",
            "pending",
            "last_request_id",
        }


class TestPublicReExport:
    """The new types are re-exported from the top-level ``checkrd``
    package so users can do ``from checkrd import Policy`` without
    reaching into the underscore module."""

    @pytest.mark.parametrize(
        "name",
        [
            "Policy",
            "PolicyRule",
            "PolicyAction",
            "PolicyDefault",
            "HealthCheck",
            "HealthStatus",
            "TelemetryDiagnostics",
        ],
    )
    def test_each_name_is_importable_from_root(self, name: str) -> None:
        import checkrd

        assert hasattr(checkrd, name), (
            f"{name} should be re-exported from checkrd.__init__"
        )
        assert name in checkrd.__all__, (
            f"{name} should be in checkrd.__all__"
        )

    def test_healthy_return_type_is_healthcheck(self) -> None:
        """The `healthy()` function's annotation must be the public
        ``HealthCheck`` TypedDict — accidentally falling back to
        ``dict[str, Any]`` would defeat the whole exercise."""
        import checkrd
        from checkrd._types import HealthCheck

        hints = typing.get_type_hints(checkrd.healthy)
        assert hints["return"] is HealthCheck
