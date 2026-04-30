//! Billing plan tiers and their rate limits.
//!
//! This is the **single source of truth** for the billing contract. Both the API
//! service and the telemetry-ingestion service import `PlanTier` to determine
//! per-org rate limits. When pricing changes, update this one enum and both
//! services recompile with the new limits.
//!
//! This module has zero I/O dependencies — it is pure data. It can be imported
//! by the WASM core without pulling in Redis, HTTP, or database crates.

use serde::{Deserialize, Serialize};
use typeshare::typeshare;

/// Billing plan tier stored in `organizations.plan_tier`.
#[typeshare]
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PlanTier {
    Free,
    Team,
    Enterprise,
}

impl PlanTier {
    /// Parse from the database `plan_tier` text column.
    /// Unknown values default to `Free` (fail-safe: most restrictive tier).
    pub fn from_db(s: &str) -> Self {
        match s {
            "free" => Self::Free,
            "team" => Self::Team,
            "enterprise" => Self::Enterprise,
            _ => Self::Free,
        }
    }

    /// String representation for database writes and error messages.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Free => "free",
            Self::Team => "team",
            Self::Enterprise => "enterprise",
        }
    }

    /// Per-org telemetry ingestion rate limit: `(max_requests, window_ms)`.
    /// Returns `None` for unlimited tiers (enterprise).
    pub fn telemetry_rate_limit(self) -> Option<(u32, u64)> {
        match self {
            Self::Free => Some((100, 60_000)),   // 100 requests per minute
            Self::Team => Some((1_000, 60_000)), // 1,000 requests per minute
            Self::Enterprise => None,            // unlimited
        }
    }

    /// Resource and feature limits for this plan tier.
    ///
    /// These are compile-time constants, not database rows. Enforcement middleware
    /// checks these without a DB query, and they deploy atomically with code.
    pub fn limits(self) -> PlanLimits {
        match self {
            Self::Free => PlanLimits {
                max_agents: 5,
                max_members: 1,
                max_api_keys: 2,
                max_events_per_month: 100_000,
                data_retention_days: 7,
                sso_enabled: false,
                audit_log_enabled: false,
            },
            Self::Team => PlanLimits {
                max_agents: 50,
                max_members: 20,
                max_api_keys: 20,
                max_events_per_month: 1_000_000,
                data_retention_days: 90,
                sso_enabled: false,
                audit_log_enabled: true,
            },
            Self::Enterprise => PlanLimits {
                max_agents: u32::MAX,
                max_members: u32::MAX,
                max_api_keys: u32::MAX,
                max_events_per_month: u64::MAX,
                data_retention_days: 365,
                sso_enabled: true,
                audit_log_enabled: true,
            },
        }
    }
}

