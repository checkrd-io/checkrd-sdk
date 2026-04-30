use serde::{Deserialize, Serialize};
use typeshare::typeshare;

use crate::dsse::DsseEnvelope;
use crate::http::HttpMethod;
use crate::policy::PolicyResult;

// ---------------------------------------------------------------------------
// PII safety: path parameterization
// ---------------------------------------------------------------------------

/// Replace path segments that look like identifiers with `{id}`.
///
/// This is the PII-safety guarantee: URL paths can contain resource IDs,
/// account numbers, email addresses, etc. By parameterizing at the source
/// (the WASM core running in the customer's process), sensitive identifiers
/// never leave the customer's machine.
///
/// Recognized patterns:
/// - UUIDs: `550e8400-e29b-41d4-a716-446655440000` -> `{id}`
/// - Numeric IDs: `12345`, `001` -> `{id}`
/// - Hex strings (8+ chars): `a1b2c3d4e5f6` -> `{id}`
/// - Base64-ish (16+ chars with mixed case/digits/+/=): `dGVzdA==` -> `{id}`
/// - Email-like: `user@domain.com` -> `{id}`
/// - Prefixed IDs (Stripe, Square): `ch_abc123def456ghi` -> `{id}`
///
/// Static segments (API version prefixes, resource names) are preserved:
/// `/v1/charges/{id}` not `/v1/{id}/{id}`
pub fn parameterize_path(path: &str) -> String {
    let mut result = String::with_capacity(path.len());
    result.push('/');

    let segments: Vec<&str> = path.split('/').filter(|s| !s.is_empty()).collect();
    for (i, seg) in segments.iter().enumerate() {
        if i > 0 {
            result.push('/');
        }
        if is_identifier_segment(seg) {
            result.push_str("{id}");
        } else {
            result.push_str(seg);
        }
    }

    result
}

/// Returns true if a path segment looks like a dynamic identifier.
fn is_identifier_segment(seg: &str) -> bool {
    // UUID: 8-4-4-4-12 hex with dashes
    if seg.len() == 36
        && seg.chars().all(|c| c.is_ascii_hexdigit() || c == '-')
        && seg.chars().filter(|&c| c == '-').count() == 4
    {
        return true;
    }

    // Pure numeric (any length > 0)
    if !seg.is_empty() && seg.chars().all(|c| c.is_ascii_digit()) {
        return true;
    }

    // Hex string (8+ chars, all hex)
    if seg.len() >= 8 && seg.chars().all(|c| c.is_ascii_hexdigit()) {
        return true;
    }

    // Prefixed IDs common in APIs: ch_xxx, cus_xxx, sub_xxx, acct_xxx, pi_xxx
    // (Stripe, Square, payment processors)
    if seg.contains('_') {
        let parts: Vec<&str> = seg.splitn(2, '_').collect();
        if parts.len() == 2 && parts[0].len() <= 6 && parts[1].len() >= 4 {
            return true;
        }
    }

    // Email-like: contains @ with text on both sides
    if seg.contains('@') && seg.len() >= 5 {
        if let Some(at_pos) = seg.find('@') {
            if at_pos > 0 && at_pos < seg.len() - 1 {
                return true;
            }
        }
    }

    // Long mixed alphanumeric (base64, API keys, tokens): 16+ chars with mix of letters and digits
    if seg.len() >= 16 {
        let has_letters = seg.chars().any(|c| c.is_ascii_alphabetic());
        let has_digits = seg.chars().any(|c| c.is_ascii_digit());
        if has_letters && has_digits {
            return true;
        }
    }

    false
}

// ---------------------------------------------------------------------------
// Telemetry source — records which ingestion path produced the batch
// ---------------------------------------------------------------------------

/// Records which ingestion path authenticated and produced a telemetry batch.
///
/// Persisted to `telemetry_events.source` so dashboards can distinguish
/// SDK-governed events from externally-observed OTLP events.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TelemetrySource {
    /// Signed with Ed25519 via RFC 9421 + DSSE by the Checkrd SDK.
    #[default]
    SdkSigned,
    /// OTLP bridge: authenticated via bearer API key (no per-batch signature).
    Otlp,
}

impl TelemetrySource {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::SdkSigned => "sdk_signed",
            Self::Otlp => "otlp",
        }
    }
}

// ---------------------------------------------------------------------------
// Batch signature — groups all RFC 9421 + DSSE signature fields
// ---------------------------------------------------------------------------

/// All cryptographic signature fields for a single telemetry batch.
///
/// Present only on `SdkSigned` batches. OTLP batches omit this — no OTLP
/// SDK in the ecosystem signs payloads.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BatchSignature {
    /// Agent whose Ed25519 key signed this batch (verified by ingestion).
    pub signer_agent_id: uuid::Uuid,
    /// DSSE envelope for storage-layer non-repudiation.
    pub dsse_envelope: DsseEnvelope,
    /// Echoed RFC 9421 `Signature-Input` header for audit.
    pub http_signature_input: String,
    /// Echoed RFC 9421 `Signature` header for audit.
    pub http_signature: String,
    /// Echoed RFC 9530 `Content-Digest` header for audit.
    pub http_content_digest: String,
}

