//! API key scope model — first-class scoped keys.
//!
//! Industry pattern: per-resource × per-access-level matrix, mirroring
//! Stripe Restricted Keys + GitHub fine-grained Personal Access Tokens.
//! Three forms an API key's scope can take:
//!
//! - [`ApiKeyScope::All`] — unrestricted; any resource, any action.
//!   Reserved for keys minted by interactive `checkrd login` (the user
//!   has already authenticated their full identity in a browser).
//! - [`ApiKeyScope::ReadOnly`] — every resource gets [`AccessLevel::Read`].
//!   Preset for the dashboard's "create read-only key" flow and the
//!   common audit/scripting use case.
//! - [`ApiKeyScope::Restricted`] — explicit per-resource matrix. The
//!   primary "fine-grained" mode. Resources not listed default to
//!   [`AccessLevel::None`] (deny-by-default).
//!
//! Stored as JSONB in `api_keys.permissions` with the `kind`
//! discriminator. Wire shape (the forms below all serialize cleanly):
//!
//! ```json
//! {"kind": "all"}
//! {"kind": "read_only"}
//! {"kind": "restricted", "resources": {"agents": "write", "policies": "read"}}
//! ```
//!
//! Enforcement happens at the route boundary via
//! `Principal::require_scope(resource, action)` in each handler. JWT
//! users (`Principal::User`) bypass scope checks — their permissions
//! are governed by `org_members.role` (RBAC) instead. API key
//! principals carry the scope and the route gates on it.
//!
//! No backward compatibility — every key explicitly declares one of
//! the three forms at creation. There is no implicit "missing scope =
//! full access" fallback.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// Resource taxonomy. One-to-one with `crates/api/src/routes/*.rs`
/// modules so the wire shape mirrors the URL structure (`/v1/agents`
/// → [`Resource::Agents`], etc.).
///
/// Adding a new resource: add a variant + mention it in the dashboard
/// "Create API key" matrix. Removing one is a breaking change —
/// existing restricted keys would drop the resource, silently
/// downgrading to None. Don't.
#[derive(
    Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize,
)]
#[serde(rename_all = "snake_case")]
pub enum Resource {
    /// `/v1/agents/*` — agent CRUD, kill switch, public key registration.
    Agents,
    /// `/v1/agents/:id/policies/*` — per-agent policy versioning + activation.
    Policies,
    /// `/v1/org-policies/*` — org-default policies (SCP-style merge).
    OrgPolicies,
    /// `/v1/policy-templates/*` — built-in policy template catalog +
    /// render.
    Templates,
    /// `/v1/keys/*` — API key administration. A key with
    /// `keys: write` can mint other keys, so this is effectively
    /// privilege-escalation-adjacent — restrict deliberately.
    Keys,
    /// `/v1/alerts/*` — alert rule CRUD + transition history.
    Alerts,
    /// `/v1/dashboard/events` — paginated telemetry events (read-only
    /// in practice; `write` is undefined and rejected by the route).
    Events,
    /// `/v1/audit-log/*` — partitioned audit log reads. Always
    /// read-only at the API layer; `write` is meaningless.
    Audit,
    /// `/v1/dashboard/*` — stats + timeseries aggregates. Read-only.
    Dashboard,
    /// `/v1/billing/*` — Stripe checkout / portal / status. `write`
    /// implies starting a checkout session in the org's name.
    Billing,
    /// `/v1/orgs/*` — workspace management. Most ops are
    /// JWT-session-only by design (the API-key path is gated to org
    /// reads); listing this resource lets a key see its own org
    /// metadata.
    Orgs,
}

/// Access level per resource. Ordered: `None < Read < Write` so a
/// `permits()` check is a single `<=`.
///
/// `Write` implies `Read` (read-modify-write). There is no separate
/// `Delete` or `Admin` level; cross-resource sensitive ops (revoking
/// a key, deleting an org, toggling a kill switch) are gated by the
/// creator's role-based-access checks (`live_role_for_principal`),
/// not the per-resource scope. Scopes constrain WHAT the key touches;
/// roles constrain WHICH operations within that.
#[derive(
    Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize,
)]
#[serde(rename_all = "snake_case")]
pub enum AccessLevel {
    None,
    Read,
    Write,
}

