//! Signed pricing-bundle verification and per-call cost computation (M-3 / M-4).
//!
//! This module is to cost metering what [`crate::interface::reload_policy_signed`]
//! plus the policy engine are to enforcement. It has two halves:
//!
//! 1. **`reload_pricing_signed_internal` (M-3)** — a faithful structural clone of
//!    `reload_policy_signed_internal`. The control plane DSSE-signs a
//!    [`PricingBundle`]; this verifies the envelope against the runtime trust
//!    list under the *pricing* payload type, then runs the same
//!    schema → monotonic → staleness → future-skew gauntlet the policy bundle
//!    runs before installing it as the active price table. A tampered price
//!    table is an integrity attack on *money*, so it inherits the codebase's
//!    strongest defenses unchanged.
//!
//! 2. **`settle_usage_internal` (M-4)** — resolves the most-specific [`PriceSku`]
//!    for a completed call's `(provider, model)` and computes the call's cost in
//!    integer micro-USD via [`Cost`]. Metering is *fail-open*: a missing price
//!    table marks the event `disabled`, an unknown model marks it
//!    `unpriced_model`; neither ever blocks the call.
//!
//! The `EngineState` and `ENGINE` thread-local are private to
//! [`crate::interface`], so the two install/read seams this module needs
//! (`install_verified_pricing_bundle`, `with_active_pricing_bundle`) live there
//! and keep the monotonic check atomic with the store. Everything else — verify
//! orchestration, error-code mapping, SKU resolution, and the cost arithmetic —
//! lives here.

use checkrd_shared::dsse::{DsseEnvelope, PRICING_BUNDLE_PAYLOAD_TYPE};
use checkrd_shared::pricing_bundle::{PriceSku, PricingBundle, PRICING_BUNDLE_SCHEMA_VERSION};
use checkrd_shared::Cost;
use serde::{Deserialize, Serialize};

use crate::interface::{
    install_verified_pricing_bundle, with_active_pricing_bundle, FFI_INVALID_KEY, FFI_PARSE_ERROR,
    FFI_PRICING_BUNDLE_IN_FUTURE, FFI_PRICING_BUNDLE_TOO_OLD,
    FFI_PRICING_KEY_NOT_IN_VALIDITY_WINDOW, FFI_PRICING_PAYLOAD_TYPE_MISMATCH,
    FFI_PRICING_SCHEMA_VERSION_MISMATCH, FFI_PRICING_SIGNATURE_INVALID,
    FFI_PRICING_UNKNOWN_OR_NO_SIGNER, FFI_PRICING_VERIFIED_PAYLOAD_INVALID,
};

/// Maximum forward clock skew accepted on a pricing bundle's `signed_at`
/// (defends against future-dated price tables being installed). Symmetric with
/// the policy path's `POLICY_BUNDLE_FUTURE_SKEW_SECS` and the +5 minute window
/// the telemetry signing path uses.
const PRICING_BUNDLE_FUTURE_SKEW_SECS: u64 = 300;

/// Inner implementation of [`crate::interface::reload_pricing_signed`] that
/// operates on Rust references and returns a structured FFI error code.
///
/// A faithful mirror of `reload_policy_signed_internal`: the verify → schema →
/// monotonic → staleness → future-skew sequence is identical, only the payload
/// type, bundle struct, and error-code family differ. Split out from the FFI
/// shim so unit tests can call it directly without pointer marshaling.
pub(crate) fn reload_pricing_signed_internal(
    envelope_json: &str,
    trusted_keys_json: &str,
    now_unix_secs: u64,
    max_age_secs: u64,
) -> i32 {
    let envelope: DsseEnvelope = match serde_json::from_str(envelope_json) {
        Ok(e) => e,
        Err(_) => return FFI_PARSE_ERROR,
    };
    let trusted_keys: Vec<crate::dsse_verify::TrustedKey> =
        match serde_json::from_str(trusted_keys_json) {
            Ok(k) => k,
            // Same -3 slot the policy path uses for a malformed trusted-keys
            // array — both are "the key material was malformed".
            Err(_) => return FFI_INVALID_KEY,
        };

    // Verify under the PRICING payload type. `verify_dsse_envelope` is payload-
    // type-parameterized, so the cross-type replay defense (a policy or
    // telemetry signature can never be presented as a price table) comes for
    // free from passing the distinct type here. Each VerifyError maps to the
    // matching -15..-19 pricing code, the same mapping the policy path uses for
    // -4..-8.
    let payload_bytes = match crate::dsse_verify::verify_dsse_envelope(
        &envelope,
        PRICING_BUNDLE_PAYLOAD_TYPE,
        &trusted_keys,
        now_unix_secs,
    ) {
        Ok(b) => b,
        Err(crate::dsse_verify::VerifyError::PayloadTypeMismatch { .. }) => {
            return FFI_PRICING_PAYLOAD_TYPE_MISMATCH
        }
        Err(crate::dsse_verify::VerifyError::SignatureInvalid)
        | Err(crate::dsse_verify::VerifyError::MalformedEncoding(_)) => {
            return FFI_PRICING_SIGNATURE_INVALID
        }
        Err(crate::dsse_verify::VerifyError::UnknownKeyid)
        | Err(crate::dsse_verify::VerifyError::NoSignatures) => {
            return FFI_PRICING_UNKNOWN_OR_NO_SIGNER
        }
        Err(crate::dsse_verify::VerifyError::KeyExpired { .. })
        | Err(crate::dsse_verify::VerifyError::KeyNotYetValid { .. }) => {
            return FFI_PRICING_KEY_NOT_IN_VALIDITY_WINDOW
        }
    };

    // Parse the verified payload as a PricingBundle. The bundle wrapper carries
    // monotonic version + signed_at metadata inside the signed bytes, so it
    // can't be tampered with after signing.
    let payload_str = match std::str::from_utf8(&payload_bytes) {
        Ok(s) => s,
        Err(_) => return FFI_PRICING_VERIFIED_PAYLOAD_INVALID,
    };
    let bundle: PricingBundle = match serde_json::from_str(payload_str) {
        Ok(b) => b,
        Err(_) => return FFI_PRICING_VERIFIED_PAYLOAD_INVALID,
    };

    // Schema version: reject bundles produced by a control plane on a future
    // format we don't understand.
    if bundle.schema_version != PRICING_BUNDLE_SCHEMA_VERSION {
        return FFI_PRICING_SCHEMA_VERSION_MISMATCH;
    }

    // Freshness: reject bundles significantly future-dated (clock-skew defense)
    // or older than max_age_secs (replay defense). Same order and strict-greater
    // comparisons as the policy path.
    if bundle.signed_at > now_unix_secs.saturating_add(PRICING_BUNDLE_FUTURE_SKEW_SECS) {
        return FFI_PRICING_BUNDLE_IN_FUTURE;
    }
    if now_unix_secs.saturating_sub(bundle.signed_at) > max_age_secs {
        return FFI_PRICING_BUNDLE_TOO_OLD;
    }

    // Hand the verified bundle to the install seam, which applies the monotonic
    // rollback gate atomically with the store (mirrors the policy path's
    // in-borrow_mut version check) and returns FFI_OK / -21 / -9.
    install_verified_pricing_bundle(bundle)
}

// --- settle_usage (M-4) ---

/// Normalized token usage the wrapper extracts from a completed provider
/// response and passes to [`crate::interface::settle_usage`].
///
/// All counts post-normalization: `cache_read_tokens` / `cache_creation_tokens`
/// are a *subset* of `input_tokens`, and `reasoning_tokens` is already part of
/// `output_tokens` (it is informational here, billed at the output rate, and is
/// deliberately NOT added separately). `#[serde(default)]` on every field means
/// a wrapper that omits the optionals — or a provider that doesn't report
/// cache/reasoning — still deserializes.
#[derive(Debug, Clone, Default, Deserialize)]
pub(crate) struct UsageInput {
    /// OTel `gen_ai` well-known provider name (`openai`, `anthropic`, …).
    #[serde(default)]
    pub provider: String,
    /// Model id as the provider reported it (`gpt-4o`, `claude-sonnet-4-5`, …).
    #[serde(default)]
    pub model: String,
    /// Total input (prompt) tokens, including any cached tokens.
    #[serde(default)]
    pub input_tokens: i64,
    /// Total output (completion) tokens, already including reasoning tokens.
    #[serde(default)]
    pub output_tokens: i64,
    /// Cache-read (cache-hit) input tokens, a subset of `input_tokens`.
    #[serde(default)]
    pub cache_read_tokens: Option<i64>,
    /// Cache-creation (cache-write) input tokens, a subset of `input_tokens`.
    #[serde(default)]
    pub cache_creation_tokens: Option<i64>,
    /// Reasoning tokens — informational only; already counted in
    /// `output_tokens` and billed at the output rate, never added separately.
    /// Part of the wire contract (the wrapper sends it) but deliberately not
    /// read by the cost path, hence the allow.
    #[serde(default)]
    #[allow(dead_code)]
    pub reasoning_tokens: Option<i64>,
}

/// Outcome of pricing one call (TDD §4.4). Serialized as the `SettleResult` JSON
/// the wrapper attaches to the telemetry event before it is signed.
/// `Deserialize` is derived only so tests can round-trip the wire form back.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub(crate) struct SettleResult {
    /// Computed cost in integer micro-USD (see [`checkrd_shared::cost`]).
    pub cost_usd_micros: i64,
    /// ISO 4217 currency code. Always `"USD"` in v1.
    pub currency: String,
    /// Version of the pricing bundle the cost was computed against, or `0` when
    /// metering is disabled (no bundle installed).
    pub pricing_bundle_version: u64,
    /// How the figure was produced — see [`PricingStatus`].
    pub pricing_status: PricingStatus,
    /// `true` iff the cost arithmetic saturated (`Cost::overflow`); the figure
    /// is then approximate. Unreachable in practice (~$9.2T single call).
    pub overflow: bool,
    /// The matched SKU's `sku_id`, when a SKU priced the call.
    pub sku_id: Option<String>,
}