// ---------------------------------------------------------------------------
// Internal telemetry event (used by WASM core / Python wrapper round-trip)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TelemetryEvent {
    pub event_id: String,
    pub agent_id: String,
    pub instance_id: String,
    pub timestamp: String,
    pub request: TelemetryRequest,
    #[serde(default)]
    pub response: Option<TelemetryResponse>,
    pub policy_result: PolicyResult,
    #[serde(default)]
    pub deny_reason: Option<String>,
    pub trace_id: String,
    pub span_id: String,
    #[serde(default)]
    pub parent_span_id: Option<String>,
    #[serde(default)]
    pub span_name: String,
    #[serde(default)]
    pub span_kind: String,
    #[serde(default)]
    pub span_status_code: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TelemetryRequest {
    pub url_host: String,
    pub url_path: String,
    pub method: HttpMethod,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TelemetryResponse {
    pub status_code: u16,
    pub latency_ms: u64,
}

// ---------------------------------------------------------------------------
// Ingestion input type + validation (shared across API, ingestion, writer)
// ---------------------------------------------------------------------------

/// A single telemetry event as received from the SDK or translated from OTLP.
///
/// # PII Safety Contract
///
/// Every field on this struct has been classified as PII-safe. This struct is
/// the **sole interface** between the ingestion service and Aurora storage —
/// anything not represented here cannot be persisted.
///
/// Before adding a new field, you MUST:
/// 1. Classify it as PII-safe (see field comments below).
/// 2. Add it to the `ALLOWED_FIELD_NAMES` list in the test module.
/// 3. If the field contains customer-controlled free text, it MUST be
///    parameterized client-side (SDK path) or omitted (OTLP path).
///
/// The compile-time `pii_field_allowlist` test will fail if a field is added
/// without updating the allowlist — this is intentional.
#[typeshare]
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TelemetryEventInput {
    /// PII: SAFE — UUID generated by Checkrd, not customer data.
    pub request_id: String,
    /// PII: SAFE — UUID from Checkrd agent registry.
    pub agent_id: uuid::Uuid,
    /// PII: SAFE — derived from Ed25519 public key hash.
    #[serde(default)]
    pub instance_id: Option<String>,
    /// PII: SAFE — ISO 8601 timestamp.
    pub timestamp: String,
    /// PII: SAFE — public domain name (e.g., "api.stripe.com").
    pub url_host: String,
    /// PII: SAFE — parameterized path template from WASM core (SDK path) or
    /// "/" (OTLP path). Identifiers replaced with `{id}` client-side.
    pub url_path: String,
    /// PII: SAFE — constrained to VALID_METHODS enum.
    pub method: String,
    /// PII: SAFE — integer HTTP status code.
    #[serde(default)]
    pub status_code: Option<i16>,
    /// PII: SAFE — integer latency in milliseconds.
    #[serde(default)]
    pub latency_ms: Option<i32>,
    /// PII: SAFE — constrained to "allowed"/"denied"/None.
    #[serde(default)]
    pub policy_result: Option<String>,
    /// PII: SAFE — contains only rule names (e.g., "denied by rule 'block-deletes'"),
    /// never body field values. Verified: policy.rs only emits rule names.
    #[serde(default)]
    pub deny_reason: Option<String>,
    /// PII: SAFE — 32 lowercase hex chars (W3C Trace Context).
    #[serde(default)]
    pub trace_id: Option<String>,
    /// PII: SAFE — 16 lowercase hex chars.
    #[serde(default)]
    pub span_id: Option<String>,
    /// PII: SAFE — 16 lowercase hex chars.
    #[serde(default)]
    pub parent_span_id: Option<String>,
    /// PII: SAFE — derived as "{METHOD} {host}" by WASM core (SDK) and
    /// ingestion service (OTLP). Never contains customer-controlled text.
    #[serde(default)]
    pub span_name: Option<String>,
    /// PII: SAFE — constrained to VALID_SPAN_KINDS enum.
    #[serde(default)]
    pub span_kind: Option<String>,
    /// PII: SAFE — constrained to "OK"/"ERROR"/"UNSET".
    #[serde(default)]
    pub span_status_code: Option<String>,
    /// PII: SAFE — provider name ("openai", "anthropic"), not customer data.
    #[serde(default)]
    pub gen_ai_system: Option<String>,
    /// PII: SAFE — model identifier ("gpt-4o"), not customer data.
    #[serde(default)]
    pub gen_ai_model: Option<String>,
    /// PII: SAFE — integer token count.
    #[serde(default)]
    #[typeshare(serialized_as = "Option<number>")]
    pub gen_ai_input_tokens: Option<i64>,
    /// PII: SAFE — integer token count.
    #[serde(default)]
    #[typeshare(serialized_as = "Option<number>")]
    pub gen_ai_output_tokens: Option<i64>,
    /// PII: SAFE — rule name from core policy engine. Never contains customer
    /// body values; rule names are user-chosen in YAML.
    #[serde(default)]
    pub matched_rule: Option<String>,
    /// PII: SAFE — constrained to allow/deny/rate_limit/kill_switch/default enum.
    #[serde(default)]
    pub matched_rule_kind: Option<String>,
    /// PII: SAFE — constrained to enforce/dry_run enum (PolicyMode::serialize).
    #[serde(default)]
    pub policy_mode: Option<String>,
    /// PII: SAFE — ordered evaluation trace. Each step holds stage name +
    /// rule name + result enum + optional detail string. Rule names are
    /// user-authored YAML identifiers; no body values.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub evaluation_path: Vec<crate::policy::EvaluationStep>,
}

const VALID_METHODS: &[&str] = &["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"];
const VALID_RESULTS: &[&str] = &["allowed", "denied"];
const VALID_SPAN_KINDS: &[&str] = &["INTERNAL", "CLIENT", "SERVER", "PRODUCER", "CONSUMER"];
const VALID_SPAN_STATUS_CODES: &[&str] = &["OK", "ERROR", "UNSET"];

#[derive(Debug, Clone, thiserror::Error)]
pub enum TelemetryValidationError {
    #[error("{0} is required")]
    Required(&'static str),
    #[error("invalid value for {field}: {value}")]
    InvalidValue { field: &'static str, value: String },
}

fn validate_hex_id(
    value: &Option<String>,
    field: &'static str,
    expected_len: usize,
) -> Result<(), TelemetryValidationError> {
    if let Some(ref v) = value {
        if v.len() != expected_len || !v.chars().all(|c| c.is_ascii_hexdigit()) {
            return Err(TelemetryValidationError::InvalidValue {
                field,
                value: v.clone(),
            });
        }
    }
    Ok(())
}

pub fn validate_telemetry_event(
    event: &TelemetryEventInput,
) -> Result<(), TelemetryValidationError> {
    if event.request_id.is_empty() {
        return Err(TelemetryValidationError::Required("request_id"));
    }
    if event.url_host.is_empty() {
        return Err(TelemetryValidationError::Required("url_host"));
    }
    if !VALID_METHODS.contains(&event.method.as_str()) {
        return Err(TelemetryValidationError::InvalidValue {
            field: "method",
            value: event.method.clone(),
        });
    }
    if let Some(ref result) = event.policy_result {
        if !VALID_RESULTS.contains(&result.as_str()) {
            return Err(TelemetryValidationError::InvalidValue {
                field: "policy_result",
                value: result.clone(),
            });
        }
    }
    validate_hex_id(&event.trace_id, "trace_id", 32)?;
    validate_hex_id(&event.span_id, "span_id", 16)?;
    validate_hex_id(&event.parent_span_id, "parent_span_id", 16)?;
    if let Some(ref kind) = event.span_kind {
        if !VALID_SPAN_KINDS.contains(&kind.as_str()) {
            return Err(TelemetryValidationError::InvalidValue {
                field: "span_kind",
                value: kind.clone(),
            });
        }
    }
    if let Some(ref code) = event.span_status_code {
        if !VALID_SPAN_STATUS_CODES.contains(&code.as_str()) {
            return Err(TelemetryValidationError::InvalidValue {
                field: "span_status_code",
                value: code.clone(),
            });
        }
    }
    if let Some(tokens) = event.gen_ai_input_tokens {
        if tokens < 0 {
            return Err(TelemetryValidationError::InvalidValue {
                field: "gen_ai_input_tokens",
                value: tokens.to_string(),
            });
        }
    }
    if let Some(tokens) = event.gen_ai_output_tokens {
        if tokens < 0 {
            return Err(TelemetryValidationError::InvalidValue {
                field: "gen_ai_output_tokens",
                value: tokens.to_string(),
            });
        }
    }
    // B1: evaluation metadata validation
    if let Some(ref rule) = event.matched_rule {
        if rule.len() > 128 {
            return Err(TelemetryValidationError::InvalidValue {
                field: "matched_rule",
                value: format!("len={} (max 128)", rule.len()),
            });
        }
    }
    if let Some(ref kind) = event.matched_rule_kind {
        const VALID_KINDS: &[&str] = &["allow", "deny", "rate_limit", "kill_switch", "default"];
        if !VALID_KINDS.contains(&kind.as_str()) {
            return Err(TelemetryValidationError::InvalidValue {
                field: "matched_rule_kind",
                value: kind.clone(),
            });
        }
    }
    if let Some(ref mode) = event.policy_mode {
        if mode != "enforce" && mode != "dry_run" {
            return Err(TelemetryValidationError::InvalidValue {
                field: "policy_mode",
                value: mode.clone(),
            });
        }
    }
    if event.evaluation_path.len() > 32 {
        return Err(TelemetryValidationError::InvalidValue {
            field: "evaluation_path",
            value: format!("{} steps (max 32)", event.evaluation_path.len()),
        });
    }
    for step in &event.evaluation_path {
        if step.stage.is_empty() || step.stage.len() > 64 {
            return Err(TelemetryValidationError::InvalidValue {
                field: "evaluation_path.stage",
                value: format!("len={}", step.stage.len()),
            });
        }
        if let Some(ref rule) = step.rule {
            if rule.len() > 128 {
                return Err(TelemetryValidationError::InvalidValue {
                    field: "evaluation_path.rule",
                    value: format!("len={} (max 128)", rule.len()),
                });
            }
        }
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// SQS wire format v3
// ---------------------------------------------------------------------------

pub const TELEMETRY_BATCH_SCHEMA_VERSION: u32 = 3;

/// SQS message body: the contract between telemetry-ingestion and telemetry-writer.
///
/// v3: `source` field + `signature` as grouped `Option<BatchSignature>`.
/// Additive: `traceparent` (W3C Trace Context) for end-to-end request
/// correlation across the async pipeline. Backwards compatible — older
/// messages without this field still deserialize.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TelemetryBatchMessage {
    pub schema_version: u32,
    pub org_id: uuid::Uuid,
    pub api_key_id: uuid::Uuid,
    pub sdk_version: String,
    pub events: Vec<TelemetryEventInput>,
    pub batch_id: String,
    #[serde(default)]
    pub source: TelemetrySource,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub signature: Option<BatchSignature>,
    /// W3C Trace Context traceparent header for end-to-end request correlation.
    /// Format: `00-{trace_id}-{parent_id}-{flags}`
    /// (e.g., `00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01`).
    /// Propagated from the HTTP request that enqueued this batch. The
    /// telemetry-writer extracts `trace_id` from this field and uses it to
    /// create a correlated span for batch processing, so logs from the
    /// writer can be joined to the originating HTTP request in Grafana Loki.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub traceparent: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::policy::EvaluationStep;

    #[test]
    fn telemetry_event_round_trip() {
        let event = TelemetryEvent {
            event_id: "550e8400-e29b-41d4-a716-446655440000".into(),
            agent_id: "sales-agent-01".into(),
            instance_id: "abc123hash".into(),
            timestamp: "2026-03-28T14:30:00Z".into(),
            request: TelemetryRequest {
                url_host: "api.salesforce.com".into(),
                url_path: "/services/data/v58.0/sobjects/Contact/".into(),
                method: HttpMethod::GET,
            },
            response: Some(TelemetryResponse {
                status_code: 200,
                latency_ms: 142,
            }),
            policy_result: PolicyResult::Allowed,
            deny_reason: None,
            trace_id: "0af7651916cd43dd8448eb211c80319c".into(),
            span_id: "b7ad6b7169203331".into(),
            parent_span_id: Some("00f067aa0ba902b7".into()),
            span_name: "GET api.salesforce.com".into(),
            span_kind: "INTERNAL".into(),
            span_status_code: "OK".into(),
        };
        let json = serde_json::to_string(&event).unwrap();
        let de: TelemetryEvent = serde_json::from_str(&json).unwrap();
        assert_eq!(de, event);
    }

    #[test]
    fn source_serde() {
        assert_eq!(
            serde_json::to_string(&TelemetrySource::SdkSigned).unwrap(),
            "\"sdk_signed\""
        );
        assert_eq!(
            serde_json::to_string(&TelemetrySource::Otlp).unwrap(),
            "\"otlp\""
        );
        assert_eq!(TelemetrySource::default(), TelemetrySource::SdkSigned);
    }

    fn valid_event_input() -> TelemetryEventInput {
        TelemetryEventInput {
            request_id: "req-001".into(),
            agent_id: uuid::Uuid::new_v4(),
            instance_id: Some("inst-abc".into()),
            timestamp: "2026-03-28T14:30:00Z".into(),
            url_host: "api.stripe.com".into(),
            url_path: "/v1/charges".into(),
            method: "GET".into(),
            status_code: Some(200),
            latency_ms: Some(142),
            policy_result: Some("allowed".into()),
            deny_reason: None,
            trace_id: Some("0af7651916cd43dd8448eb211c80319c".into()),
            span_id: Some("b7ad6b7169203331".into()),
            parent_span_id: None,
            span_name: Some("GET api.stripe.com".into()),
            span_kind: Some("INTERNAL".into()),
            span_status_code: Some("OK".into()),
            gen_ai_system: None,
            gen_ai_model: None,
            gen_ai_input_tokens: None,
            gen_ai_output_tokens: None,
            matched_rule: Some("block-external-api".into()),
            matched_rule_kind: Some("deny".into()),
            policy_mode: Some("enforce".into()),
            evaluation_path: vec![
                EvaluationStep {
                    stage: "kill_switch".into(),
                    rule: None,
                    result: "pass".into(),
                    detail: None,
                },
                EvaluationStep {
                    stage: "deny_rules".into(),
                    rule: Some("block-external-api".into()),
                    result: "matched".into(),
                    detail: None,
                },
            ],
        }
    }

    fn otlp_event_input() -> TelemetryEventInput {
        TelemetryEventInput {
            request_id: "otlp-001".into(),
            agent_id: uuid::Uuid::new_v4(),
            instance_id: None,
            timestamp: "2026-04-08T10:00:00Z".into(),
            url_host: "api.anthropic.com".into(),
            url_path: "/v1/messages".into(),
            method: "POST".into(),
            status_code: Some(200),
            latency_ms: Some(1250),
            policy_result: None,
            deny_reason: None,
            trace_id: Some("abcdef1234567890abcdef1234567890".into()),
            span_id: Some("1234567890abcdef".into()),
            parent_span_id: None,
            span_name: Some("POST api.anthropic.com".into()),
            span_kind: Some("CLIENT".into()),
            span_status_code: Some("OK".into()),
            gen_ai_system: Some("anthropic".into()),
            gen_ai_model: Some("claude-sonnet-4-20250514".into()),
            gen_ai_input_tokens: Some(1500),
            gen_ai_output_tokens: Some(350),
            matched_rule: None,
            matched_rule_kind: None,
            policy_mode: None,
            evaluation_path: vec![],
        }
    }

    fn sample_signature(signer: uuid::Uuid) -> BatchSignature {
        BatchSignature {
            signer_agent_id: signer,
            dsse_envelope: crate::dsse::DsseEnvelope {
                payload_type: crate::dsse::TELEMETRY_BATCH_PAYLOAD_TYPE.to_string(),
                payload: "eyJldmVudHMiOltdfQ==".to_string(),
                signatures: vec![crate::dsse::DsseSignature {
                    keyid: "a1b2c3d4e5f6a7b8".to_string(),
                    sig: "AAAA".to_string(),
                }],
            },
            http_signature_input: "checkrd=(\"@method\");created=1;keyid=\"k\";alg=\"ed25519\""
                .into(),
            http_signature: "checkrd=:AAAA:".into(),
            http_content_digest: "sha-256=:abcd:".into(),
        }
    }

    fn sdk_signed_batch(events: Vec<TelemetryEventInput>, id: &str) -> TelemetryBatchMessage {
        let signer = events
            .first()
            .map(|e| e.agent_id)
            .unwrap_or_else(uuid::Uuid::new_v4);
        TelemetryBatchMessage {
            schema_version: TELEMETRY_BATCH_SCHEMA_VERSION,
            org_id: uuid::Uuid::new_v4(),
            api_key_id: uuid::Uuid::new_v4(),
            sdk_version: "0.3.0".into(),
            events,
            batch_id: id.into(),
            source: TelemetrySource::SdkSigned,
            signature: Some(sample_signature(signer)),
            traceparent: None,
        }
    }

    fn otlp_batch(events: Vec<TelemetryEventInput>, id: &str) -> TelemetryBatchMessage {
        TelemetryBatchMessage {
            schema_version: TELEMETRY_BATCH_SCHEMA_VERSION,
            org_id: uuid::Uuid::new_v4(),
            api_key_id: uuid::Uuid::new_v4(),
            sdk_version: String::new(),
            events,
            batch_id: id.into(),
            source: TelemetrySource::Otlp,
            signature: None,
            traceparent: None,
        }
    }

    #[test]
    fn validate_valid() {
        assert!(validate_telemetry_event(&valid_event_input()).is_ok());
    }
    #[test]
    fn validate_otlp() {
        assert!(validate_telemetry_event(&otlp_event_input()).is_ok());
    }
    #[test]
    fn validate_none_policy_ok() {
        let mut e = valid_event_input();
        e.policy_result = None;
        assert!(validate_telemetry_event(&e).is_ok());
    }
    #[test]
    fn validate_bad_policy() {
        let mut e = valid_event_input();
        e.policy_result = Some("maybe".into());
        assert!(validate_telemetry_event(&e).is_err());
    }
    #[test]
    fn validate_empty_req_id() {
        let mut e = valid_event_input();
        e.request_id = "".into();
        assert!(validate_telemetry_event(&e).is_err());
    }
    #[test]
    fn validate_empty_host() {
        let mut e = valid_event_input();
        e.url_host = "".into();
        assert!(validate_telemetry_event(&e).is_err());
    }
    #[test]
    fn validate_bad_method() {
        let mut e = valid_event_input();
        e.method = "HACK".into();
        assert!(validate_telemetry_event(&e).is_err());
    }
    #[test]
    fn validate_short_trace() {
        let mut e = valid_event_input();
        e.trace_id = Some("abcd".into());
        assert!(validate_telemetry_event(&e).is_err());
    }
    #[test]
    fn validate_neg_in_tok() {
        let mut e = otlp_event_input();
        e.gen_ai_input_tokens = Some(-1);
        assert!(validate_telemetry_event(&e).is_err());
    }
    #[test]
    fn validate_neg_out_tok() {
        let mut e = otlp_event_input();
        e.gen_ai_output_tokens = Some(-5);
        assert!(validate_telemetry_event(&e).is_err());
    }
    #[test]
    fn validate_all_methods() {
        for m in VALID_METHODS {
            let mut e = valid_event_input();
            e.method = m.to_string();
            assert!(validate_telemetry_event(&e).is_ok());
        }
    }
    #[test]
    fn validate_all_kinds() {
        for k in VALID_SPAN_KINDS {
            let mut e = valid_event_input();
            e.span_kind = Some(k.to_string());
            assert!(validate_telemetry_event(&e).is_ok());
        }
    }

    #[test]
    fn sdk_batch_round_trip() {
        let msg = sdk_signed_batch(vec![valid_event_input()], "b1");
        let json = serde_json::to_string(&msg).unwrap();
        let de: TelemetryBatchMessage = serde_json::from_str(&json).unwrap();
        assert_eq!(de.schema_version, 3);
        assert_eq!(de.source, TelemetrySource::SdkSigned);
        assert!(de.signature.is_some());
        assert_eq!(de.events.len(), 1);
    }

    #[test]
    fn otlp_batch_round_trip() {
        let msg = otlp_batch(vec![otlp_event_input()], "o1");
        let json = serde_json::to_string(&msg).unwrap();
        let de: TelemetryBatchMessage = serde_json::from_str(&json).unwrap();
        assert_eq!(de.source, TelemetrySource::Otlp);
        assert!(de.signature.is_none());
        assert!(de.events[0].policy_result.is_none());
        assert_eq!(de.events[0].gen_ai_system.as_deref(), Some("anthropic"));
    }

    #[test]
    fn otlp_batch_no_sig_key() {
        let json = serde_json::to_string(&otlp_batch(vec![], "x")).unwrap();
        assert!(!json.contains("\"signature\""));
    }

    #[test]
    fn schema_version_is_3() {
        assert_eq!(TELEMETRY_BATCH_SCHEMA_VERSION, 3);
    }

    // ---- traceparent field (W3C Trace Context end-to-end correlation) ----

    #[test]
    fn traceparent_field_round_trips_when_set() {
        let mut msg = sdk_signed_batch(vec![valid_event_input()], "tp1");
        msg.traceparent =
            Some("00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01".to_string());
        let json = serde_json::to_string(&msg).unwrap();
        let de: TelemetryBatchMessage = serde_json::from_str(&json).unwrap();
        assert_eq!(
            de.traceparent.as_deref(),
            Some("00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01")
        );
    }

    #[test]
    fn traceparent_defaults_to_none_when_missing() {
        // Simulate a v3 message without the traceparent field (forwards compat).
        let json = r#"{
            "schema_version": 3,
            "org_id": "550e8400-e29b-41d4-a716-446655440000",
            "api_key_id": "550e8400-e29b-41d4-a716-446655440001",
            "sdk_version": "0.3.0",
            "events": [],
            "batch_id": "backcompat-test",
            "source": "sdk_signed"
        }"#;
        let de: TelemetryBatchMessage = serde_json::from_str(json).unwrap();
        assert!(de.traceparent.is_none());
    }

    #[test]
    fn traceparent_is_omitted_from_json_when_none() {
        let msg = otlp_batch(vec![], "no-tp");
        assert!(msg.traceparent.is_none());
        let json = serde_json::to_string(&msg).unwrap();
        assert!(
            !json.contains("\"traceparent\""),
            "traceparent field should be omitted when None, got: {json}"
        );
    }

    #[test]
    fn traceparent_is_present_in_json_when_set() {
        let mut msg = otlp_batch(vec![], "with-tp");
        msg.traceparent = Some("00-abc-def-01".to_string());
        let json = serde_json::to_string(&msg).unwrap();
        assert!(json.contains("\"traceparent\":\"00-abc-def-01\""));
    }

    #[test]
    fn source_defaults_sdk_signed() {
        let json = serde_json::json!({
            "schema_version": 3, "org_id": "550e8400-e29b-41d4-a716-446655440000",
            "api_key_id": "550e8400-e29b-41d4-a716-446655440001",
            "sdk_version": "0.3.0", "batch_id": "t", "events": [],
            "signature": {
                "signer_agent_id": "550e8400-e29b-41d4-a716-446655440002",
                "dsse_envelope": { "payloadType": "application/vnd.checkrd.telemetry-batch+json", "payload": "e30=", "signatures": [{"keyid":"k","sig":"s"}] },
                "http_signature_input": "x", "http_signature": "y", "http_content_digest": "z"
            }
        }).to_string();
        let de: TelemetryBatchMessage = serde_json::from_str(&json).unwrap();
        assert_eq!(de.source, TelemetrySource::SdkSigned);
    }

    #[test]
    fn event_input_without_policy() {
        let json = r#"{"request_id":"r","agent_id":"550e8400-e29b-41d4-a716-446655440000","timestamp":"2026-01-01T00:00:00Z","url_host":"x.com","url_path":"/","method":"GET","gen_ai_system":"openai","gen_ai_input_tokens":100}"#;
        let e: TelemetryEventInput = serde_json::from_str(json).unwrap();
        assert!(e.policy_result.is_none());
        assert_eq!(e.gen_ai_system.as_deref(), Some("openai"));
    }

    // --- PII safety: path parameterization ---

    #[test]
    fn param_uuid() {
        assert_eq!(
            parameterize_path("/users/550e8400-e29b-41d4-a716-446655440000/profile"),
            "/users/{id}/profile"
        );
    }

    #[test]
    fn param_numeric_id() {
        assert_eq!(
            parameterize_path("/patients/12345/records"),
            "/patients/{id}/records"
        );
    }

    #[test]
    fn param_hex_id() {
        assert_eq!(
            parameterize_path("/transactions/a1b2c3d4e5f6a7b8"),
            "/transactions/{id}"
        );
    }

    #[test]
    fn param_stripe_id() {
        assert_eq!(
            parameterize_path("/v1/charges/ch_abc123def456ghi"),
            "/v1/charges/{id}"
        );
    }

    #[test]
    fn param_email_in_path() {
        assert_eq!(
            parameterize_path("/users/john.doe@email.com/profile"),
            "/users/{id}/profile"
        );
    }

    #[test]
    fn param_api_version_preserved() {
        assert_eq!(parameterize_path("/v1/charges"), "/v1/charges");
    }

    #[test]
    fn param_static_segments_preserved() {
        assert_eq!(
            parameterize_path("/services/data/v58.0/sobjects/Contact"),
            "/services/data/v58.0/sobjects/Contact"
        );
    }

    #[test]
    fn param_mixed_static_and_dynamic() {
        assert_eq!(
            parameterize_path(
                "/v2/orgs/12345/agents/550e8400-e29b-41d4-a716-446655440000/policies"
            ),
            "/v2/orgs/{id}/agents/{id}/policies"
        );
    }

    #[test]
    fn param_root_path_unchanged() {
        assert_eq!(parameterize_path("/"), "/");
    }

    #[test]
    fn param_short_segments_preserved() {
        assert_eq!(parameterize_path("/v1/api/test"), "/v1/api/test");
    }

    #[test]
    fn param_long_token() {
        assert_eq!(
            parameterize_path("/auth/eyJhbGciOiJIUzI1NiJ9/refresh"),
            "/auth/{id}/refresh"
        );
    }

    #[test]
    fn param_ssn_like_numeric() {
        assert_eq!(
            parameterize_path("/patients/123456789/records"),
            "/patients/{id}/records"
        );
    }

    // ---------------------------------------------------------------
    // PII safety: field allowlist
    // ---------------------------------------------------------------
    //
    // This test enforces the PII safety contract on TelemetryEventInput.
    // If you add a field, you MUST:
    //   1. Classify it as PII-safe (add a `/// PII: SAFE` doc comment).
    //   2. Add it to ALLOWED_FIELD_NAMES below.
    //   3. If it's customer-controlled free text, parameterize client-side
    //      or omit for OTLP.
    //
    // This test uses serde to introspect the struct's serialized field names.
    // Adding a field without updating the allowlist causes a CI failure.

    /// Every field that TelemetryEventInput is allowed to have, with its PII
    /// classification. A field NOT on this list cannot be serialized to JSON
    /// (and therefore cannot reach Aurora) without failing this test.
    const ALLOWED_FIELD_NAMES: &[&str] = &[
        "request_id",           // SAFE: UUID generated by Checkrd
        "agent_id",             // SAFE: UUID from Checkrd registry
        "instance_id",          // SAFE: Ed25519 key hash derivative
        "timestamp",            // SAFE: ISO 8601
        "url_host",             // SAFE: public domain name
        "url_path",             // SAFE: parameterized template (SDK) or "/" (OTLP)
        "method",               // SAFE: constrained enum
        "status_code",          // SAFE: integer
        "latency_ms",           // SAFE: integer
        "policy_result",        // SAFE: constrained enum
        "deny_reason",          // SAFE: rule name only, no body values
        "trace_id",             // SAFE: 32 hex chars
        "span_id",              // SAFE: 16 hex chars
        "parent_span_id",       // SAFE: 16 hex chars
        "span_name",            // SAFE: "{METHOD} {host}", not customer text
        "span_kind",            // SAFE: constrained enum
        "span_status_code",     // SAFE: constrained enum
        "gen_ai_system",        // SAFE: provider name
        "gen_ai_model",         // SAFE: model identifier
        "gen_ai_input_tokens",  // SAFE: integer
        "gen_ai_output_tokens", // SAFE: integer
        // B1 additions:
        "matched_rule",      // SAFE: rule name from user-authored YAML, no body values
        "matched_rule_kind", // SAFE: constrained enum (allow/deny/rate_limit/kill_switch/default)
        "policy_mode",       // SAFE: constrained enum (enforce/dry_run)
        // evaluation_path uses skip_serializing_if = "Vec::is_empty" so it
        // won't appear in JSON when empty. Allowlist test populates it, so it
        // shows up when the event has evaluation_path set.
        "evaluation_path", // SAFE: stage/rule/result/detail — rule names only, no body values
    ];

    #[test]
    fn pii_field_allowlist() {
        // Serialize a fully-populated event and check that every key in the
        // JSON output is on the allowlist. This catches fields added to the
        // struct without updating the PII classification.
        let event = valid_event_input();
        let json = serde_json::to_value(&event).unwrap();
        let fields: Vec<&str> = json
            .as_object()
            .unwrap()
            .keys()
            .map(|k| k.as_str())
            .collect();

        for field in &fields {
            assert!(
                ALLOWED_FIELD_NAMES.contains(field),
                "TelemetryEventInput has field '{field}' that is NOT on the PII allowlist. \
                 Before adding a new field:\n\
                 1. Add a `/// PII: SAFE` doc comment explaining why it's safe.\n\
                 2. Add it to ALLOWED_FIELD_NAMES in this test.\n\
                 3. If it contains customer-controlled text, parameterize client-side \
                    or omit for OTLP."
            );
        }

        // Also verify the allowlist hasn't drifted — every allowed field
        // should actually exist on the struct.
        for allowed in ALLOWED_FIELD_NAMES {
            assert!(
                fields.contains(allowed),
                "ALLOWED_FIELD_NAMES contains '{allowed}' but TelemetryEventInput \
                 no longer has this field. Remove it from the allowlist."
            );
        }
    }

    #[test]
    fn pii_banned_fields_absent() {
        // Explicitly verify that known-dangerous field names are NOT present.
        // This is a defense-in-depth against someone re-adding a dropped field.
        let event = valid_event_input();
        let json = serde_json::to_value(&event).unwrap();
        let obj = json.as_object().unwrap();

        const BANNED: &[&str] = &[
            "body",
            "body_hash",
            "request_body",
            "response_body",
            "prompt",
            "completion",
            "span_status_message",
            "headers",
            "authorization",
            "cookie",
            "api_key",
            "password",
            "ssn",
            "email",
        ];
        for banned in BANNED {
            assert!(
                !obj.contains_key(*banned),
                "TelemetryEventInput contains banned field '{banned}'. \
                 This field carries PII risk and must NOT be stored."
            );
        }
    }

    // B1: validation tests for new evaluation metadata fields

    #[test]
    fn rejects_matched_rule_over_128_bytes() {
        let mut ev = valid_event_input();
        ev.matched_rule = Some("a".repeat(129));
        assert!(validate_telemetry_event(&ev).is_err());
    }

    #[test]
    fn accepts_matched_rule_at_128_bytes() {
        let mut ev = valid_event_input();
        ev.matched_rule = Some("a".repeat(128));
        assert!(validate_telemetry_event(&ev).is_ok());
    }

    #[test]
    fn rejects_invalid_matched_rule_kind() {
        let mut ev = valid_event_input();
        ev.matched_rule_kind = Some("garbage".into());
        assert!(validate_telemetry_event(&ev).is_err());
    }

    #[test]
    fn rejects_evaluation_path_over_32_steps() {
        let mut ev = valid_event_input();
        ev.evaluation_path = (0..33)
            .map(|i| EvaluationStep {
                stage: format!("s{i}"),
                rule: None,
                result: "pass".into(),
                detail: None,
            })
            .collect();
        assert!(validate_telemetry_event(&ev).is_err());
    }

    #[test]
    fn accepts_well_formed_evaluation_metadata() {
        let mut ev = valid_event_input();
        ev.matched_rule = Some("block-external-api".into());
        ev.matched_rule_kind = Some("deny".into());
        ev.policy_mode = Some("enforce".into());
        ev.evaluation_path = vec![
            EvaluationStep {
                stage: "kill_switch".into(),
                rule: None,
                result: "pass".into(),
                detail: None,
            },
            EvaluationStep {
                stage: "deny_rules".into(),
                rule: Some("block-external-api".into()),
                result: "matched".into(),
                detail: None,
            },
        ];
        assert!(validate_telemetry_event(&ev).is_ok());
    }
}
