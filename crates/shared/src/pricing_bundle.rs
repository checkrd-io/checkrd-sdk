//! Versioned pricing bundle for signed list-price distribution (TDD App. E).
//!
//! A [`PricingBundle`] is to cost metering what [`PolicyBundle`](crate::policy_bundle)
//! is to enforcement: the control plane signs the canonical JSON of this struct
//! via DSSE, the SDK verifies it in-WASM against a pinned trust list, and the
//! core then computes each call's cost from the contained [`PriceSku`] rows —
//! *inside the Ed25519-signed telemetry batch* (ADR-002), so the spend figure
//! is an independently verifiable record rather than a wrapper-side estimate.
//!
//! It is a deliberate structural clone of the policy bundle and inherits the
//! same defenses, all bound into the signed bytes:
//!
//! 1. `schema_version` — wrapper format version; the verifier rejects unknown
//!    versions (FFI `-20`).
//! 2. `version` — monotonically increasing; a bundle whose version is not
//!    strictly greater than the highest installed is rejected as a rollback
//!    (FFI `-21`), the TUF "never replace with a lower version number" rule.
//! 3. `signed_at` — Unix seconds at signing; bundles older than the configured
//!    max-age (FFI `-22`) or dated in the future beyond a small skew (FFI `-23`)
//!    are rejected, for freshness.
//!
//! A tampered price table is an *integrity attack on money*, so it gets the
//! codebase's strongest defenses — the same OPA-bundle / TUF / Notary lineage
//! documented on [`PolicyBundle`](crate::policy_bundle). The DSSE PAE binds the
//! distinct `application/vnd.checkrd.pricing-bundle+json` payload type
//! ([`PRICING_BUNDLE_PAYLOAD_TYPE`](crate::dsse::PRICING_BUNDLE_PAYLOAD_TYPE)),
//! so a policy or telemetry signature can never be replayed as a price table.

use serde::{Deserialize, Serialize};

/// Current schema version for the pricing bundle wrapper. The SDK verifier
/// rejects bundles with an unknown schema version (FFI `-20`), so a future
/// control plane can ship a v2 shape that older SDKs refuse rather than
/// misinterpret.
pub const PRICING_BUNDLE_SCHEMA_VERSION: u32 = 1;

/// Rounding mode applied when converting a token count and a per-unit price
/// into integer micro-USD. Declared *inside the signed bundle* so any
/// independent implementation reproduces byte-identical signed cost (ADR-003).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Rounding {
    /// Round halves toward positive infinity. On the non-negative money domain
    /// this equals round-half-away-from-zero. See [`crate::cost`].
    HalfUp,
}

/// The unit a [`PriceSku`]'s `*_usd_micros_per_unit` price is quoted in. An
/// enum (rather than a bare constant) so a future unit can be added without a
/// breaking bundle-schema change; v1 has the single LLM-pricing convention.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum PriceUnit {
    /// Price is per 1,000,000 tokens (see [`crate::cost::TOKENS_PER_PRICE_UNIT`]).
    #[serde(rename = "per_1m_tokens")]
    Per1mTokens,
}

/// Where a [`PriceSku`] came from. `Org` rows overlay `List` rows via merge
/// precedence (an org-negotiated rate wins over the published list price).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PriceSource {
    /// Published list price.
    List,
    /// Org-negotiated overlay rate.
    Org,
}