/// Why a [`SettleResult`] has the cost it does. Serialized snake_case so the
/// wire values are `"priced"` / `"unpriced_model"` / `"untallied"` / `"disabled"`.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub(crate) enum PricingStatus {
    /// A SKU matched and priced the call; `cost_usd_micros` is authoritative.
    Priced,
    /// A price table is installed but no SKU matched `(provider, model)`. The
    /// event is marked for backfill; the call was never blocked (fail-open).
    UnpricedModel,
    /// Usage could not be tallied (e.g. an abandoned stream). Set by the
    /// *wrapper*, never by `settle_usage` — present for wire completeness so the
    /// wrapper and dashboard share one status vocabulary, hence the allow.
    #[allow(dead_code)]
    Untallied,
    /// No pricing bundle is installed; cost metering is not configured.
    Disabled,
}

/// Inner implementation of [`crate::interface::settle_usage`] operating on Rust
/// references and returning the serialized `SettleResult` JSON.
///
/// `request_id` is accepted for log/trace correlation only and does not affect
/// the result. Split out from the FFI shim so unit tests can assert on the
/// `SettleResult` directly. Never panics: malformed usage JSON deserializes to
/// an all-zero [`UsageInput`] (a zero-token call costs nothing), consistent with
/// the fail-open metering contract.
pub(crate) fn settle_usage_internal(_request_id: &str, usage_json: &str) -> String {
    let usage: UsageInput = serde_json::from_str(usage_json).unwrap_or_default();
    let result = with_active_pricing_bundle(|bundle| settle(bundle, &usage));
    // Serialization of SettleResult (all owned, no NaN/Inf — integers + a
    // fixed enum) cannot realistically fail; fall back to a Disabled figure
    // rather than panic if it ever does.
    serde_json::to_string(&result).unwrap_or_else(|_| {
        r#"{"cost_usd_micros":0,"currency":"USD","pricing_bundle_version":0,"pricing_status":"disabled","overflow":false,"sku_id":null}"#.to_string()
    })
}

/// Pure core of settle: resolve the SKU and compute the cost. Separated from the
/// JSON/engine plumbing so it is trivially unit-testable.
fn settle(bundle: Option<&PricingBundle>, usage: &UsageInput) -> SettleResult {
    // 1. No price table installed → metering not configured.
    let Some(bundle) = bundle else {
        return SettleResult {
            cost_usd_micros: 0,
            currency: "USD".to_string(),
            pricing_bundle_version: 0,
            pricing_status: PricingStatus::Disabled,
            overflow: false,
            sku_id: None,
        };
    };

    // 2. Resolve the most-specific matching SKU.
    let Some(sku) = resolve_sku(bundle, &usage.provider, &usage.model) else {
        // 3. No SKU match → fail-open mark, cost 0. The version is still the
        //    installed bundle's so the dashboard can see which table was in
        //    force when the model came back unpriced.
        return SettleResult {
            cost_usd_micros: 0,
            currency: "USD".to_string(),
            pricing_bundle_version: bundle.version,
            pricing_status: PricingStatus::UnpricedModel,
            overflow: false,
            sku_id: None,
        };
    };

    // 4. Compute the cost. Cache-read / cache-creation tokens are a subset of
    //    input_total (post-normalization), so bill fresh (non-cached) input at
    //    the input rate, cache-read at the cache-read rate (falling back to the
    //    input rate when the SKU doesn't price it separately), cache-creation at
    //    the cache-write rate (same fallback), and output (which already
    //    includes reasoning) at the output rate. This reproduces provider
    //    invoices line-for-line.
    let cache_read = usage.cache_read_tokens.unwrap_or(0).max(0);
    let cache_creation = usage.cache_creation_tokens.unwrap_or(0).max(0);
    let input_total = usage.input_tokens.max(0);
    let output_total = usage.output_tokens.max(0);
    // Subtract the cache portions saturatingly, then floor at 0. All three
    // terms are already clamped `>= 0`; `saturating_sub` means a wrapper (or
    // attacker) reporting near-`i64::MAX` cache counts cannot overflow `i64`
    // and panic the agent's process (ADR-003 total-arithmetic rule), and the
    // final `.max(0)` keeps `fresh_input` non-negative when cache tokens
    // exceed `input_total` (so the cost never goes negative). Both the
    // overflow and the negative-cost case were found by the
    // `settle_never_panics` proptest.
    let fresh_input = input_total
        .saturating_sub(cache_read)
        .saturating_sub(cache_creation)
        .max(0);

    let cost = Cost::line_item(fresh_input, sku.input_usd_micros_per_unit)
        .saturating_add(Cost::line_item(
            cache_read,
            sku.cache_read_usd_micros_per_unit
                .unwrap_or(sku.input_usd_micros_per_unit),
        ))
        .saturating_add(Cost::line_item(
            cache_creation,
            sku.cache_write_usd_micros_per_unit
                .unwrap_or(sku.input_usd_micros_per_unit),
        ))
        .saturating_add(Cost::line_item(
            output_total,
            sku.output_usd_micros_per_unit,
        ));

    SettleResult {
        cost_usd_micros: cost.micros,
        currency: "USD".to_string(),
        pricing_bundle_version: bundle.version,
        pricing_status: PricingStatus::Priced,
        overflow: cost.overflow,
        sku_id: Some(sku.sku_id.clone()),
    }
}

/// Resolve the SKU that prices `(provider, model)`: among SKUs whose `provider`
/// matches exactly and whose `model_match` glob matches `model`, return the most
/// specific (most literal characters), breaking ties by the newest
/// `effective_from`. Returns `None` when nothing matches.
fn resolve_sku<'a>(bundle: &'a PricingBundle, provider: &str, model: &str) -> Option<&'a PriceSku> {
    bundle
        .skus
        .iter()
        .filter(|sku| sku.provider == provider && model_matches(&sku.model_match, model))
        // Specificity first (more literal chars = more specific), then newest
        // effective_from as the tie-break. `max_by_key` returns the LAST maximal
        // element, so on a full tie (equal specificity AND equal effective_from)
        // the later SKU in the bundle wins — deterministic for a given bundle.
        .max_by_key(|sku| (model_specificity(&sku.model_match), sku.effective_from))
}

// --- Model-id glob -----------------------------------------------------------
//
// JUDGMENT CALL (documented per the task): the audited URL matcher in
// `policy.rs` / `crate::url` is *segment*-based — it splits on `/` and a `*`
// is only a wildcard when it is the WHOLE segment. Model ids
// (`claude-sonnet-4-5`, `gpt-4o-mini`) are single hyphen/dot-delimited tokens
// with no `/`, so that matcher does NOT cleanly apply: `parse_pattern(
// "claude-sonnet-*")` would yield one literal segment `claude-sonnet-*` and
// never match `claude-sonnet-4-5`. The `PriceSku` doc nonetheless specifies
// `*`/`**` globbing on `model_match` with "specificity = literal char count"
// and ships fixtures like `claude-sonnet-*` and `**`.
//
// So this is a small, self-contained glob consistent with that grammar's
// spirit, applied to the whole model string rather than per `/`-segment:
//   * `*`  matches any run of characters, including empty.
//   * `**` is treated identically to `*` for a non-segmented identifier (a
//          single token has no segments for `**` to span), so consecutive
//          wildcards collapse.
//   * specificity = the number of literal (non-`*`) characters in the pattern;
//          a longer literal prefix/suffix is a more specific SKU.
// `**` (or `*`) alone is the catch-all default SKU (specificity 0).

/// Does `pattern` glob-match `model`? `*` (and `**`) match any run of
/// characters including empty; all other characters match literally. Linear-time
/// greedy backtracking over a single token (no `/` segmentation).
fn model_matches(pattern: &str, model: &str) -> bool {
    // Collapse `**`→`*` so the two are equivalent for a single-token id, then
    // run classic two-pointer wildcard matching with backtracking on the last
    // `*`. Bytes (not chars) are fine: model ids are ASCII, and a multi-byte
    // char still matches literally byte-for-byte.
    let pat = pattern.as_bytes();
    let txt = model.as_bytes();
    let (mut p, mut t) = (0usize, 0usize);
    let (mut star, mut mark) = (None, 0usize);
    while t < txt.len() {
        if p < pat.len() && pat[p] == b'*' {
            // Skip a run of consecutive `*` (handles `**`, `***`, …).
            while p < pat.len() && pat[p] == b'*' {
                p += 1;
            }
            star = Some(p);
            mark = t;
            if p == pat.len() {
                return true; // trailing wildcard matches the rest
            }
        } else if p < pat.len() && pat[p] == txt[t] {
            p += 1;
            t += 1;
        } else if let Some(sp) = star {
            // Mismatch under a wildcard: let the last `*` consume one more char.
            p = sp;
            mark += 1;
            t = mark;
        } else {
            return false;
        }
    }
    // Consume any trailing `*` left in the pattern.
    while p < pat.len() && pat[p] == b'*' {
        p += 1;
    }
    p == pat.len()
}

