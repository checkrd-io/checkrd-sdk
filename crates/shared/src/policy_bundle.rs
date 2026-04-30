//! Versioned policy bundle wrapper for signed distribution.
//!
//! The control plane wraps every policy in a [`PolicyBundle`] before signing.
//! The bundle binds three pieces of metadata into the signed payload:
//!
//! 1. `schema_version` — bundle wrapper format version, for forward-compat
//! 2. `version` — monotonically increasing policy version (rollback defense)
//! 3. `signed_at` — Unix timestamp at signing time (freshness defense)
//!
//! All three are inside the DSSE-signed bytes, so an attacker can't tamper
//! with them without invalidating the signature. The SDK checks all three
//! before installing the policy:
//!
//! - `schema_version` must match a known version (currently only 1)
//! - `version` must be strictly greater than the highest version the SDK
//!   has previously installed (rollback prevention)
//! - `now - signed_at` must be within the configured maximum age window
//!   (default 24 hours; freshness prevention)
//!
//! # Why a wrapper struct
//!
//! The naive approach would sign the bare `PolicyConfig` JSON. This works
//! cryptographically but provides no replay protection: an attacker who
//! captured a previously-valid policy bundle could replay it on the SSE
//! channel and the SDK would install it (the signature still verifies).
//!
//! Wrapping the policy in a metadata envelope and binding the metadata
//! into the signed bytes is the standard pattern for signed config
//! distribution:
//!
//! - **OPA bundles** use a `.manifest` file with a revision and roots,
//!   verified before the policy files.
//! - **TUF (The Update Framework)** uses a snapshot role with version
//!   numbers and a timestamp role for freshness.
//! - **Notary v2** uses signed manifests with revision IDs.
//! - **Sigstore policy-controller** uses Rekor inclusion proofs with
//!   signed timestamps.
//!
//! Our `PolicyBundle` is the lightweight equivalent: one struct, three
//! metadata fields, no extra round-trips, cryptographically bound via
//! DSSE PAE.

use serde::{Deserialize, Serialize};

use crate::policy::PolicyConfig;

/// Current schema version for the policy bundle wrapper.
///
/// Bump on any breaking change to the [`PolicyBundle`] shape. The SDK
/// verifier rejects bundles with an unknown schema version, so a future
/// SDK can refuse to install bundles produced by a control plane it
/// doesn't understand.
pub const POLICY_BUNDLE_SCHEMA_VERSION: u32 = 1;

/// A signed, versioned policy bundle. The control plane signs the canonical
/// JSON serialization of this struct via DSSE; the SDK verifies and installs
/// the contained policy.
///
/// All metadata fields are inside the signed bytes — see the module docs.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct PolicyBundle {
    /// Wrapper schema version. SDK rejects unknown versions.
    pub schema_version: u32,
    /// Monotonically increasing policy version. SDK persists the highest
    /// version it has installed and rejects any bundle with `version <= seen_max`.
    pub version: u64,
    /// Unix seconds when the control plane signed this bundle. SDK rejects
    /// bundles older than the configured max-age window.
    pub signed_at: u64,
    /// The actual policy that gets installed after verification + monotonicity
    /// + freshness checks pass.
    pub policy: PolicyConfig,
}

impl PolicyBundle {
    /// Construct a new bundle with the current schema version.
    pub fn new(version: u64, signed_at: u64, policy: PolicyConfig) -> Self {
        Self {
            schema_version: POLICY_BUNDLE_SCHEMA_VERSION,
            version,
            signed_at,
            policy,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_policy() -> PolicyConfig {
        serde_json::from_str(r#"{"agent":"test","default":"deny","rules":[]}"#).unwrap()
    }

    #[test]
    fn bundle_round_trips_through_serde_json() {
        let bundle = PolicyBundle::new(42, 1_700_000_000, sample_policy());
        let json = serde_json::to_string(&bundle).unwrap();
        let parsed: PolicyBundle = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed, bundle);
    }

    #[test]
    fn bundle_preserves_all_metadata_fields() {
        let bundle = PolicyBundle::new(99, 1_700_000_500, sample_policy());
        let json = serde_json::to_value(&bundle).unwrap();
        assert_eq!(json["schema_version"], POLICY_BUNDLE_SCHEMA_VERSION);
        assert_eq!(json["version"], 99);
        assert_eq!(json["signed_at"], 1_700_000_500);
        assert!(json["policy"].is_object());
    }

    #[test]
    fn schema_version_constant_starts_at_1() {
        // The constant existing at v1 means this is the inaugural format.
        // Bump on any breaking change.
        assert_eq!(POLICY_BUNDLE_SCHEMA_VERSION, 1);
    }
}