/// One priced model (or model glob) within a [`PricingBundle`].
///
/// SKU resolution (M-4 `settle_usage`) reuses the audited URL glob grammar from
/// [`crate::url`] on `model_match`: `*` matches one segment, `**` zero-or-more,
/// and specificity is the literal-segment count. The most specific match wins;
/// ties break to the newest `effective_from`; no match yields
/// `pricing_status = "unpriced_model"` (fail-open metering — an unknown model
/// marks the event, never blocks the call).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PriceSku {
    /// Stable identifier for this SKU; surfaces as the FOCUS `SkuId`.
    pub sku_id: String,
    /// OTel `gen_ai` well-known provider name (`openai`, `anthropic`, …).
    pub provider: String,
    /// Glob over the model id (`*`, `**` per [`crate::url`]).
    pub model_match: String,
    /// Unit the per-unit prices below are quoted in.
    pub unit: PriceUnit,
    /// Micro-USD per unit of input tokens.
    pub input_usd_micros_per_unit: i64,
    /// Micro-USD per unit of output tokens.
    pub output_usd_micros_per_unit: i64,
    /// Micro-USD per unit of cache-read input tokens, when priced separately.
    #[serde(default)]
    pub cache_read_usd_micros_per_unit: Option<i64>,
    /// Micro-USD per unit of cache-write / cache-creation tokens, when priced
    /// separately.
    #[serde(default)]
    pub cache_write_usd_micros_per_unit: Option<i64>,
    /// Output-token cap used to size the pre-flight reserve when a request does
    /// not specify its own `max_tokens` (ADR-008 reserve fallback).
    pub default_max_output_tokens: u32,
    /// Unix seconds from which this SKU is in effect.
    pub effective_from: u64,
    /// Unix seconds after which this SKU should no longer be used, if set.
    #[serde(default)]
    pub deprecated_after: Option<u64>,
    /// Whether this is a list price or an org-negotiated overlay.
    pub source: PriceSource,
}

/// A signed, versioned pricing bundle. The control plane signs the canonical
/// JSON serialization of this struct via DSSE; the SDK verifies and installs it,
/// then the core computes per-call cost from `skus`.
///
/// All metadata fields are inside the signed bytes — see the module docs.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PricingBundle {
    /// Wrapper schema version. SDK rejects unknown versions (FFI `-20`).
    pub schema_version: u32,
    /// Monotonically increasing bundle version. SDK persists the highest
    /// version installed and rejects any bundle with `version <= seen_max`
    /// (FFI `-21`).
    pub version: u64,
    /// Unix seconds when the control plane signed this bundle. SDK rejects
    /// bundles outside the configured max-age / future-skew window
    /// (FFI `-22` / `-23`).
    pub signed_at: u64,
    /// Rounding mode for cost computation, bound into the signed bytes.
    pub rounding: Rounding,
    /// The priced models. Resolution is most-specific-glob-wins (see
    /// [`PriceSku`]).
    pub skus: Vec<PriceSku>,
}

impl PricingBundle {
    /// Construct a new bundle with the current schema version.
    pub fn new(version: u64, signed_at: u64, rounding: Rounding, skus: Vec<PriceSku>) -> Self {
        Self {
            schema_version: PRICING_BUNDLE_SCHEMA_VERSION,
            version,
            signed_at,
            rounding,
            skus,
        }
    }
}