/// Specificity of a `model_match` pattern: the count of literal (non-`*`)
/// characters. More literal characters ⇒ more specific ⇒ wins resolution.
fn model_specificity(pattern: &str) -> usize {
    pattern.bytes().filter(|&b| b != b'*').count()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::interface::{
        get_active_pricing_version, set_initial_pricing_version, FFI_OK,
        FFI_PRICING_VERSION_NOT_MONOTONIC,
    };
    use checkrd_shared::pricing_bundle::{PriceSource, PriceUnit, Rounding};
    use ed25519_dalek::{Signer, SigningKey};
    use proptest::prelude::*;

    // The reload tests drive the real `ENGINE` thread-local through
    // `reload_pricing_signed_internal`, exactly like the policy reload tests
    // drive it through `reload_policy_signed_internal`. `EngineState` / `ENGINE`
    // are private to `interface`, so we bootstrap a live engine by calling the
    // public `extern "C"` `init` export (with raw byte inputs, as a host would)
    // and use the crate-visible `reset_engine_for_test` to clear it.

    const TEST_NOW_SECS: u64 = 1_000_000;
    const TEST_MAX_AGE_SECS: u64 = 86_400; // 24 hours

    fn init_engine() {
        // Start from a clean slate: `init` deliberately PRESERVES pricing
        // version / install-flag / bundle across re-init (the rollback
        // defense), so tests sharing this thread's `ENGINE` thread-local must
        // reset first or they'd inherit a prior test's high water mark.
        crate::interface::reset_engine_for_test();
        // Minimal allow-all policy; identity anonymous (no key). We call the
        // real `init` export with raw pointers, mirroring how a host would.
        let policy = br#"{"agent":"t","mode":"enforce","default":"allow","rules":[]}"#;
        let agent = b"pricing-test-agent";
        let rc = crate::interface::init(
            policy.as_ptr(),
            policy.len() as u32,
            agent.as_ptr(),
            agent.len() as u32,
            std::ptr::null(),
            0,
            std::ptr::null(),
            0,
        );
        assert_eq!(rc, FFI_OK, "engine init failed");
    }

    // ----- Pricing bundle / envelope builders ---------------------------

    fn sample_skus() -> Vec<PriceSku> {
        vec![
            // Anthropic Sonnet family, cache priced separately. Input $3/1M,
            // output $15/1M, cache-read $0.30/1M, cache-write $3.75/1M.
            PriceSku {
                sku_id: "anthropic-claude-sonnet".to_string(),
                provider: "anthropic".to_string(),
                model_match: "claude-sonnet-*".to_string(),
                unit: PriceUnit::Per1mTokens,
                input_usd_micros_per_unit: 3_000_000,
                output_usd_micros_per_unit: 15_000_000,
                cache_read_usd_micros_per_unit: Some(300_000),
                cache_write_usd_micros_per_unit: Some(3_750_000),
                default_max_output_tokens: 8192,
                effective_from: 1_700_000_000,
                deprecated_after: None,
                source: PriceSource::List,
            },
            // Catch-all default for anthropic, no separate cache pricing.
            PriceSku {
                sku_id: "anthropic-default".to_string(),
                provider: "anthropic".to_string(),
                model_match: "**".to_string(),
                unit: PriceUnit::Per1mTokens,
                input_usd_micros_per_unit: 1_000_000,
                output_usd_micros_per_unit: 5_000_000,
                cache_read_usd_micros_per_unit: None,
                cache_write_usd_micros_per_unit: None,
                default_max_output_tokens: 4096,
                effective_from: 1_700_000_000,
                deprecated_after: None,
                source: PriceSource::List,
            },
        ]
    }

    fn sample_bundle(version: u64, signed_at: u64) -> PricingBundle {
        PricingBundle::new(version, signed_at, Rounding::HalfUp, sample_skus())
    }

    fn signing_key() -> SigningKey {
        SigningKey::from_bytes(&[0xb7; 32])
    }

    /// Build a signed DSSE envelope wrapping a `PricingBundle`. Mirrors the
    /// policy test's `make_signed_bundle_envelope`.
    fn make_signed_pricing_envelope(key: &SigningKey, bundle: &PricingBundle) -> String {
        use base64::engine::general_purpose::STANDARD as B64;
        use base64::Engine;
        let bundle_bytes = serde_json::to_vec(bundle).unwrap();
        let pae = checkrd_shared::dsse::pae(PRICING_BUNDLE_PAYLOAD_TYPE, &bundle_bytes);
        let sig = key.sign(&pae);
        let envelope = DsseEnvelope {
            payload_type: PRICING_BUNDLE_PAYLOAD_TYPE.to_string(),
            payload: B64.encode(&bundle_bytes),
            signatures: vec![checkrd_shared::dsse::DsseSignature {
                keyid: "test-cp".to_string(),
                sig: B64.encode(sig.to_bytes()),
            }],
        };
        serde_json::to_string(&envelope).unwrap()
    }

    fn trusted_keys_json(key: &SigningKey) -> String {
        let pk = key.verifying_key().to_bytes();
        let hex: String = pk.iter().map(|b| format!("{b:02x}")).collect();
        serde_json::json!([{
            "keyid": "test-cp",
            "public_key_hex": hex,
            "valid_from": 0,
            "valid_until": u64::MAX,
        }])
        .to_string()
    }

    // =====================================================================
    // reload_pricing_signed_internal — happy path + every error code.
    // These mirror the policy reload adversarial tests one-for-one.
    // =====================================================================

    #[test]
    fn reload_installs_verified_bundle_and_bumps_version() {
        init_engine();
        let key = signing_key();
        let envelope = make_signed_pricing_envelope(&key, &sample_bundle(3, TEST_NOW_SECS));
        let trusted = trusted_keys_json(&key);
        let rc =
            reload_pricing_signed_internal(&envelope, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS);
        assert_eq!(rc, FFI_OK, "expected FFI_OK, got {rc}");
        assert_eq!(get_active_pricing_version(), 3);

        // The active bundle is now readable and prices a known model.
        let usage = serde_json::json!({
            "provider": "anthropic", "model": "claude-sonnet-4-5",
            "input_tokens": 0, "output_tokens": 0
        })
        .to_string();
        let out: SettleResult = serde_json::from_str(&settle_usage_internal("r", &usage)).unwrap();
        assert_eq!(out.pricing_status, PricingStatus::Priced);
        assert_eq!(out.pricing_bundle_version, 3);
    }

    #[test]
    fn reload_rejects_wrong_payload_type() {
        // Correctly signed but under the TELEMETRY type — cross-type replay
        // defense must reject with -15.
        init_engine();
        use base64::engine::general_purpose::STANDARD as B64;
        use base64::Engine;
        let key = signing_key();
        let bundle = sample_bundle(1, TEST_NOW_SECS);
        let bundle_bytes = serde_json::to_vec(&bundle).unwrap();
        let pae = checkrd_shared::dsse::pae(
            checkrd_shared::dsse::TELEMETRY_BATCH_PAYLOAD_TYPE,
            &bundle_bytes,
        );
        let sig = key.sign(&pae);
        let envelope = DsseEnvelope {
            payload_type: checkrd_shared::dsse::TELEMETRY_BATCH_PAYLOAD_TYPE.to_string(),
            payload: B64.encode(&bundle_bytes),
            signatures: vec![checkrd_shared::dsse::DsseSignature {
                keyid: "test-cp".to_string(),
                sig: B64.encode(sig.to_bytes()),
            }],
        };
        let envelope_json = serde_json::to_string(&envelope).unwrap();
        let rc = reload_pricing_signed_internal(
            &envelope_json,
            &trusted_keys_json(&key),
            TEST_NOW_SECS,
            TEST_MAX_AGE_SECS,
        );
        assert_eq!(rc, FFI_PRICING_PAYLOAD_TYPE_MISMATCH);
    }

    /// HEADLINE CROSS-TYPE REPLAY DEFENSE. A *validly-signed* policy bundle
    /// envelope (signed with the SAME trusted control-plane key the pricing
    /// path trusts) MUST be rejected with -15 when fed to the pricing reload.
    /// The DSSE PAE binds the payload type into the signed bytes, so a real,
    /// in-window, trusted-key signature over a `policy-bundle+json` payload can
    /// never be presented as a price table. The previous test proves the same
    /// for a telemetry-batch envelope; this proves it for the *policy* type,
    /// which is the more dangerous direction (policy is the other DSSE-signed
    /// control-plane artifact an attacker on the SSE channel could capture).
    ///
    /// Both halves sign with the real key and present a real, otherwise-valid
    /// envelope — the ONLY thing wrong is the payload type. If the type binding
    /// were ever dropped, these would slip through and a captured policy/
    /// telemetry signature would tamper with money. This is the test that fails
    /// the instant domain separation regresses.
    #[test]
    fn reload_rejects_valid_policy_envelope_as_cross_type_replay() {
        init_engine();
        use base64::engine::general_purpose::STANDARD as B64;
        use base64::Engine;
        let key = signing_key();

        // A *real, valid* policy bundle, signed under the POLICY payload type by
        // the trusted key — exactly what the policy reload path would accept.
        let policy_bundle = serde_json::json!({
            "schema_version": checkrd_shared::policy_bundle::POLICY_BUNDLE_SCHEMA_VERSION,
            "version": 1,
            "signed_at": TEST_NOW_SECS,
            "policy": {"agent":"t","mode":"enforce","default":"allow","rules":[]},
        });
        let policy_bytes = serde_json::to_vec(&policy_bundle).unwrap();
        let pae = checkrd_shared::dsse::pae(
            checkrd_shared::dsse::POLICY_BUNDLE_PAYLOAD_TYPE,
            &policy_bytes,
        );
        let sig = key.sign(&pae);
        let policy_envelope = DsseEnvelope {
            payload_type: checkrd_shared::dsse::POLICY_BUNDLE_PAYLOAD_TYPE.to_string(),
            payload: B64.encode(&policy_bytes),
            signatures: vec![checkrd_shared::dsse::DsseSignature {
                keyid: "test-cp".to_string(),
                sig: B64.encode(sig.to_bytes()),
            }],
        };
        let policy_json = serde_json::to_string(&policy_envelope).unwrap();
        let rc = reload_pricing_signed_internal(
            &policy_json,
            &trusted_keys_json(&key),
            TEST_NOW_SECS,
            TEST_MAX_AGE_SECS,
        );
        assert_eq!(
            rc, FFI_PRICING_PAYLOAD_TYPE_MISMATCH,
            "a validly-signed POLICY envelope must be rejected -15 by the pricing reload"
        );
        // No price table was installed by the rejected replay.
        assert_eq!(get_active_pricing_version(), 0);

        // Sanity floor: the SAME bytes, re-signed under the PRICING type by the
        // SAME key, would NOT collide structurally — prove the rejection above
        // is the payload-type gate and not an accidental parse failure, by
        // confirming a real pricing bundle from this key still installs.
        let ok = make_signed_pricing_envelope(&key, &sample_bundle(1, TEST_NOW_SECS));
        assert_eq!(
            reload_pricing_signed_internal(
                &ok,
                &trusted_keys_json(&key),
                TEST_NOW_SECS,
                TEST_MAX_AGE_SECS
            ),
            FFI_OK
        );
    }

    /// A real telemetry-batch envelope (the SDK's own outbound signed artifact)
    /// produced with `sign_telemetry_batch_internal` under a trusted key and
    /// fed to the pricing reload is rejected -15. This exercises the *exact*
    /// wire shape an attacker would capture off the ingestion path, not a
    /// hand-rolled approximation, closing the cross-type replay defense against
    /// the genuine telemetry envelope format.
    #[test]
    fn reload_rejects_real_telemetry_envelope_as_cross_type_replay() {
        // Build a live signing engine and emit a genuine telemetry DSSE
        // envelope through the production signing path.
        crate::interface::reset_engine_for_test();
        let signing = signing_key();
        let key_bytes = signing.to_bytes();
        let policy = br#"{"agent":"t","mode":"enforce","default":"allow","rules":[]}"#;
        let agent = b"telemetry-replay-agent";
        let rc = crate::interface::init(
            policy.as_ptr(),
            policy.len() as u32,
            agent.as_ptr(),
            agent.len() as u32,
            key_bytes.as_ptr(),
            key_bytes.len() as u32,
            std::ptr::null(),
            0,
        );
        assert_eq!(rc, FFI_OK);

        let signed = crate::interface::sign_telemetry_batch_internal(
            br#"{"events":[]}"#,
            "https://api.checkrd.io/v1/telemetry",
            "00000000-0000-0000-0000-000000000001",
            "deadbeef",
            TEST_NOW_SECS,
            TEST_NOW_SECS + 60,
        )
        .expect("telemetry signing should succeed for a keyed identity");
        let signed: serde_json::Value = serde_json::from_str(&signed).unwrap();
        let telemetry_envelope = serde_json::to_string(&signed["dsse_envelope"]).unwrap();

        // The telemetry envelope's keyid is the agent instance id; trust THAT
        // key so the rejection can only be the payload-type gate, never an
        // unknown-signer short-circuit. (If trust were the gate we'd see -17.)
        let instance_keyid = signed["dsse_envelope"]["signatures"][0]["keyid"]
            .as_str()
            .unwrap();
        let pk: String = signing
            .verifying_key()
            .to_bytes()
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        let trusted = serde_json::json!([{
            "keyid": instance_keyid, "public_key_hex": pk,
            "valid_from": 0, "valid_until": u64::MAX,
        }])
        .to_string();

        let rc = reload_pricing_signed_internal(
            &telemetry_envelope,
            &trusted,
            TEST_NOW_SECS,
            TEST_MAX_AGE_SECS,
        );
        assert_eq!(
            rc, FFI_PRICING_PAYLOAD_TYPE_MISMATCH,
            "a real telemetry-batch envelope must be rejected -15, not accepted as a price table"
        );
    }

    #[test]
    fn reload_rejects_tampered_envelope() {
        init_engine();
        use base64::engine::general_purpose::STANDARD as B64;
        use base64::Engine;
        let key = signing_key();
        let mut envelope: DsseEnvelope = serde_json::from_str(&make_signed_pricing_envelope(
            &key,
            &sample_bundle(1, TEST_NOW_SECS),
        ))
        .unwrap();
        // Replace the payload AFTER signing.
        envelope.payload =
            B64.encode(serde_json::to_vec(&sample_bundle(2, TEST_NOW_SECS)).unwrap());
        let tampered = serde_json::to_string(&envelope).unwrap();
        let rc = reload_pricing_signed_internal(
            &tampered,
            &trusted_keys_json(&key),
            TEST_NOW_SECS,
            TEST_MAX_AGE_SECS,
        );
        assert_eq!(rc, FFI_PRICING_SIGNATURE_INVALID);
    }

    /// Single-bit tamper of the *signature* (not the payload) → -16. Mirrors
    /// `dsse_verify::verify_rejects_tampered_signature_with_signature_invalid`
    /// but drives the full pricing reload: flip one byte inside the signature,
    /// keep it valid base64 and exactly 64 bytes (so it passes encoding and
    /// length checks and reaches the Ed25519 verify), and confirm it is
    /// rejected. Proves the figure is bound to an unforgeable signature, not
    /// merely to a well-formed envelope shape.
    #[test]
    fn reload_rejects_single_byte_flipped_signature() {
        init_engine();
        use base64::engine::general_purpose::STANDARD as B64;
        use base64::Engine;
        let key = signing_key();
        let mut envelope: DsseEnvelope = serde_json::from_str(&make_signed_pricing_envelope(
            &key,
            &sample_bundle(1, TEST_NOW_SECS),
        ))
        .unwrap();
        // Flip the first byte of the 64-byte signature; re-encode (still valid
        // base64, still 64 bytes → reaches the cryptographic check).
        let mut sig_bytes = B64.decode(&envelope.signatures[0].sig).unwrap();
        sig_bytes[0] ^= 0xff;
        envelope.signatures[0].sig = B64.encode(&sig_bytes);
        let tampered = serde_json::to_string(&envelope).unwrap();
        let rc = reload_pricing_signed_internal(
            &tampered,
            &trusted_keys_json(&key),
            TEST_NOW_SECS,
            TEST_MAX_AGE_SECS,
        );
        assert_eq!(rc, FFI_PRICING_SIGNATURE_INVALID);
        assert_eq!(
            get_active_pricing_version(),
            0,
            "tampered bundle must not install"
        );
    }

    /// An envelope with an empty `signatures` array maps to -17 (the
    /// `NoSignatures` verify error shares the `UNKNOWN_OR_NO_SIGNER` code).
    /// Mirrors `dsse_verify::verify_rejects_envelope_with_no_signatures`; the
    /// pricing path had no direct test for the no-signatures branch.
    #[test]
    fn reload_rejects_envelope_with_no_signatures() {
        init_engine();
        use base64::engine::general_purpose::STANDARD as B64;
        use base64::Engine;
        let bundle_bytes = serde_json::to_vec(&sample_bundle(1, TEST_NOW_SECS)).unwrap();
        let envelope = DsseEnvelope {
            payload_type: PRICING_BUNDLE_PAYLOAD_TYPE.to_string(),
            payload: B64.encode(&bundle_bytes),
            signatures: vec![],
        };
        let envelope_json = serde_json::to_string(&envelope).unwrap();
        let rc = reload_pricing_signed_internal(
            &envelope_json,
            &trusted_keys_json(&signing_key()),
            TEST_NOW_SECS,
            TEST_MAX_AGE_SECS,
        );
        assert_eq!(rc, FFI_PRICING_UNKNOWN_OR_NO_SIGNER);
    }

    #[test]
    fn reload_rejects_unknown_signer() {
        init_engine();
        let signer = signing_key();
        let envelope = make_signed_pricing_envelope(&signer, &sample_bundle(1, TEST_NOW_SECS));
        // Trust list carries a different keyid.
        let other = SigningKey::from_bytes(&[0x99; 32]);
        let pk: String = other
            .verifying_key()
            .to_bytes()
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        let trusted = serde_json::json!([{
            "keyid": "other-cp", "public_key_hex": pk,
            "valid_from": 0, "valid_until": u64::MAX,
        }])
        .to_string();
        let rc =
            reload_pricing_signed_internal(&envelope, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS);
        assert_eq!(rc, FFI_PRICING_UNKNOWN_OR_NO_SIGNER);
    }

    #[test]
    fn reload_rejects_key_out_of_validity_window() {
        init_engine();
        let key = signing_key();
        let envelope = make_signed_pricing_envelope(&key, &sample_bundle(1, TEST_NOW_SECS));
        let pk: String = key
            .verifying_key()
            .to_bytes()
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        let trusted = serde_json::json!([{
            "keyid": "test-cp", "public_key_hex": pk,
            "valid_from": 0, "valid_until": 500,
        }])
        .to_string();
        // now well past valid_until; max_age disabled so the key window fires.
        let rc = reload_pricing_signed_internal(&envelope, &trusted, 2_000_000, u64::MAX);
        assert_eq!(rc, FFI_PRICING_KEY_NOT_IN_VALIDITY_WINDOW);
    }

    /// The other window edge: a trusted key whose `valid_from` is in the future
    /// (not yet valid) also maps to -18. The pricing code folds both
    /// `KeyExpired` and `KeyNotYetValid` into one code, so both directions must
    /// be exercised — the test above covers expired, this covers not-yet-valid.
    /// A skew of 0 on `signed_at` keeps the freshness checks from firing first.
    #[test]
    fn reload_rejects_key_not_yet_valid() {
        init_engine();
        let key = signing_key();
        // Sign the bundle "now" at a small timestamp, but make the key only
        // become valid far in the future and evaluate at that same small now.
        let now = 1_000u64;
        let envelope = make_signed_pricing_envelope(&key, &sample_bundle(1, now));
        let pk: String = key
            .verifying_key()
            .to_bytes()
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        let trusted = serde_json::json!([{
            "keyid": "test-cp", "public_key_hex": pk,
            "valid_from": 5_000_000, "valid_until": u64::MAX,
        }])
        .to_string();
        // now (1_000) < valid_from (5_000_000) → not-yet-valid. max_age disabled.
        let rc = reload_pricing_signed_internal(&envelope, &trusted, now, u64::MAX);
        assert_eq!(rc, FFI_PRICING_KEY_NOT_IN_VALIDITY_WINDOW);
    }

    #[test]
    fn reload_rejects_malformed_envelope_json() {
        init_engine();
        let rc = reload_pricing_signed_internal(
            "{not valid json",
            &trusted_keys_json(&signing_key()),
            TEST_NOW_SECS,
            TEST_MAX_AGE_SECS,
        );
        assert_eq!(rc, FFI_PARSE_ERROR);
    }

    #[test]
    fn reload_rejects_malformed_trusted_keys_json() {
        init_engine();
        let key = signing_key();
        let envelope = make_signed_pricing_envelope(&key, &sample_bundle(1, TEST_NOW_SECS));
        let rc = reload_pricing_signed_internal(
            &envelope,
            "{not an array",
            TEST_NOW_SECS,
            TEST_MAX_AGE_SECS,
        );
        assert_eq!(rc, FFI_INVALID_KEY);
    }

    #[test]
    fn reload_rejects_when_engine_not_initialized() {
        // Tear the engine down so the install seam sees no state.
        crate::interface::reset_engine_for_test();
        let key = signing_key();
        let envelope = make_signed_pricing_envelope(&key, &sample_bundle(1, TEST_NOW_SECS));
        let rc = reload_pricing_signed_internal(
            &envelope,
            &trusted_keys_json(&key),
            TEST_NOW_SECS,
            TEST_MAX_AGE_SECS,
        );
        assert_eq!(rc, crate::interface::FFI_POLICY_ENGINE_NOT_INITIALIZED);
    }

    #[test]
    fn reload_rejects_invalid_verified_payload() {
        // Verification succeeds but the verified bytes aren't a PricingBundle.
        init_engine();
        use base64::engine::general_purpose::STANDARD as B64;
        use base64::Engine;
        let key = signing_key();
        let garbage = b"{\"not\":\"a pricing bundle\"}";
        let pae = checkrd_shared::dsse::pae(PRICING_BUNDLE_PAYLOAD_TYPE, garbage);
        let sig = key.sign(&pae);
        let envelope = DsseEnvelope {
            payload_type: PRICING_BUNDLE_PAYLOAD_TYPE.to_string(),
            payload: B64.encode(garbage),
            signatures: vec![checkrd_shared::dsse::DsseSignature {
                keyid: "test-cp".to_string(),
                sig: B64.encode(sig.to_bytes()),
            }],
        };
        let envelope_json = serde_json::to_string(&envelope).unwrap();
        let rc = reload_pricing_signed_internal(
            &envelope_json,
            &trusted_keys_json(&key),
            TEST_NOW_SECS,
            TEST_MAX_AGE_SECS,
        );
        assert_eq!(rc, FFI_PRICING_VERIFIED_PAYLOAD_INVALID);
    }

    /// The OTHER -19 path: the signature verifies but the verified bytes are not
    /// valid UTF-8 at all (so `std::str::from_utf8` fails before JSON parsing is
    /// even attempted). A trusted signer could, in principle, sign raw non-UTF-8
    /// bytes; the reload must still fail closed with -19, never panic on the
    /// `from_utf8` boundary. The sibling test above covers valid-UTF-8-but-not-a-
    /// bundle; this covers the not-even-UTF-8 branch.
    #[test]
    fn reload_rejects_non_utf8_verified_payload() {
        init_engine();
        use base64::engine::general_purpose::STANDARD as B64;
        use base64::Engine;
        let key = signing_key();
        // Invalid UTF-8: a lone 0xFF continuation byte is never valid UTF-8.
        let non_utf8: &[u8] = &[0xff, 0xfe, 0xfd, 0x00, 0x80];
        let pae = checkrd_shared::dsse::pae(PRICING_BUNDLE_PAYLOAD_TYPE, non_utf8);
        let sig = key.sign(&pae);
        let envelope = DsseEnvelope {
            payload_type: PRICING_BUNDLE_PAYLOAD_TYPE.to_string(),
            payload: B64.encode(non_utf8),
            signatures: vec![checkrd_shared::dsse::DsseSignature {
                keyid: "test-cp".to_string(),
                sig: B64.encode(sig.to_bytes()),
            }],
        };
        let envelope_json = serde_json::to_string(&envelope).unwrap();
        let rc = reload_pricing_signed_internal(
            &envelope_json,
            &trusted_keys_json(&key),
            TEST_NOW_SECS,
            TEST_MAX_AGE_SECS,
        );
        assert_eq!(rc, FFI_PRICING_VERIFIED_PAYLOAD_INVALID);
        assert_eq!(get_active_pricing_version(), 0);
    }

    #[test]
    fn reload_rejects_unknown_schema_version() {
        init_engine();
        use base64::engine::general_purpose::STANDARD as B64;
        use base64::Engine;
        let key = signing_key();
        // schema_version=999, otherwise a valid-shaped bundle.
        let bundle_json = serde_json::json!({
            "schema_version": 999, "version": 1, "signed_at": TEST_NOW_SECS,
            "rounding": "half_up", "skus": [],
        });
        let bundle_bytes = serde_json::to_vec(&bundle_json).unwrap();
        let pae = checkrd_shared::dsse::pae(PRICING_BUNDLE_PAYLOAD_TYPE, &bundle_bytes);
        let sig = key.sign(&pae);
        let envelope = DsseEnvelope {
            payload_type: PRICING_BUNDLE_PAYLOAD_TYPE.to_string(),
            payload: B64.encode(&bundle_bytes),
            signatures: vec![checkrd_shared::dsse::DsseSignature {
                keyid: "test-cp".to_string(),
                sig: B64.encode(sig.to_bytes()),
            }],
        };
        let envelope_json = serde_json::to_string(&envelope).unwrap();
        let rc = reload_pricing_signed_internal(
            &envelope_json,
            &trusted_keys_json(&key),
            TEST_NOW_SECS,
            TEST_MAX_AGE_SECS,
        );
        assert_eq!(rc, FFI_PRICING_SCHEMA_VERSION_MISMATCH);
    }

    /// The expected forward path: each new bundle has a strictly higher version,
    /// so v1 → v2 → v3 → v5 → 100 all install and bump the high-water-mark.
    /// Parity with `reload_policy_signed_accepts_strictly_higher_version`; the
    /// pricing path only tested rollback rejection, not the monotone-increasing
    /// accept loop that proves each successful install advances the counter.
    #[test]
    fn reload_accepts_strictly_higher_versions_in_sequence() {
        init_engine();
        let key = signing_key();
        let trusted = trusted_keys_json(&key);
        for v in [1u64, 2, 3, 5, 100] {
            let bundle = make_signed_pricing_envelope(&key, &sample_bundle(v, TEST_NOW_SECS));
            assert_eq!(
                reload_pricing_signed_internal(&bundle, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS),
                FFI_OK,
                "version {v} install should succeed"
            );
            assert_eq!(get_active_pricing_version(), v);
        }
    }

    /// `get_active_pricing_version` returns 0 before any signed install (engine
    /// initialized but no bundle yet). Parity with
    /// `get_active_policy_version_returns_zero_before_any_install`.
    #[test]
    fn get_active_pricing_version_is_zero_before_any_install() {
        init_engine();
        assert_eq!(get_active_pricing_version(), 0);
    }

    /// `get_active_pricing_version` returns 0 (not a panic) when the engine is
    /// not initialized at all. Parity with
    /// `get_active_policy_version_returns_zero_when_engine_uninitialized`.
    #[test]
    fn get_active_pricing_version_is_zero_when_engine_uninitialized() {
        crate::interface::reset_engine_for_test();
        assert_eq!(get_active_pricing_version(), 0);
    }

    #[test]
    fn reload_rejects_rollback_and_replay() {
        init_engine();
        let key = signing_key();
        let trusted = trusted_keys_json(&key);
        // Install v5.
        let v5 = make_signed_pricing_envelope(&key, &sample_bundle(5, TEST_NOW_SECS));
        assert_eq!(
            reload_pricing_signed_internal(&v5, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS),
            FFI_OK
        );
        assert_eq!(get_active_pricing_version(), 5);
        // Rollback to v3 rejected.
        let v3 = make_signed_pricing_envelope(&key, &sample_bundle(3, TEST_NOW_SECS));
        assert_eq!(
            reload_pricing_signed_internal(&v3, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS),
            FFI_PRICING_VERSION_NOT_MONOTONIC
        );
        // Replay of v5 (equal version) rejected.
        assert_eq!(
            reload_pricing_signed_internal(&v5, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS),
            FFI_PRICING_VERSION_NOT_MONOTONIC
        );
        assert_eq!(get_active_pricing_version(), 5);
    }

    #[test]
    fn reload_accepts_bootstrap_at_version_zero_then_enforces_monotonic() {
        init_engine();
        let key = signing_key();
        let trusted = trusted_keys_json(&key);
        // Bootstrap install at v=0 must succeed.
        let v0 = make_signed_pricing_envelope(&key, &sample_bundle(0, TEST_NOW_SECS));
        assert_eq!(
            reload_pricing_signed_internal(&v0, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS),
            FFI_OK
        );
        assert_eq!(get_active_pricing_version(), 0);
        // A second v=0 is now a replay → rejected.
        assert_eq!(
            reload_pricing_signed_internal(&v0, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS),
            FFI_PRICING_VERSION_NOT_MONOTONIC
        );
        // Forward to v=1 succeeds.
        let v1 = make_signed_pricing_envelope(&key, &sample_bundle(1, TEST_NOW_SECS));
        assert_eq!(
            reload_pricing_signed_internal(&v1, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS),
            FFI_OK
        );
        assert_eq!(get_active_pricing_version(), 1);
    }

    #[test]
    fn reload_rejects_stale_bundle_but_accepts_at_max_age_boundary() {
        init_engine();
        let key = signing_key();
        let trusted = trusted_keys_json(&key);
        // 25h old, max age 24h → too old.
        let stale =
            make_signed_pricing_envelope(&key, &sample_bundle(1, TEST_NOW_SECS - 25 * 3600));
        assert_eq!(
            reload_pricing_signed_internal(&stale, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS),
            FFI_PRICING_BUNDLE_TOO_OLD
        );
        // Exactly max_age old is still accepted (strict-greater check).
        let boundary = make_signed_pricing_envelope(
            &key,
            &sample_bundle(1, TEST_NOW_SECS - TEST_MAX_AGE_SECS),
        );
        assert_eq!(
            reload_pricing_signed_internal(&boundary, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS),
            FFI_OK
        );
    }

    #[test]
    fn reload_rejects_future_bundle_but_accepts_within_skew() {
        init_engine();
        let key = signing_key();
        let trusted = trusted_keys_json(&key);
        // 10 min ahead, past the 5 min skew → rejected.
        let future = make_signed_pricing_envelope(&key, &sample_bundle(1, TEST_NOW_SECS + 600));
        assert_eq!(
            reload_pricing_signed_internal(&future, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS),
            FFI_PRICING_BUNDLE_IN_FUTURE
        );
        // Exactly at the skew boundary is accepted.
        let edge = make_signed_pricing_envelope(
            &key,
            &sample_bundle(1, TEST_NOW_SECS + PRICING_BUNDLE_FUTURE_SKEW_SECS),
        );
        assert_eq!(
            reload_pricing_signed_internal(&edge, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS),
            FFI_OK
        );
    }

    // ----- set_initial_pricing_version one-shot -------------------------

    #[test]
    fn set_initial_pricing_version_is_one_shot() {
        init_engine();
        assert_eq!(set_initial_pricing_version(10), FFI_OK);
        assert_eq!(get_active_pricing_version(), 10);
        // Second call rejected.
        assert_eq!(
            set_initial_pricing_version(5),
            crate::interface::FFI_PRICING_VERSION_ALREADY_SET
        );
        assert_eq!(get_active_pricing_version(), 10);
    }

    #[test]
    fn set_initial_pricing_version_then_reload_enforces_monotonic() {
        init_engine();
        assert_eq!(set_initial_pricing_version(10), FFI_OK);
        let key = signing_key();
        let trusted = trusted_keys_json(&key);
        // v=8 replay rejected after restore at v=10.
        let v8 = make_signed_pricing_envelope(&key, &sample_bundle(8, TEST_NOW_SECS));
        assert_eq!(
            reload_pricing_signed_internal(&v8, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS),
            FFI_PRICING_VERSION_NOT_MONOTONIC
        );
        // v=11 succeeds.
        let v11 = make_signed_pricing_envelope(&key, &sample_bundle(11, TEST_NOW_SECS));
        assert_eq!(
            reload_pricing_signed_internal(&v11, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS),
            FFI_OK
        );
        assert_eq!(get_active_pricing_version(), 11);
    }

    #[test]
    fn set_initial_pricing_version_requires_initialized_engine() {
        crate::interface::reset_engine_for_test();
        assert_eq!(
            set_initial_pricing_version(1),
            crate::interface::FFI_POLICY_ENGINE_NOT_INITIALIZED
        );
    }

    /// CROSS-RESTART ROLLBACK lockout: if a *real* signed pricing install has
    /// already happened this process, the persisted-version restore path MUST be
    /// locked out (-24) — otherwise it could be abused to roll the in-process
    /// high-water-mark backwards and re-open a replay window. Parity with
    /// `set_initial_policy_version_rejects_when_real_install_already_happened`,
    /// which had no pricing equivalent. This is distinct from
    /// `set_initial_pricing_version_is_one_shot` (two restores): here the FIRST
    /// write to the counter is a genuine reload, and the restore is the second.
    #[test]
    fn set_initial_pricing_version_rejects_after_real_install() {
        init_engine();
        let key = signing_key();
        let trusted = trusted_keys_json(&key);
        // A real signed install advances the counter to 7 and sets the
        // installed flag.
        let v7 = make_signed_pricing_envelope(&key, &sample_bundle(7, TEST_NOW_SECS));
        assert_eq!(
            reload_pricing_signed_internal(&v7, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS),
            FFI_OK
        );
        assert_eq!(get_active_pricing_version(), 7);
        // Now a "restore from persistence" at a LOWER version must be refused —
        // the in-process counter is authoritative once a bundle is installed.
        assert_eq!(
            set_initial_pricing_version(3),
            crate::interface::FFI_PRICING_VERSION_ALREADY_SET
        );
        // High-water-mark unmoved; the restore could not lower it.
        assert_eq!(get_active_pricing_version(), 7);
    }

    // =====================================================================
    // settle_usage — disabled / unpriced / priced / cache / overflow.
    // These call `settle()` directly (pure, no engine) plus a couple that
    // round-trip through `settle_usage_internal` against a live engine.
    // =====================================================================

    #[test]
    fn settle_disabled_when_no_bundle() {
        let usage = UsageInput {
            provider: "openai".to_string(),
            model: "gpt-4o".to_string(),
            input_tokens: 100,
            output_tokens: 50,
            ..Default::default()
        };
        let r = settle(None, &usage);
        assert_eq!(r.pricing_status, PricingStatus::Disabled);
        assert_eq!(r.cost_usd_micros, 0);
        assert_eq!(r.pricing_bundle_version, 0);
        assert_eq!(r.currency, "USD");
        assert!(r.sku_id.is_none());
        assert!(!r.overflow);
    }

    #[test]
    fn settle_unpriced_model_when_no_sku_matches() {
        let bundle = PricingBundle::new(
            9,
            TEST_NOW_SECS,
            Rounding::HalfUp,
            // Only an anthropic catch-all; an openai call won't match (provider
            // must match exactly).
            vec![sample_skus().remove(1)],
        );
        let usage = UsageInput {
            provider: "openai".to_string(),
            model: "gpt-4o".to_string(),
            input_tokens: 100,
            output_tokens: 50,
            ..Default::default()
        };
        let r = settle(Some(&bundle), &usage);
        assert_eq!(r.pricing_status, PricingStatus::UnpricedModel);
        assert_eq!(r.cost_usd_micros, 0);
        assert_eq!(r.pricing_bundle_version, 9);
        assert!(r.sku_id.is_none());
    }

    /// An installed bundle that carries ZERO skus is `unpriced_model`, NOT
    /// `disabled`. The distinction matters operationally: `disabled` means "no
    /// price table at all" (metering not configured), whereas an empty-but-
    /// present table means "table exists, this call just isn't priced by it" —
    /// a backfill candidate, and the bundle version is reported so the dashboard
    /// knows which (empty) table was in force. Proves the two fail-open statuses
    /// are not conflated.
    #[test]
    fn settle_empty_skus_is_unpriced_model_not_disabled() {
        let bundle = PricingBundle::new(12, TEST_NOW_SECS, Rounding::HalfUp, vec![]);
        let usage = UsageInput {
            provider: "anthropic".to_string(),
            model: "claude-sonnet-4-5".to_string(),
            input_tokens: 100,
            output_tokens: 50,
            ..Default::default()
        };
        let r = settle(Some(&bundle), &usage);
        assert_eq!(r.pricing_status, PricingStatus::UnpricedModel);
        assert_eq!(r.cost_usd_micros, 0);
        // Version reported (table present), distinguishing from the version-0
        // `disabled` no-bundle case.
        assert_eq!(r.pricing_bundle_version, 12);
        assert!(r.sku_id.is_none());
    }

    #[test]
    fn settle_priced_exact_worked_example() {
        // SKU: input $3/1M (3_000_000 micros/1M), output $15/1M. No cache tokens.
        // 1500 input * 3_000_000 / 1_000_000 = 4500 micros.
        //  350 output * 15_000_000 / 1_000_000 = 5250 micros.
        // total = 9750 micros ($0.00975). Exact, no rounding, no overflow.
        let bundle = sample_bundle(7, TEST_NOW_SECS);
        let usage = UsageInput {
            provider: "anthropic".to_string(),
            model: "claude-sonnet-4-5".to_string(),
            input_tokens: 1500,
            output_tokens: 350,
            ..Default::default()
        };
        let r = settle(Some(&bundle), &usage);
        assert_eq!(r.pricing_status, PricingStatus::Priced);
        assert_eq!(r.cost_usd_micros, 9750);
        assert!(!r.overflow);
        assert_eq!(r.sku_id.as_deref(), Some("anthropic-claude-sonnet"));
        assert_eq!(r.pricing_bundle_version, 7);
    }

    #[test]
    fn settle_cache_tokens_are_netted_out_of_fresh_input() {
        // input_total=1500, of which 1000 cache-read and 200 cache-creation, so
        // fresh_input=300. With the Sonnet SKU:
        //   fresh   300 * 3_000_000 /1M =   900
        //   read   1000 *   300_000 /1M =   300
        //   create  200 * 3_750_000 /1M =   750
        //   output  350 * 15_000_000/1M =  5250
        //   total                        =  7200 micros.
        let bundle = sample_bundle(1, TEST_NOW_SECS);
        let usage = UsageInput {
            provider: "anthropic".to_string(),
            model: "claude-sonnet-4-5".to_string(),
            input_tokens: 1500,
            output_tokens: 350,
            cache_read_tokens: Some(1000),
            cache_creation_tokens: Some(200),
            ..Default::default()
        };
        let r = settle(Some(&bundle), &usage);
        assert_eq!(r.cost_usd_micros, 7200);
        assert_eq!(r.pricing_status, PricingStatus::Priced);
    }

    /// REAL OPENAI INVOICE — the cache-read tokens are a SUBSET already inside
    /// `prompt_tokens`. OpenAI's `usage.prompt_tokens` is the TOTAL prompt
    /// (cached + fresh) and `prompt_tokens_details.cached_tokens` is the cached
    /// subset; OpenAI auto-caches and does NOT bill a separate cache-write line.
    /// So the wrapper sends `input_tokens = prompt_tokens` (10_000),
    /// `cache_read_tokens = cached_tokens` (8_000), and NO cache_creation. The
    /// netting `fresh = input - cache_read - cache_creation` must recover the
    /// fresh prompt portion and reproduce the real GPT-4o bill line-for-line.
    ///
    /// GPT-4o published list price: input $2.50/1M, cached input $1.25/1M,
    /// output $10.00/1M. For 10_000 prompt (8_000 cached) + 500 completion:
    ///   fresh   2_000 * 2_500_000 /1M =  5_000
    ///   cached  8_000 * 1_250_000 /1M = 10_000
    ///   output    500 * 10_000_000/1M =  5_000
    ///   total                          = 20_000 micros = $0.020 — the invoice.
    /// If the netting double-counted the 8_000 cached tokens as fresh input,
    /// the figure would be 5_000 + (8_000*2.5) + 10_000 + 5_000, far over the
    /// real bill; this asserts the exact subtraction.
    #[test]
    fn settle_openai_cache_subset_reproduces_real_invoice() {
        let openai_sku = PriceSku {
            sku_id: "openai-gpt-4o".to_string(),
            provider: "openai".to_string(),
            model_match: "gpt-4o".to_string(),
            unit: PriceUnit::Per1mTokens,
            input_usd_micros_per_unit: 2_500_000,
            output_usd_micros_per_unit: 10_000_000,
            cache_read_usd_micros_per_unit: Some(1_250_000),
            // OpenAI auto-caching has no cache-write line item.
            cache_write_usd_micros_per_unit: None,
            default_max_output_tokens: 16_384,
            effective_from: 1_700_000_000,
            deprecated_after: None,
            source: PriceSource::List,
        };
        let bundle = PricingBundle::new(3, TEST_NOW_SECS, Rounding::HalfUp, vec![openai_sku]);
        let usage = UsageInput {
            provider: "openai".to_string(),
            model: "gpt-4o".to_string(),
            // prompt_tokens (total, cache-inclusive) and the cached subset.
            input_tokens: 10_000,
            output_tokens: 500,
            cache_read_tokens: Some(8_000),
            cache_creation_tokens: None, // OpenAI does not report/bill this
            ..Default::default()
        };
        let r = settle(Some(&bundle), &usage);
        assert_eq!(r.cost_usd_micros, 20_000, "must equal the real GPT-4o bill");
        assert_eq!(r.pricing_status, PricingStatus::Priced);
        assert_eq!(r.sku_id.as_deref(), Some("openai-gpt-4o"));
    }

    #[test]
    fn settle_cache_falls_back_to_input_rate_when_sku_unpriced() {
        // The catch-all anthropic SKU has no cache pricing → cache tokens bill
        // at the input rate. Force a match to it with a non-sonnet model.
        // input_total=1000 (600 cache-read), output=0. input rate $1/1M.
        //   fresh   400 * 1_000_000 /1M = 400
        //   read    600 * 1_000_000 /1M = 600   (fallback to input rate)
        //   total                        = 1000 micros.
        let bundle = sample_bundle(1, TEST_NOW_SECS);
        let usage = UsageInput {
            provider: "anthropic".to_string(),
            model: "claude-opus-4".to_string(), // matches "**", not "claude-sonnet-*"
            input_tokens: 1000,
            output_tokens: 0,
            cache_read_tokens: Some(600),
            ..Default::default()
        };
        let r = settle(Some(&bundle), &usage);
        assert_eq!(r.sku_id.as_deref(), Some("anthropic-default"));
        assert_eq!(r.cost_usd_micros, 1000);
    }

    #[test]
    fn settle_resolves_most_specific_sku() {
        // "claude-sonnet-*" (14 literal chars) is more specific than "**" (0),
        // so a sonnet model resolves to the sonnet SKU even though both match.
        let bundle = sample_bundle(1, TEST_NOW_SECS);
        let usage = UsageInput {
            provider: "anthropic".to_string(),
            model: "claude-sonnet-4-5".to_string(),
            ..Default::default()
        };
        let r = settle(Some(&bundle), &usage);
        assert_eq!(r.sku_id.as_deref(), Some("anthropic-claude-sonnet"));
    }

    #[test]
    fn settle_tie_breaks_on_newest_effective_from() {
        // Two SKUs with identical specificity (exact literal match, 0 wildcards)
        // for the same model; the one with the newer effective_from wins.
        let old = PriceSku {
            sku_id: "old-rate".to_string(),
            provider: "openai".to_string(),
            model_match: "gpt-4o".to_string(),
            unit: PriceUnit::Per1mTokens,
            input_usd_micros_per_unit: 5_000_000,
            output_usd_micros_per_unit: 15_000_000,
            cache_read_usd_micros_per_unit: None,
            cache_write_usd_micros_per_unit: None,
            default_max_output_tokens: 4096,
            effective_from: 1_700_000_000,
            deprecated_after: None,
            source: PriceSource::List,
        };
        let new = PriceSku {
            sku_id: "new-rate".to_string(),
            effective_from: 1_800_000_000, // newer
            input_usd_micros_per_unit: 2_500_000,
            ..old.clone()
        };
        // Put the OLD one last to prove effective_from, not position, decides.
        let bundle = PricingBundle::new(1, TEST_NOW_SECS, Rounding::HalfUp, vec![new, old]);
        let usage = UsageInput {
            provider: "openai".to_string(),
            model: "gpt-4o".to_string(),
            input_tokens: 1_000_000, // 1M tokens → cost == per-unit price
            output_tokens: 0,
            ..Default::default()
        };
        let r = settle(Some(&bundle), &usage);
        assert_eq!(r.sku_id.as_deref(), Some("new-rate"));
        assert_eq!(r.cost_usd_micros, 2_500_000);
    }

    #[test]
    fn settle_saturates_and_flags_overflow() {
        // Astronomically large token counts at a large rate saturate Cost to
        // i64::MAX and set overflow — the figure is marked approximate, never
        // panics, never drops the event.
        let bundle = sample_bundle(1, TEST_NOW_SECS);
        let usage = UsageInput {
            provider: "anthropic".to_string(),
            model: "claude-sonnet-4-5".to_string(),
            input_tokens: i64::MAX,
            output_tokens: i64::MAX,
            ..Default::default()
        };
        let r = settle(Some(&bundle), &usage);
        assert!(r.overflow);
        assert_eq!(r.cost_usd_micros, i64::MAX);
        assert_eq!(r.pricing_status, PricingStatus::Priced);
    }

    #[test]
    fn settle_negative_token_counts_clamp_to_zero() {
        // A malformed/negative count must not produce a negative or nonsense
        // cost — counts clamp at 0 (a zero-token call costs nothing).
        let bundle = sample_bundle(1, TEST_NOW_SECS);
        let usage = UsageInput {
            provider: "anthropic".to_string(),
            model: "claude-sonnet-4-5".to_string(),
            input_tokens: -100,
            output_tokens: -5,
            cache_read_tokens: Some(-3),
            ..Default::default()
        };
        let r = settle(Some(&bundle), &usage);
        assert_eq!(r.cost_usd_micros, 0);
        assert!(!r.overflow);
    }

    #[test]
    fn settle_reasoning_tokens_are_not_billed_separately() {
        // reasoning_tokens is already inside output_tokens; supplying it must
        // not change the cost (only output_tokens drives output billing).
        let bundle = sample_bundle(1, TEST_NOW_SECS);
        let base = UsageInput {
            provider: "anthropic".to_string(),
            model: "claude-sonnet-4-5".to_string(),
            input_tokens: 0,
            output_tokens: 1000,
            ..Default::default()
        };
        let with_reasoning = UsageInput {
            reasoning_tokens: Some(400),
            ..base.clone()
        };
        assert_eq!(
            settle(Some(&bundle), &base).cost_usd_micros,
            settle(Some(&bundle), &with_reasoning).cost_usd_micros
        );
    }

    #[test]
    fn settle_usage_internal_disabled_on_uninitialized_engine() {
        // No engine → no bundle → Disabled (fail-open), never a panic.
        crate::interface::reset_engine_for_test();
        let usage = r#"{"provider":"openai","model":"gpt-4o","input_tokens":10,"output_tokens":5}"#;
        let r: SettleResult = serde_json::from_str(&settle_usage_internal("req", usage)).unwrap();
        assert_eq!(r.pricing_status, PricingStatus::Disabled);
    }

    #[test]
    fn settle_usage_internal_tolerates_malformed_usage_json() {
        // Garbage usage JSON deserializes to all-zero usage → cost 0, no panic.
        init_engine();
        let key = signing_key();
        let envelope = make_signed_pricing_envelope(&key, &sample_bundle(1, TEST_NOW_SECS));
        reload_pricing_signed_internal(
            &envelope,
            &trusted_keys_json(&key),
            TEST_NOW_SECS,
            TEST_MAX_AGE_SECS,
        );
        let r: SettleResult =
            serde_json::from_str(&settle_usage_internal("req", "{not json")).unwrap();
        assert_eq!(r.cost_usd_micros, 0);
    }

    // ----- SettleResult wire format snapshot ----------------------------

    #[test]
    fn settle_result_json_wire_shape_is_stable() {
        // Pin the exact JSON the wrapper parses. No `insta` dependency in this
        // crate, so this is a plain serde assertion (the documented fallback).
        let bundle = sample_bundle(7, TEST_NOW_SECS);
        let usage = UsageInput {
            provider: "anthropic".to_string(),
            model: "claude-sonnet-4-5".to_string(),
            input_tokens: 1500,
            output_tokens: 350,
            ..Default::default()
        };
        let json = serde_json::to_string(&settle(Some(&bundle), &usage)).unwrap();
        assert_eq!(
            json,
            r#"{"cost_usd_micros":9750,"currency":"USD","pricing_bundle_version":7,"pricing_status":"priced","overflow":false,"sku_id":"anthropic-claude-sonnet"}"#
        );
        // Pin the snake_case status wire values too.
        assert_eq!(
            serde_json::to_string(&PricingStatus::UnpricedModel).unwrap(),
            "\"unpriced_model\""
        );
        assert_eq!(
            serde_json::to_string(&PricingStatus::Untallied).unwrap(),
            "\"untallied\""
        );
        assert_eq!(
            serde_json::to_string(&PricingStatus::Disabled).unwrap(),
            "\"disabled\""
        );
    }

    // ----- Model glob unit tests ----------------------------------------

    #[test]
    fn model_glob_matches_and_specificity() {
        assert!(model_matches("claude-sonnet-*", "claude-sonnet-4-5"));
        assert!(model_matches("claude-sonnet-*", "claude-sonnet-")); // `*` matches empty
        assert!(!model_matches("claude-sonnet-*", "claude-opus-4"));
        assert!(model_matches("*", "anything-at-all"));
        assert!(model_matches("**", "")); // catch-all matches empty
        assert!(model_matches("gpt-4o", "gpt-4o")); // exact literal
        assert!(!model_matches("gpt-4o", "gpt-4o-mini")); // no trailing wildcard
        assert!(model_matches("gpt-*-mini", "gpt-4o-mini")); // interior wildcard
        assert!(model_matches("gpt-*-mini", "gpt--mini")); // wildcard run = empty
        assert!(!model_matches("gpt-*-mini", "gpt-mini")); // the literal `-mini` still required
        assert!(!model_matches("gpt-*-mini", "gpt-4o")); // suffix required

        // `**` collapses to `*` for a single-token id.
        assert!(model_matches("claude-**", "claude-3-opus"));

        // Specificity counts only literal chars.
        assert_eq!(model_specificity("claude-sonnet-*"), 14);
        assert_eq!(model_specificity("**"), 0);
        assert_eq!(model_specificity("gpt-4o"), 6);
        assert!(model_specificity("claude-sonnet-*") > model_specificity("**"));
    }

    /// Each glob shape called out in the `PriceSku::model_match` contract,
    /// asserted explicitly: prefix (`gpt-4o*`), suffix (`*-mini`), infix
    /// (`gpt-*-preview`), exact, bare `*`/`**`, empty pattern, and clear
    /// non-matches. The combined test above mixes several; this isolates each
    /// shape so a regression names the exact form that broke.
    #[test]
    fn model_glob_covers_every_shape() {
        // Prefix.
        assert!(model_matches("gpt-4o*", "gpt-4o"));
        assert!(model_matches("gpt-4o*", "gpt-4o-mini"));
        assert!(model_matches("gpt-4o*", "gpt-4o-2024-08-06"));
        assert!(!model_matches("gpt-4o*", "gpt-4-turbo"));
        // Suffix.
        assert!(model_matches("*-mini", "gpt-4o-mini"));
        assert!(model_matches("*-mini", "-mini"));
        assert!(!model_matches("*-mini", "gpt-4o"));
        // Infix.
        assert!(model_matches("gpt-*-preview", "gpt-4-preview"));
        assert!(model_matches("gpt-*-preview", "gpt-4o-2024-preview"));
        assert!(!model_matches("gpt-*-preview", "gpt-4-final"));
        // Exact (no wildcard) matches only itself.
        assert!(model_matches("o1-mini", "o1-mini"));
        assert!(!model_matches("o1-mini", "o1"));
        assert!(!model_matches("o1-mini", "o1-mini-2024"));
        // Bare wildcards.
        assert!(model_matches("*", ""));
        assert!(model_matches("*", "literally-anything"));
        assert!(model_matches("**", "literally-anything"));
        // Empty pattern matches ONLY the empty string.
        assert!(model_matches("", ""));
        assert!(!model_matches("", "anything"));

        // Specificity ordering across shapes: a longer literal body is more
        // specific. exact > prefix-with-long-literal > short-literal-glob > `**`.
        assert!(model_specificity("gpt-4o-mini") > model_specificity("gpt-4o*"));
        assert!(model_specificity("gpt-4o*") > model_specificity("gpt-*"));
        assert!(model_specificity("gpt-*") > model_specificity("**"));
        assert_eq!(model_specificity(""), 0);
    }

    /// CATASTROPHIC-BACKTRACKING SAFETY. The classic ReDoS-shaped pattern
    /// `a*a*a*...a*X` against a long run of `a`s (which arms every `*` but never
    /// satisfies the trailing literal `X`) destroys a naive recursive matcher
    /// exponentially. This greedy two-pointer matcher is LINEAR in the text
    /// length, so even an adversarially-shaped pattern terminates in
    /// microseconds. The pattern comes from the *signed* bundle (so it's
    /// trusted), but `settle` runs on the agent's hot path for every call — a
    /// matcher that could be made to spin would still be a self-inflicted DoS,
    /// so this pins the no-blow-up property with a wall-clock ceiling.
    #[test]
    fn model_glob_no_catastrophic_backtracking() {
        // `a*a*...a*` (30 pairs) followed by a literal `z` the text never has.
        let mut pattern: String = "a*".repeat(30);
        pattern.push('z');
        let model: String = "a".repeat(4000);

        let start = std::time::Instant::now();
        let matched = model_matches(&pattern, &model);
        let elapsed = start.elapsed();

        assert!(!matched, "no `z` in the text, so the pattern cannot match");
        // Linear matcher: well under a ms. A generous 200ms ceiling still fails
        // hard if the matcher ever regresses to exponential backtracking.
        assert!(
            elapsed < std::time::Duration::from_millis(200),
            "model_matches took {elapsed:?} — possible catastrophic backtracking"
        );

        // A second adversarial shape: alternating wildcards with a mismatching
        // tail, longer text. Also must stay fast.
        let pattern2: String = "*a".repeat(40) + "*b";
        let model2: String = "a".repeat(8000);
        let start2 = std::time::Instant::now();
        let matched2 = model_matches(&pattern2, &model2);
        assert!(!matched2);
        assert!(
            start2.elapsed() < std::time::Duration::from_millis(200),
            "second pathological pattern blew up"
        );
    }

    /// Resolution actually USES specificity to pick among overlapping globs: a
    /// concrete bundle with `**`, a prefix glob, and an exact SKU all matching
    /// the same model must resolve to the exact one (highest literal count).
    /// Complements the unit specificity asserts with an end-to-end `settle`.
    #[test]
    fn settle_specificity_picks_exact_over_globs() {
        let mk = |sku_id: &str, model_match: &str| PriceSku {
            sku_id: sku_id.to_string(),
            provider: "openai".to_string(),
            model_match: model_match.to_string(),
            unit: PriceUnit::Per1mTokens,
            input_usd_micros_per_unit: 1_000_000,
            output_usd_micros_per_unit: 1_000_000,
            cache_read_usd_micros_per_unit: None,
            cache_write_usd_micros_per_unit: None,
            default_max_output_tokens: 4096,
            effective_from: 1_700_000_000,
            deprecated_after: None,
            source: PriceSource::List,
        };
        // Deliberately order most-specific FIRST to prove ordering, not
        // position, decides (max_by_key independence).
        let bundle = PricingBundle::new(
            1,
            TEST_NOW_SECS,
            Rounding::HalfUp,
            vec![
                mk("exact", "gpt-4o-mini"),
                mk("prefix", "gpt-4o*"),
                mk("catchall", "**"),
            ],
        );
        let usage = UsageInput {
            provider: "openai".to_string(),
            model: "gpt-4o-mini".to_string(),
            ..Default::default()
        };
        assert_eq!(
            settle(Some(&bundle), &usage).sku_id.as_deref(),
            Some("exact")
        );
        // A model that only the prefix + catchall match resolves to the prefix.
        let usage2 = UsageInput {
            provider: "openai".to_string(),
            model: "gpt-4o-2024-08-06".to_string(),
            ..Default::default()
        };
        assert_eq!(
            settle(Some(&bundle), &usage2).sku_id.as_deref(),
            Some("prefix")
        );
        // A model only the catchall matches resolves to the catchall.
        let usage3 = UsageInput {
            provider: "openai".to_string(),
            model: "o1-preview".to_string(),
            ..Default::default()
        };
        assert_eq!(
            settle(Some(&bundle), &usage3).sku_id.as_deref(),
            Some("catchall")
        );
    }

    // ----- Property fuzz: settle never panics ---------------------------

    proptest::proptest! {
        #![proptest_config(proptest::prelude::ProptestConfig::with_cases(512))]

        /// Totality of the cost path: arbitrary token counts (including
        /// negatives and extremes) against a real bundle must never panic and
        /// must produce a non-negative cost (or a saturated, overflow-flagged
        /// figure). The money domain is non-negative by clamping.
        #[test]
        fn settle_never_panics_on_arbitrary_usage(
            input in proptest::prelude::any::<i64>(),
            output in proptest::prelude::any::<i64>(),
            cache_read in proptest::prelude::any::<i64>(),
            cache_creation in proptest::prelude::any::<i64>(),
        ) {
            let bundle = sample_bundle(1, TEST_NOW_SECS);
            let usage = UsageInput {
                provider: "anthropic".to_string(),
                model: "claude-sonnet-4-5".to_string(),
                input_tokens: input,
                output_tokens: output,
                cache_read_tokens: Some(cache_read),
                cache_creation_tokens: Some(cache_creation),
                reasoning_tokens: None,
            };
            let r = settle(Some(&bundle), &usage);
            // Cost is non-negative (clamped domain) or saturated-with-flag.
            prop_assert!(r.cost_usd_micros >= 0 || r.overflow);
        }
    }

    // =====================================================================
    // DSSE proptest fuzz on the pricing reload path — cloned from
    // dsse_verify.rs. `reload_pricing_signed_internal` handles untrusted
    // control-plane bytes; it must never panic, hang, or take unbounded time
    // on arbitrary envelopes / keys.
    // =====================================================================

    proptest::proptest! {
        #![proptest_config(proptest::prelude::ProptestConfig::with_cases(1024))]

        #[test]
        fn proptest_reload_does_not_panic_on_arbitrary_envelope_bytes(
            bytes in proptest::collection::vec(proptest::prelude::any::<u8>(), 0..2048),
            now in proptest::prelude::any::<u64>(),
            max_age in proptest::prelude::any::<u64>(),
        ) {
            // Arbitrary bytes as the envelope JSON. Must return an Err code,
            // never panic. The engine may or may not be initialized — either
            // way the call is total.
            let env_str = String::from_utf8_lossy(&bytes);
            let trusted = trusted_keys_json(&signing_key());
            let _ = reload_pricing_signed_internal(&env_str, &trusted, now, max_age);
        }

        #[test]
        fn proptest_reload_does_not_panic_on_structured_arbitrary_envelope(
            payload in proptest::collection::vec(proptest::prelude::any::<u8>(), 0..512),
            sig_bytes in proptest::collection::vec(proptest::prelude::any::<u8>(), 0..256),
            keyid in r"[\PC]{0,40}",
            now in proptest::prelude::any::<u64>(),
            max_age in proptest::prelude::any::<u64>(),
        ) {
            // A structurally well-formed envelope built from arbitrary bytes.
            // Verification must fail (signature won't verify) without panicking
            // or hanging.
            use base64::engine::general_purpose::STANDARD as B64;
            use base64::Engine;
            let envelope = DsseEnvelope {
                payload_type: PRICING_BUNDLE_PAYLOAD_TYPE.to_string(),
                payload: B64.encode(&payload),
                signatures: vec![checkrd_shared::dsse::DsseSignature {
                    keyid,
                    sig: B64.encode(&sig_bytes),
                }],
            };
            let env_json = serde_json::to_string(&envelope).unwrap();
            let trusted = trusted_keys_json(&signing_key());
            let _ = reload_pricing_signed_internal(&env_json, &trusted, now, max_age);
        }

        #[test]
        fn proptest_reload_with_arbitrary_trust_list_does_not_panic(
            trust_count in 0usize..16,
            valid_from in proptest::prelude::any::<u64>(),
            valid_until in proptest::prelude::any::<u64>(),
            now in proptest::prelude::any::<u64>(),
        ) {
            // Random validity windows exercise the window-check arithmetic at
            // the edges of u64 without panicking.
            let key = signing_key();
            let envelope = make_signed_pricing_envelope(&key, &sample_bundle(1, TEST_NOW_SECS));
            let pk: String = key
                .verifying_key()
                .to_bytes()
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect();
            let keys: Vec<serde_json::Value> = (0..trust_count)
                .map(|_| serde_json::json!({
                    "keyid": "test-cp", "public_key_hex": pk,
                    "valid_from": valid_from, "valid_until": valid_until,
                }))
                .collect();
            let trusted = serde_json::to_string(&keys).unwrap();
            let _ = reload_pricing_signed_internal(&envelope, &trusted, now, TEST_MAX_AGE_SECS);
        }

        #[test]
        fn proptest_model_matches_never_panics(
            pattern in r"[\PC]{0,40}",
            model in r"[\PC]{0,60}",
        ) {
            // The glob runs on untrusted bundle + provider data; it must be
            // total over arbitrary strings.
            let _ = model_matches(&pattern, &model);
            let _ = model_specificity(&pattern);
        }
    }
}