/// Resource counts and feature flags for a billing tier.
///
/// Used by the tier enforcement middleware to check plan limits on resource
/// creation and feature access. Lives in `crates/shared` (no I/O deps) so it
/// can be imported by any service including the WASM core.
///
/// Serializable so the billing status endpoint can return limits to the dashboard.
//
// The wire DTO with the same name lives in `api-types/src/billing.rs`
// and is the typeshare-emitted version. Dual annotation produces
// order-non-deterministic output across CI vs local — keep typeshare
// on the api-types copy only.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct PlanLimits {
    pub max_agents: u32,
    pub max_members: u32,
    pub max_api_keys: u32,
    // u64 on the wire (enterprise = u64::MAX). Serialized as JSON
    // number; consumers via the api-types copy honor the TS `number`
    // mapping. We never round-trip this server-side.
    pub max_events_per_month: u64,
    pub data_retention_days: u32,
    pub sso_enabled: bool,
    pub audit_log_enabled: bool,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn from_db_known_tiers() {
        assert_eq!(PlanTier::from_db("free"), PlanTier::Free);
        assert_eq!(PlanTier::from_db("team"), PlanTier::Team);
        assert_eq!(PlanTier::from_db("enterprise"), PlanTier::Enterprise);
    }

    #[test]
    fn from_db_unknown_defaults_to_free() {
        assert_eq!(PlanTier::from_db("starter"), PlanTier::Free);
        assert_eq!(PlanTier::from_db(""), PlanTier::Free);
        assert_eq!(PlanTier::from_db("FREE"), PlanTier::Free); // case-sensitive
    }

    // -- Telemetry rate limits (regression guards) --

    #[test]
    fn free_telemetry_rate_limit() {
        let (limit, window) = PlanTier::Free.telemetry_rate_limit().unwrap();
        assert_eq!(limit, 100);
        assert_eq!(window, 60_000);
    }

    #[test]
    fn team_telemetry_rate_limit() {
        let (limit, window) = PlanTier::Team.telemetry_rate_limit().unwrap();
        assert_eq!(limit, 1_000);
        assert_eq!(window, 60_000);
    }

    #[test]
    fn enterprise_telemetry_unlimited() {
        assert!(PlanTier::Enterprise.telemetry_rate_limit().is_none());
    }

    // -- Plan limits --

    #[test]
    fn free_plan_limits() {
        let limits = PlanTier::Free.limits();
        assert_eq!(limits.max_agents, 5);
        assert_eq!(limits.max_members, 1);
        assert_eq!(limits.max_api_keys, 2);
        assert_eq!(limits.max_events_per_month, 100_000);
        assert_eq!(limits.data_retention_days, 7);
        assert!(!limits.sso_enabled);
        assert!(!limits.audit_log_enabled);
    }

    #[test]
    fn team_plan_limits() {
        let limits = PlanTier::Team.limits();
        assert_eq!(limits.max_agents, 50);
        assert_eq!(limits.max_members, 20);
        assert_eq!(limits.max_api_keys, 20);
        assert_eq!(limits.max_events_per_month, 1_000_000);
        assert_eq!(limits.data_retention_days, 90);
        assert!(!limits.sso_enabled);
        assert!(limits.audit_log_enabled);
    }

    #[test]
    fn enterprise_plan_limits() {
        let limits = PlanTier::Enterprise.limits();
        assert_eq!(limits.max_agents, u32::MAX);
        assert_eq!(limits.max_members, u32::MAX);
        assert_eq!(limits.max_api_keys, u32::MAX);
        assert_eq!(limits.max_events_per_month, u64::MAX);
        assert_eq!(limits.data_retention_days, 365);
        assert!(limits.sso_enabled);
        assert!(limits.audit_log_enabled);
    }

    #[test]
    fn unknown_tier_gets_free_limits() {
        // Fail closed: unknown tiers get the most restrictive limits
        let tier = PlanTier::from_db("unknown_garbage");
        let limits = tier.limits();
        assert_eq!(limits, PlanTier::Free.limits());
    }

    #[test]
    fn plan_tier_serde_roundtrip() {
        for (tier, expected) in [
            (PlanTier::Free, "\"free\""),
            (PlanTier::Team, "\"team\""),
            (PlanTier::Enterprise, "\"enterprise\""),
        ] {
            let serialized = serde_json::to_string(&tier).unwrap();
            assert_eq!(serialized, expected);
            let deserialized: PlanTier = serde_json::from_str(&serialized).unwrap();
            assert_eq!(deserialized, tier);
        }
    }

    #[test]
    fn as_str_roundtrips_with_from_db() {
        for tier in [PlanTier::Free, PlanTier::Team, PlanTier::Enterprise] {
            assert_eq!(PlanTier::from_db(tier.as_str()), tier);
        }
    }

    // -- Hierarchy invariants --
    // Higher tiers must be strict supersets of lower tiers. These tests prevent
    // regressions where a code change accidentally gives Free more resources
    // than Team, or removes a feature from Enterprise that Team has.

    #[test]
    fn tier_hierarchy_resource_limits_increase() {
        let free = PlanTier::Free.limits();
        let team = PlanTier::Team.limits();
        let enterprise = PlanTier::Enterprise.limits();

        // Team >= Free for all resource counts
        assert!(team.max_agents >= free.max_agents);
        assert!(team.max_members >= free.max_members);
        assert!(team.max_api_keys >= free.max_api_keys);
        assert!(team.max_events_per_month >= free.max_events_per_month);
        assert!(team.data_retention_days >= free.data_retention_days);

        // Enterprise >= Team for all resource counts
        assert!(enterprise.max_agents >= team.max_agents);
        assert!(enterprise.max_members >= team.max_members);
        assert!(enterprise.max_api_keys >= team.max_api_keys);
        assert!(enterprise.max_events_per_month >= team.max_events_per_month);
        assert!(enterprise.data_retention_days >= team.data_retention_days);
    }

    #[test]
    fn tier_hierarchy_features_only_add() {
        let free = PlanTier::Free.limits();
        let team = PlanTier::Team.limits();
        let enterprise = PlanTier::Enterprise.limits();

        // If Free has a feature, Team must also have it
        if free.sso_enabled {
            assert!(team.sso_enabled);
        }
        if free.audit_log_enabled {
            assert!(team.audit_log_enabled);
        }

        // If Team has a feature, Enterprise must also have it
        if team.sso_enabled {
            assert!(enterprise.sso_enabled);
        }
        if team.audit_log_enabled {
            assert!(enterprise.audit_log_enabled);
        }
    }

    #[test]
    fn plan_limits_serializes_for_api_response() {
        let limits = PlanTier::Free.limits();
        let json = serde_json::to_value(limits).unwrap();
        assert_eq!(json["max_agents"], 5);
        assert_eq!(json["max_members"], 1);
        assert_eq!(json["sso_enabled"], false);
        assert_eq!(json["data_retention_days"], 7);
    }
}