/// Merge org-negotiated overlay SKUs over the base list-price catalog.
///
/// An overlay row (`source: Org`) shadows a base list row with the SAME
/// `(provider, model_match)` key — the org-negotiated rate replaces the list
/// price for that exact SKU. Base rows with no matching overlay are kept, and
/// overlay rows with no matching base row are appended. The core still resolves
/// the most-specific glob over the merged set at settle time (ties → newest
/// `effective_from`), so this decides which rows are *present*, not which wins
/// at a given specificity.
///
/// The control plane merges once, signs the result, and the SDK installs it
/// verbatim — mirroring [`merge_policies`](crate::merge_policies) (agent rules
/// replace org rules by name) and the same server-side-merge / verify-only-SDK
/// pattern (Envoy xDS, OPA bundles). Base-row order is preserved; overlays keep
/// their given order and follow the surviving base rows.
pub fn merge_pricing(base: &[PriceSku], overlays: &[PriceSku]) -> Vec<PriceSku> {
    use std::collections::HashSet;

    let overridden: HashSet<(&str, &str)> = overlays
        .iter()
        .map(|s| (s.provider.as_str(), s.model_match.as_str()))
        .collect();

    let mut merged: Vec<PriceSku> = base
        .iter()
        .filter(|s| !overridden.contains(&(s.provider.as_str(), s.model_match.as_str())))
        .cloned()
        .collect();
    merged.extend(overlays.iter().cloned());
    merged
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_sku(sku_id: &str, model_match: &str, with_cache: bool) -> PriceSku {
        PriceSku {
            sku_id: sku_id.to_string(),
            provider: "anthropic".to_string(),
            model_match: model_match.to_string(),
            unit: PriceUnit::Per1mTokens,
            input_usd_micros_per_unit: 3_000_000,
            output_usd_micros_per_unit: 15_000_000,
            cache_read_usd_micros_per_unit: with_cache.then_some(300_000),
            cache_write_usd_micros_per_unit: with_cache.then_some(3_750_000),
            default_max_output_tokens: 4096,
            effective_from: 1_700_000_000,
            deprecated_after: None,
            source: PriceSource::List,
        }
    }

    fn sample_bundle() -> PricingBundle {
        PricingBundle::new(
            7,
            1_750_000_000,
            Rounding::HalfUp,
            vec![
                sample_sku("anthropic-claude-sonnet", "claude-sonnet-*", true),
                sample_sku("anthropic-default", "**", false),
            ],
        )
    }

    #[test]
    fn new_stamps_the_current_schema_version() {
        assert_eq!(
            sample_bundle().schema_version,
            PRICING_BUNDLE_SCHEMA_VERSION
        );
        assert_eq!(PRICING_BUNDLE_SCHEMA_VERSION, 1);
    }

    #[test]
    fn serde_round_trip_preserves_every_field() {
        let bundle = sample_bundle();
        let json = serde_json::to_string(&bundle).unwrap();
        let back: PricingBundle = serde_json::from_str(&json).unwrap();
        assert_eq!(bundle, back);
    }

    #[test]
    fn optional_sku_prices_default_to_none_when_absent() {
        // A producer that omits the cache fields (older bundle) still parses.
        let json = r#"{
            "sku_id":"x","provider":"openai","model_match":"gpt-4o",
            "unit":"per_1m_tokens","input_usd_micros_per_unit":2500000,
            "output_usd_micros_per_unit":10000000,"default_max_output_tokens":4096,
            "effective_from":1700000000,"source":"list"
        }"#;
        let sku: PriceSku = serde_json::from_str(json).unwrap();
        assert_eq!(sku.cache_read_usd_micros_per_unit, None);
        assert_eq!(sku.cache_write_usd_micros_per_unit, None);
        assert_eq!(sku.deprecated_after, None);
    }

    #[test]
    fn enum_wire_values_are_pinned() {
        // These strings are inside the SIGNED bytes — a rename is a breaking,
        // signature-invalidating change and must be deliberate.
        assert_eq!(
            serde_json::to_string(&Rounding::HalfUp).unwrap(),
            "\"half_up\""
        );
        assert_eq!(
            serde_json::to_string(&PriceUnit::Per1mTokens).unwrap(),
            "\"per_1m_tokens\""
        );
        assert_eq!(
            serde_json::to_string(&PriceSource::List).unwrap(),
            "\"list\""
        );
        assert_eq!(serde_json::to_string(&PriceSource::Org).unwrap(), "\"org\"");
    }

    /// The committed JSON Schema is the contract the SDK fixtures and FOCUS
    /// export build on. This guards the Rust struct against drifting from it:
    /// every serialized key must be a declared property, and every required
    /// property must be present on a fully-populated value. (No `jsonschema`
    /// crate in the workspace, so this is a structural conformance check.)
    #[test]
    fn struct_conforms_to_committed_json_schema() {
        let schema: serde_json::Value =
            serde_json::from_str(include_str!("../../../schemas/pricing-bundle.schema.json"))
                .expect("schema is valid JSON");
        assert_eq!(schema["$id"], "checkrd-pricing-bundle");

        let check = |value: &serde_json::Value,
                     props: &serde_json::Value,
                     required: &serde_json::Value,
                     ctx: &str| {
            let prop_names: std::collections::BTreeSet<&str> = props
                .as_object()
                .unwrap()
                .keys()
                .map(String::as_str)
                .collect();
            let obj = value.as_object().unwrap();
            for key in obj.keys() {
                assert!(
                    prop_names.contains(key.as_str()),
                    "{ctx}: serialized key `{key}` is not in the schema"
                );
            }
            for req in required.as_array().unwrap() {
                let req = req.as_str().unwrap();
                assert!(
                    obj.contains_key(req),
                    "{ctx}: required property `{req}` missing from value"
                );
            }
        };

        let bundle = serde_json::to_value(sample_bundle()).unwrap();
        check(
            &bundle,
            &schema["properties"],
            &schema["required"],
            "PricingBundle",
        );

        let sku_schema = &schema["$defs"]["priceSku"];
        // Use the cache-bearing SKU so every optional property is also exercised.
        check(
            &bundle["skus"][0],
            &sku_schema["properties"],
            &sku_schema["required"],
            "PriceSku",
        );
        // Also exercise the SKU that OMITS the optional cache/deprecation
        // fields, so the conformance check proves the *required* set is
        // satisfiable without the optional ones (and that omission doesn't
        // emit some unexpected key).
        check(
            &bundle["skus"][1],
            &sku_schema["properties"],
            &sku_schema["required"],
            "PriceSku",
        );
    }

    /// Cross-check the schema's enum `$defs` against the Rust types' actual wire
    /// strings. The `enum_wire_values_are_pinned` test pins what Rust emits; this
    /// pins that the committed schema *accepts exactly that set and no more*, so
    /// the two artifacts can't drift apart (a renamed variant that updated only
    /// one side would be a silent signature/contract break).
    #[test]
    fn schema_enum_defs_match_rust_wire_strings() {
        let schema: serde_json::Value =
            serde_json::from_str(include_str!("../../../schemas/pricing-bundle.schema.json"))
                .expect("schema is valid JSON");

        let enum_set = |def: &str| -> std::collections::BTreeSet<String> {
            schema["$defs"][def]["enum"]
                .as_array()
                .unwrap_or_else(|| panic!("$defs.{def}.enum missing"))
                .iter()
                .map(|v| v.as_str().unwrap().to_string())
                .collect()
        };

        // Each Rust enum serializes to exactly the schema's enum set. Build the
        // expected set from the actual serialization so a Rust-side rename is
        // caught here too, not just a schema-side edit.
        let rounding_wire = serde_json::to_value(Rounding::HalfUp).unwrap();
        assert_eq!(
            enum_set("rounding"),
            std::collections::BTreeSet::from([rounding_wire.as_str().unwrap().to_string()])
        );

        let unit_wire = serde_json::to_value(PriceUnit::Per1mTokens).unwrap();
        assert_eq!(
            enum_set("priceUnit"),
            std::collections::BTreeSet::from([unit_wire.as_str().unwrap().to_string()])
        );

        let source_wire: std::collections::BTreeSet<String> = [PriceSource::List, PriceSource::Org]
            .iter()
            .map(|s| {
                serde_json::to_value(s)
                    .unwrap()
                    .as_str()
                    .unwrap()
                    .to_string()
            })
            .collect();
        assert_eq!(enum_set("priceSource"), source_wire);
    }

    #[test]
    fn org_source_serializes_on_the_wire_in_a_bundle() {
        // The Org overlay variant is what makes org-negotiated rates win merge
        // precedence; confirm it survives a full bundle round-trip AND lands as
        // the exact lowercase string inside the signed JSON, not just in the
        // isolated-enum test above.
        let mut sku = sample_sku("org-rate", "gpt-4o", false);
        sku.source = PriceSource::Org;
        let bundle = PricingBundle::new(1, 1_750_000_000, Rounding::HalfUp, vec![sku]);
        let value = serde_json::to_value(&bundle).unwrap();
        assert_eq!(value["skus"][0]["source"], "org");
        let back: PricingBundle = serde_json::from_value(value).unwrap();
        assert_eq!(back.skus[0].source, PriceSource::Org);
    }

    #[test]
    fn unknown_fields_are_ignored_for_forward_compat() {
        // serde's default is to ignore unknown fields. A future control plane
        // that adds a field to a SKU must still deserialize on an older SDK
        // (the verifier rejects unknown *schema_version*, not unknown keys).
        // This pins the forward-compat posture explicitly so adding
        // `#[serde(deny_unknown_fields)]` later is a conscious, tested decision.
        let json = r#"{
            "sku_id":"x","provider":"openai","model_match":"gpt-4o",
            "unit":"per_1m_tokens","input_usd_micros_per_unit":2500000,
            "output_usd_micros_per_unit":10000000,"default_max_output_tokens":4096,
            "effective_from":1700000000,"source":"list",
            "future_field_from_a_newer_control_plane":"ignored",
            "another_unknown":42
        }"#;
        let sku: PriceSku = serde_json::from_str(json).expect("unknown fields must not fail");
        assert_eq!(sku.sku_id, "x");
        assert_eq!(sku.input_usd_micros_per_unit, 2_500_000);
    }

    #[test]
    fn malformed_bundles_fail_cleanly_without_panicking() {
        // Deserialization of garbage / wrong-shaped input must return an Err,
        // never panic — the verifier turns this into a clean rejection rather
        // than crashing the agent's process. Each case targets a distinct
        // failure mode.
        let cases: &[&str] = &[
            // Not even JSON.
            "}{ not json",
            // Empty input.
            "",
            // Right type but missing every required field.
            "{}",
            // Unknown enum string for a signed field (would change the PAE).
            r#"{"sku_id":"x","provider":"p","model_match":"m","unit":"per_9000_tokens",
                "input_usd_micros_per_unit":1,"output_usd_micros_per_unit":1,
                "default_max_output_tokens":1,"effective_from":1,"source":"list"}"#,
            // Wrong JSON type for a numeric field (string where i64 expected).
            r#"{"sku_id":"x","provider":"p","model_match":"m","unit":"per_1m_tokens",
                "input_usd_micros_per_unit":"not-a-number","output_usd_micros_per_unit":1,
                "default_max_output_tokens":1,"effective_from":1,"source":"list"}"#,
            // Negative where an unsigned (u32) field is required.
            r#"{"sku_id":"x","provider":"p","model_match":"m","unit":"per_1m_tokens",
                "input_usd_micros_per_unit":1,"output_usd_micros_per_unit":1,
                "default_max_output_tokens":-5,"effective_from":1,"source":"list"}"#,
        ];
        for case in cases {
            let sku: Result<PriceSku, _> = serde_json::from_str(case);
            assert!(sku.is_err(), "expected clean Err for malformed SKU: {case}");
        }

        // And a malformed bundle wrapper (skus is not an array).
        let bad_bundle =
            r#"{"schema_version":1,"version":1,"signed_at":1,"rounding":"half_up","skus":{}}"#;
        let bundle: Result<PricingBundle, _> = serde_json::from_str(bad_bundle);
        assert!(bundle.is_err(), "skus must be an array");
    }

    // --- merge_pricing (org overlay precedence) ------------------------------

    fn sku(sku_id: &str, provider: &str, model_match: &str, source: PriceSource) -> PriceSku {
        PriceSku {
            sku_id: sku_id.to_string(),
            provider: provider.to_string(),
            model_match: model_match.to_string(),
            unit: PriceUnit::Per1mTokens,
            input_usd_micros_per_unit: 3_000_000,
            output_usd_micros_per_unit: 15_000_000,
            cache_read_usd_micros_per_unit: None,
            cache_write_usd_micros_per_unit: None,
            default_max_output_tokens: 4096,
            effective_from: 1_700_000_000,
            deprecated_after: None,
            source,
        }
    }

    #[test]
    fn merge_with_empty_overlays_returns_the_base_unchanged() {
        // The live path today (no org overlays) must be a faithful passthrough.
        let base = vec![
            sku("a", "openai", "gpt-4o*", PriceSource::List),
            sku("b", "anthropic", "claude-*", PriceSource::List),
        ];
        assert_eq!(merge_pricing(&base, &[]), base);
    }

    #[test]
    fn org_overlay_shadows_the_same_key_base_row_only() {
        let base = vec![
            sku("list-4o", "openai", "gpt-4o*", PriceSource::List),
            sku("list-mini", "openai", "gpt-4o-mini*", PriceSource::List),
        ];
        // Org-negotiated rate for gpt-4o* replaces the list row for that exact
        // (provider, model_match); the mini row is untouched.
        let overlays = vec![sku("org-4o", "openai", "gpt-4o*", PriceSource::Org)];
        let merged = merge_pricing(&base, &overlays);

        assert_eq!(merged.len(), 2);
        // The list gpt-4o* row is gone, replaced by the org one.
        assert!(!merged.iter().any(|s| s.sku_id == "list-4o"));
        assert!(merged
            .iter()
            .any(|s| s.sku_id == "org-4o" && s.source == PriceSource::Org));
        // The unrelated list row survives verbatim.
        assert!(merged
            .iter()
            .any(|s| s.sku_id == "list-mini" && s.source == PriceSource::List));
    }

    #[test]
    fn org_overlay_with_no_base_match_is_appended() {
        let base = vec![sku("list-4o", "openai", "gpt-4o*", PriceSource::List)];
        // A negotiated SKU for a model the list catalog doesn't carry.
        let overlays = vec![sku("org-custom", "openai", "ft:acme-*", PriceSource::Org)];
        let merged = merge_pricing(&base, &overlays);

        assert_eq!(merged.len(), 2);
        assert!(merged.iter().any(|s| s.sku_id == "list-4o"));
        assert!(merged.iter().any(|s| s.sku_id == "org-custom"));
    }

    #[test]
    fn same_model_match_but_different_provider_is_not_shadowed() {
        // The override key is (provider, model_match) — a same-glob overlay for
        // a DIFFERENT provider must not shadow the base row.
        let base = vec![sku("list", "openai", "*", PriceSource::List)];
        let overlays = vec![sku("org", "anthropic", "*", PriceSource::Org)];
        let merged = merge_pricing(&base, &overlays);
        assert_eq!(merged.len(), 2, "different providers coexist");
    }

    #[test]
    fn merge_preserves_surviving_base_order_then_overlays() {
        let base = vec![
            sku("b1", "openai", "gpt-4o*", PriceSource::List),
            sku("b2", "openai", "gpt-4o-mini*", PriceSource::List),
            sku("b3", "anthropic", "claude-*", PriceSource::List),
        ];
        let overlays = vec![
            sku("o1", "openai", "gpt-4o*", PriceSource::Org), // shadows b1
            sku("o2", "cohere", "command-*", PriceSource::Org),
        ];
        let merged = merge_pricing(&base, &overlays);
        let ids: Vec<&str> = merged.iter().map(|s| s.sku_id.as_str()).collect();
        // Surviving base rows in original order, then overlays in order.
        assert_eq!(ids, vec!["b2", "b3", "o1", "o2"]);
    }

    #[test]
    fn merge_invariants_hold_for_arbitrary_overrides() {
        let base = vec![
            sku("b1", "openai", "gpt-4o*", PriceSource::List),
            sku("b2", "openai", "gpt-4o-mini*", PriceSource::List),
            sku("b3", "anthropic", "claude-*", PriceSource::List),
        ];
        let overlays = vec![
            sku("o1", "openai", "gpt-4o*", PriceSource::Org),
            sku("o2", "anthropic", "claude-*", PriceSource::Org),
        ];
        let merged = merge_pricing(&base, &overlays);

        // Every overlay is present.
        for o in &overlays {
            assert!(merged.iter().any(|s| s.sku_id == o.sku_id));
        }
        // No base row whose key was overridden survives.
        let overridden: std::collections::HashSet<(&str, &str)> = overlays
            .iter()
            .map(|s| (s.provider.as_str(), s.model_match.as_str()))
            .collect();
        for m in &merged {
            if m.source == PriceSource::List {
                assert!(!overridden.contains(&(m.provider.as_str(), m.model_match.as_str())));
            }
        }
        // Length == surviving base + all overlays (no dupes, no drops).
        let surviving_base = base
            .iter()
            .filter(|s| !overridden.contains(&(s.provider.as_str(), s.model_match.as_str())))
            .count();
        assert_eq!(merged.len(), surviving_base + overlays.len());
    }
}