/// Scope carried by every API key. Three forms; serializes with a
/// `kind` discriminator (Serde "internally tagged" enum).
///
/// Wire-format examples:
///
/// ```json
/// {"kind": "all"}
/// {"kind": "read_only"}
/// {"kind": "restricted", "resources": {"agents": "write", "policies": "read"}}
/// ```
///
/// Resources omitted from a `Restricted` map default to
/// [`AccessLevel::None`] (deny-by-default), matching Stripe's
/// "unticked checkbox = no access".
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ApiKeyScope {
    /// Unrestricted. Reserved for `checkrd login` device-flow keys —
    /// the user has authenticated their full identity through WorkOS,
    /// so the resulting CLI key inherits full access. Keys minted via
    /// `POST /v1/keys` should never default to `All`.
    All,
    /// Every resource, read level. Common preset for audit /
    /// observability / CI scripts that only need to read. Mutations
    /// return `403 scope_insufficient`.
    ReadOnly,
    /// Explicit per-resource matrix. Anything not in `resources`
    /// defaults to [`AccessLevel::None`].
    Restricted {
        resources: BTreeMap<Resource, AccessLevel>,
    },
}

impl ApiKeyScope {
    /// Does the key permit `action` on `resource`?
    ///
    /// Used by `Principal::require_scope` to gate every `/v1/*`
    /// handler. JWT-shaped principals bypass this check entirely.
    pub fn permits(&self, resource: Resource, action: AccessLevel) -> bool {
        let granted = self.granted(resource);
        action <= granted
    }

    /// Effective access level for the given resource. Returns
    /// `Write` for `All` (everything), `Read` for `ReadOnly`, and the
    /// map lookup (defaulting to `None`) for `Restricted`.
    pub fn granted(&self, resource: Resource) -> AccessLevel {
        match self {
            Self::All => AccessLevel::Write,
            Self::ReadOnly => AccessLevel::Read,
            Self::Restricted { resources } => {
                resources.get(&resource).copied().unwrap_or(AccessLevel::None)
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn restricted(map: &[(Resource, AccessLevel)]) -> ApiKeyScope {
        ApiKeyScope::Restricted {
            resources: map.iter().copied().collect(),
        }
    }

    #[test]
    fn all_permits_everything() {
        let s = ApiKeyScope::All;
        for r in [Resource::Agents, Resource::Keys, Resource::Audit] {
            for a in [AccessLevel::None, AccessLevel::Read, AccessLevel::Write] {
                assert!(s.permits(r, a), "All should permit {:?} {:?}", r, a);
            }
        }
    }

    #[test]
    fn read_only_permits_read_denies_write() {
        let s = ApiKeyScope::ReadOnly;
        assert!(s.permits(Resource::Agents, AccessLevel::Read));
        assert!(!s.permits(Resource::Agents, AccessLevel::Write));
        assert!(s.permits(Resource::Audit, AccessLevel::Read));
        assert!(!s.permits(Resource::Keys, AccessLevel::Write));
    }

    #[test]
    fn restricted_defaults_unlisted_to_none() {
        let s = restricted(&[(Resource::Agents, AccessLevel::Write)]);
        assert!(s.permits(Resource::Agents, AccessLevel::Write));
        assert!(s.permits(Resource::Agents, AccessLevel::Read));
        // Unlisted resource: deny-by-default
        assert!(!s.permits(Resource::Keys, AccessLevel::Read));
        assert!(!s.permits(Resource::Policies, AccessLevel::Read));
    }

    #[test]
    fn restricted_read_does_not_imply_write() {
        let s = restricted(&[(Resource::Policies, AccessLevel::Read)]);
        assert!(s.permits(Resource::Policies, AccessLevel::Read));
        assert!(!s.permits(Resource::Policies, AccessLevel::Write));
    }

    #[test]
    fn wire_shape_round_trips() {
        let scope = restricted(&[
            (Resource::Agents, AccessLevel::Write),
            (Resource::Audit, AccessLevel::Read),
        ]);
        let s = serde_json::to_string(&scope).unwrap();
        // BTreeMap serializes in sorted key order — deterministic.
        assert!(s.contains(r#""kind":"restricted""#), "got: {s}");
        assert!(s.contains(r#""agents":"write""#), "got: {s}");
        assert!(s.contains(r#""audit":"read""#), "got: {s}");
        let back: ApiKeyScope = serde_json::from_str(&s).unwrap();
        assert_eq!(back, scope);
    }

    #[test]
    fn wire_shape_all_and_read_only() {
        let all = serde_json::to_string(&ApiKeyScope::All).unwrap();
        assert_eq!(all, r#"{"kind":"all"}"#);
        let ro = serde_json::to_string(&ApiKeyScope::ReadOnly).unwrap();
        assert_eq!(ro, r#"{"kind":"read_only"}"#);

        let all_back: ApiKeyScope = serde_json::from_str(r#"{"kind":"all"}"#).unwrap();
        assert_eq!(all_back, ApiKeyScope::All);
    }

    #[test]
    fn access_level_ordering_matches_check_logic() {
        assert!(AccessLevel::None < AccessLevel::Read);
        assert!(AccessLevel::Read < AccessLevel::Write);
        assert!(AccessLevel::Write > AccessLevel::None);
    }
}
