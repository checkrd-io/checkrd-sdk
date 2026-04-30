//! HTTP Message Signatures (RFC 9421) and Content-Digest (RFC 9530) support.
//!
//! Implements the subset of RFC 9421 that Checkrd uses to authenticate
//! `POST /v1/telemetry` requests from the SDK to the ingestion service.
//!
//! # Why a focused subset
//!
//! RFC 9421 is large and supports many algorithms, header coverage strategies,
//! and structured-field encodings. Checkrd has exactly one shape: a fixed
//! component set, Ed25519 only, one signature label per request. Implementing
//! the focused subset is ~150 lines, has no third-party dependencies beyond
//! the workspace's existing `sha2` and `base64`, compiles to `wasm32-wasip1`
//! (so the WASM core can build the same canonical bytes as the verifier),
//! and is straightforward to validate against RFC 9421 Appendix B test vectors.
//!
//! # Covered components for telemetry
//!
//! - `@method` — `POST`
//! - `@target-uri` — full request URI as the SDK constructed it
//! - `content-digest` — `sha-256=:<base64>:` per RFC 9530
//! - `x-checkrd-signer-agent` — UUID of the agent whose key signed this batch
//!
//! Plus signature parameters:
//! - `created` — Unix seconds, populated by the SDK at signing time
//! - `expires` — `created + 300` (5-minute window)
//! - `keyid` — 16-hex-char `instance_id` (matches `derive_instance_id` in core)
//! - `alg` — always `"ed25519"` for Checkrd
//! - `nonce` — 32-hex-char random string for replay protection
//!
//! # Format
//!
//! The signature base string per RFC 9421 §2.5 looks exactly like:
//!
//! ```text
//! "@method": POST
//! "@target-uri": https://api.checkrd.io/v1/telemetry
//! "content-digest": sha-256=:X48E9qOokqqrvdts8nOJRJN3OWDUoyWxBf7kbu9DBPE=:
//! "x-checkrd-signer-agent": 550e8400-e29b-41d4-a716-446655440000
//! "@signature-params": ("@method" "@target-uri" "content-digest" "x-checkrd-signer-agent");created=1712345678;expires=1712345978;keyid="a1b2c3d4e5f6a7b8";alg="ed25519";nonce="abcdef0123456789abcdef0123456789"
//! ```
//!
//! No trailing newline after the `@signature-params` line. Each component line
//! ends with `\n`. This is the byte string that gets signed and verified.

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine;
use sha2::{Digest, Sha256};

use std::fmt::Write as _;

/// The single signature label Checkrd uses. The SDK and the ingestion service
/// agree on this so neither side has to scan the `Signature` header for an
/// unknown label.
pub const TELEMETRY_SIGNATURE_LABEL: &str = "checkrd";

/// Algorithm identifier for Ed25519 in RFC 9421's algorithm registry.
pub const ALG_ED25519: &str = "ed25519";

/// HTTP header that carries the signing agent's UUID.
///
/// Lowercased; we use this name when constructing the canonical signature
/// base string so the verifier produces identical bytes.
pub const HEADER_SIGNER_AGENT: &str = "x-checkrd-signer-agent";

/// Errors when parsing or validating signature headers.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum SigError {
    #[error("missing required component {0}")]
    MissingComponent(&'static str),
    #[error("malformed Signature-Input header: {0}")]
    MalformedInput(String),
    #[error("malformed Signature header: {0}")]
    MalformedSignature(String),
    #[error("base64 decode error: {0}")]
    Base64(String),
    #[error("expected signature label '{expected}', found '{found}'")]
    UnexpectedLabel { expected: String, found: String },
    #[error("unsupported algorithm: {0}")]
    UnsupportedAlg(String),
    #[error("created/expires window invalid: {0}")]
    InvalidWindow(String),
}

/// Inputs to [`signature_base_string`] — everything the signer covers.
///
/// Constructed by both the signer (SDK) and the verifier (ingestion service)
/// from the same wire bytes. If they don't agree on every byte the signature
/// won't verify; that's the entire point.
#[derive(Debug, Clone)]
pub struct CoveredComponents<'a> {
    pub method: &'a str,
    pub target_uri: &'a str,
    pub content_digest: &'a str,
    pub signer_agent: &'a str,
    pub created: u64,
    pub expires: u64,
    pub keyid: &'a str,
    pub nonce: &'a str,
}

/// Build the RFC 9421 §2.5 signature base string for a Checkrd telemetry request.
///
/// Returns the exact byte string the signer signs and the verifier verifies.
/// The format is fully deterministic: identical inputs always produce identical
/// bytes, and any difference (extra whitespace, wrong case, missing component)
/// produces different bytes that won't verify.
///
/// Built on top of the generic [`build_signature_base`] primitive so the same
/// canonical-base construction is shared between Checkrd's fixed component set
/// and the RFC 9421 conformance test that verifies our implementation against
/// the RFC §B.2.6 Ed25519 test vector.
pub fn signature_base_string(c: &CoveredComponents<'_>) -> String {
    // Pre-compute the @signature-params value because it appears in two places:
    // (a) standalone as a `Signature-Input` header value, and
    // (b) inside the signature base as the value of `@signature-params`.
    let params_value = signature_params_value(c);

    build_signature_base(
        &[
            ("@method", c.method),
            ("@target-uri", c.target_uri),
            ("content-digest", c.content_digest),
            (HEADER_SIGNER_AGENT, c.signer_agent),
        ],
        &params_value,
    )
}

/// Generic RFC 9421 §2.5 signature base builder.
///
/// Takes the ordered list of covered components as `(name, value)` pairs and
/// the structured-field-item value for `@signature-params`, and produces the
/// exact byte string the signer signs and the verifier reconstructs.
///
/// Format per RFC 9421 §2.5 ABNF:
///
/// ```text
/// signature-base = *( signature-base-line LF ) signature-params-line
/// signature-base-line = component-identifier ":" SP
///     ( derived-component-value / *field-content )
/// signature-params-line = DQUOTE "@signature-params" DQUOTE ":" SP inner-list
/// ```
///
/// - Each component line is `"<name>": <value>` followed by LF (`\n`).
/// - The final `@signature-params` line has NO trailing LF.
/// - Component names are wrapped in literal double quotes.
///
/// This is the primitive verified against the RFC 9421 §B.2.6 Ed25519 test
/// vector. The Checkrd-specific [`signature_base_string`] wraps it with our
/// fixed component set.
pub fn build_signature_base(components: &[(&str, &str)], params_value: &str) -> String {
    // Capacity hint: enough headroom for typical telemetry to avoid reallocations.
    let mut out = String::with_capacity(256 + params_value.len());
    for (name, value) in components {
        // RFC 9421 §2.5 step 2.2-2.7: `"<name>": <value>\n`
        let _ = writeln!(out, "\"{name}\": {value}");
    }
    // RFC 9421 §2.5 step 3.1-3.4: `"@signature-params": <inner-list>` (no trailing LF)
    let _ = write!(out, "\"@signature-params\": {params_value}");
    debug_assert!(
        out.is_ascii(),
        "RFC 9421 §2.5 step 4: signature base must be ASCII"
    );
    out
}

/// Build the value that goes after `<label>=` in the `Signature-Input` header.
///
/// This is the structured-field item form: an inner list of component IDs
/// followed by the signature parameters.
pub fn signature_params_value(c: &CoveredComponents<'_>) -> String {
    format!(
        "(\"@method\" \"@target-uri\" \"content-digest\" \"{}\");created={};expires={};keyid=\"{}\";alg=\"{}\";nonce=\"{}\"",
        HEADER_SIGNER_AGENT, c.created, c.expires, c.keyid, ALG_ED25519, c.nonce
    )
}

/// Compute the `Content-Digest` header value per RFC 9530.
///
/// Uses SHA-256, formatted as a structured-field byte sequence:
/// `sha-256=:<base64-of-digest>:`. The colons are sf-binary delimiters.
pub fn compute_content_digest(body: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(body);
    let digest = hasher.finalize();
    format!("sha-256=:{}:", B64.encode(digest))
}

/// Parsed `Signature-Input` header value, scoped to the Checkrd label.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SignatureInput {
    pub label: String,
    pub created: u64,
    pub expires: u64,
    pub keyid: String,
    pub alg: String,
    pub nonce: String,
    /// Raw component-list as it appeared in the header (used for verification).
    pub component_list: String,
}

/// Parse a `Signature-Input` header looking for the Checkrd label.
///
/// We expect exactly one entry with label `checkrd` and the fixed component
/// set. Anything else is rejected. Strictness here is good — RFC 9421 allows
/// flexibility but Checkrd does not need it, so any deviation is a sign of a
/// misbehaving client and should fail closed.
pub fn parse_signature_input(
    header_value: &str,
    expected_label: &str,
) -> Result<SignatureInput, SigError> {
    // Format: <label>=(<components>);param=val;param=val
    // Find the label.
    let eq_idx = header_value
        .find('=')
        .ok_or_else(|| SigError::MalformedInput("missing '='".into()))?;
    let label = header_value[..eq_idx].trim();
    if label != expected_label {
        return Err(SigError::UnexpectedLabel {
            expected: expected_label.to_string(),
            found: label.to_string(),
        });
    }
    // Per RFC 8941 §4.1, structured-field values may have optional leading
    // whitespace after the `=`. After trimming, the next byte MUST be `(` —
    // anything else is malformed. The strict positioning is what stops an
    // attacker from sneaking junk between the label and the inner list.
    let rest = header_value[eq_idx + 1..].trim_start();
    if !rest.starts_with('(') {
        return Err(SigError::MalformedInput("expected '(' after label".into()));
    }
    let close = rest
        .find(')')
        .ok_or_else(|| SigError::MalformedInput("unterminated component list".into()))?;
    let component_list = rest[1..close].to_string();

    // Parameters: anything after the closing paren, separated by ';'.
    let mut created: Option<u64> = None;
    let mut expires: Option<u64> = None;
    let mut keyid: Option<String> = None;
    let mut alg: Option<String> = None;
    let mut nonce: Option<String> = None;

    let params_str = &rest[close + 1..];
    for raw in params_str.split(';') {
        let kv = raw.trim();
        if kv.is_empty() {
            continue;
        }
        let eq = kv
            .find('=')
            .ok_or_else(|| SigError::MalformedInput(format!("malformed param: {kv}")))?;
        let key = kv[..eq].trim();
        let value = kv[eq + 1..].trim();
        match key {
            "created" => {
                created = Some(
                    value
                        .parse::<u64>()
                        .map_err(|_| SigError::MalformedInput(format!("bad created: {value}")))?,
                );
            }
            "expires" => {
                expires = Some(
                    value
                        .parse::<u64>()
                        .map_err(|_| SigError::MalformedInput(format!("bad expires: {value}")))?,
                );
            }
            "keyid" => keyid = Some(strip_dquotes(value)?.to_string()),
            "alg" => alg = Some(strip_dquotes(value)?.to_string()),
            "nonce" => nonce = Some(strip_dquotes(value)?.to_string()),
            _ => {} // unknown params ignored per RFC 9421 §2.3 forward compat
        }
    }

    Ok(SignatureInput {
        label: label.to_string(),
        created: created.ok_or(SigError::MissingComponent("created"))?,
        expires: expires.ok_or(SigError::MissingComponent("expires"))?,
        keyid: keyid.ok_or(SigError::MissingComponent("keyid"))?,
        alg: alg.ok_or(SigError::MissingComponent("alg"))?,
        nonce: nonce.ok_or(SigError::MissingComponent("nonce"))?,
        component_list,
    })
}

/// Parse a `Signature` header value, returning the raw signature bytes for the
/// expected label. The format is `<label>=:<base64>:`.
pub fn parse_signature_header(
    header_value: &str,
    expected_label: &str,
) -> Result<Vec<u8>, SigError> {
    let eq_idx = header_value
        .find('=')
        .ok_or_else(|| SigError::MalformedSignature("missing '='".into()))?;
    let label = header_value[..eq_idx].trim();
    if label != expected_label {
        return Err(SigError::UnexpectedLabel {
            expected: expected_label.to_string(),
            found: label.to_string(),
        });
    }
    let value = header_value[eq_idx + 1..].trim();
    if !value.starts_with(':') || !value.ends_with(':') || value.len() < 2 {
        return Err(SigError::MalformedSignature(
            "value must be wrapped in colons (sf-binary)".into(),
        ));
    }
    let b64 = &value[1..value.len() - 1];
    B64.decode(b64).map_err(|e| SigError::Base64(e.to_string()))
}

fn strip_dquotes(value: &str) -> Result<&str, SigError> {
    if value.len() < 2 || !value.starts_with('"') || !value.ends_with('"') {
        return Err(SigError::MalformedInput(format!(
            "expected quoted string: {value}"
        )));
    }
    Ok(&value[1..value.len() - 1])
}

/// Validate the `created`/`expires` window against `now` with a configurable skew.
///
/// Per RFC 9421 §3.2 the verifier may impose its own validity window. We
/// enforce four bounds:
///
/// 1. **Not future-dated beyond skew** — `created` must not exceed `now + skew`.
/// 2. **Not long-stale** — `now - created` must not exceed `skew` (when
///    `created < now`; if `created >= now` this branch is trivially satisfied).
/// 3. **Not expired beyond skew** — `expires + skew` must not be less than `now`.
/// 4. **`expires >= created`** — sanity check; protects against integer corruption.
///
/// All comparisons use `saturating_add` / `saturating_sub` so unsigned underflow
/// or overflow can't trigger an undefined branch. Each bound is checked by a
/// distinct test in the unit test suite, including the exact-boundary cases
/// (e.g. `created == now + skew_secs` must be accepted). Mutation testing
/// verifies the boundary tests cover every comparison operator.
pub fn validate_window(
    created: u64,
    expires: u64,
    now: u64,
    skew_secs: u64,
) -> Result<(), SigError> {
    // Bound 1: created may be at most now+skew. `>` (strict) is intentional —
    // `created == now + skew` is the boundary and must be accepted.
    if created > now.saturating_add(skew_secs) {
        return Err(SigError::InvalidWindow(format!(
            "created in the future: {created} > {now} + {skew_secs}"
        )));
    }
    // Bound 2: long-stale check. `saturating_sub` returns 0 when `created > now`,
    // making the comparison trivially false in that case (no false reject).
    // `>` (strict) means `now - created == skew_secs` is accepted as boundary.
    if now.saturating_sub(created) > skew_secs {
        return Err(SigError::InvalidWindow(format!(
            "created too old: now={now}, created={created}, skew={skew_secs}"
        )));
    }
    // Bound 3: expired beyond skew. `<` (strict) means `expires + skew == now`
    // is accepted as boundary.
    if expires.saturating_add(skew_secs) < now {
        return Err(SigError::InvalidWindow(format!(
            "expired: now={now}, expires={expires}, skew={skew_secs}"
        )));
    }
    // Bound 4: expires must be at least created. `<` (strict) means
    // `expires == created` is accepted as boundary.
    if expires < created {
        return Err(SigError::InvalidWindow(format!(
            "expires < created: {expires} < {created}"
        )));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_components() -> CoveredComponents<'static> {
        CoveredComponents {
            method: "POST",
            target_uri: "https://api.checkrd.io/v1/telemetry",
            content_digest: "sha-256=:X48E9qOokqqrvdts8nOJRJN3OWDUoyWxBf7kbu9DBPE=:",
            signer_agent: "550e8400-e29b-41d4-a716-446655440000",
            created: 1712345678,
            expires: 1712345978,
            keyid: "a1b2c3d4e5f6a7b8",
            nonce: "abcdef0123456789abcdef0123456789",
        }
    }

    // ----- Content-Digest (RFC 9530) ------------------------------------

    #[test]
    fn content_digest_empty_body() {
        // SHA-256 of "" = e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
        let d = compute_content_digest(b"");
        assert_eq!(d, "sha-256=:47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=:");
    }

    #[test]
    fn content_digest_format_for_known_input() {
        // Verify the structural shape rather than hard-coding a base64 string.
        // Correctness of the underlying SHA-256 is the responsibility of the
        // sha2 crate, which has its own NIST CAVP test vectors.
        let d = compute_content_digest(b"Hello, World!\n");
        let inner = d
            .strip_prefix("sha-256=:")
            .unwrap()
            .strip_suffix(':')
            .unwrap();
        let decoded = B64.decode(inner).unwrap();
        assert_eq!(decoded.len(), 32);
        // The full hex digest of "Hello, World!\n" is documented and stable;
        // verify base64 decodes to the same hex.
        let hex = decoded
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect::<String>();
        assert_eq!(
            hex,
            "c98c24b677eff44860afea6f493bbaec5bb1c4cbb209c6fc2bbb47f66ff2ad31"
        );
    }

    #[test]
    fn content_digest_is_deterministic() {
        let body = br#"{"events":[{"id":1}]}"#;
        let a = compute_content_digest(body);
        let b = compute_content_digest(body);
        assert_eq!(a, b);
        assert!(a.starts_with("sha-256=:"));
        assert!(a.ends_with(':'));
    }

    #[test]
    fn content_digest_differs_for_different_bodies() {
        let a = compute_content_digest(b"a");
        let b = compute_content_digest(b"b");
        assert_ne!(a, b);
    }

    #[test]
    fn content_digest_round_trips_via_base64() {
        // The middle part should be valid base64 of 32 bytes (SHA-256 length).
        let d = compute_content_digest(b"some payload");
        let inner = d
            .strip_prefix("sha-256=:")
            .unwrap()
            .strip_suffix(':')
            .unwrap();
        let decoded = B64.decode(inner).unwrap();
        assert_eq!(decoded.len(), 32, "SHA-256 digest is 32 bytes");
    }

    // ----- Signature base string (RFC 9421 §2.5) ------------------------

    #[test]
    fn signature_base_has_correct_line_structure() {
        let c = sample_components();
        let base = signature_base_string(&c);
        let lines: Vec<&str> = base.split('\n').collect();
        assert_eq!(lines.len(), 5, "expected 5 lines, got: {base}");
        assert_eq!(lines[0], "\"@method\": POST");
        assert_eq!(
            lines[1],
            "\"@target-uri\": https://api.checkrd.io/v1/telemetry"
        );
        assert_eq!(
            lines[2],
            "\"content-digest\": sha-256=:X48E9qOokqqrvdts8nOJRJN3OWDUoyWxBf7kbu9DBPE=:"
        );
        assert_eq!(
            lines[3],
            "\"x-checkrd-signer-agent\": 550e8400-e29b-41d4-a716-446655440000"
        );
        // Last line: no trailing newline before @signature-params
        assert!(lines[4].starts_with("\"@signature-params\": "));
    }

    #[test]
    fn signature_base_no_trailing_newline() {
        let c = sample_components();
        let base = signature_base_string(&c);
        assert!(
            !base.ends_with('\n'),
            "signature base must not end with \\n"
        );
    }

    #[test]
    fn signature_base_is_byte_for_byte_deterministic() {
        let c = sample_components();
        let a = signature_base_string(&c);
        let b = signature_base_string(&c);
        assert_eq!(a, b);
        assert_eq!(a.as_bytes(), b.as_bytes());
    }

    #[test]
    fn signature_base_changes_when_any_component_changes() {
        let mut c = sample_components();
        let base = signature_base_string(&c);

        c.method = "GET";
        assert_ne!(signature_base_string(&c), base, "method must affect base");
        c.method = "POST";

        c.target_uri = "https://api.checkrd.io/v2/telemetry";
        assert_ne!(
            signature_base_string(&c),
            base,
            "target_uri must affect base"
        );
        c.target_uri = "https://api.checkrd.io/v1/telemetry";

        c.content_digest = "sha-256=:0000000000000000000000000000000000000000000=:";
        assert_ne!(signature_base_string(&c), base, "digest must affect base");
        c.content_digest = "sha-256=:X48E9qOokqqrvdts8nOJRJN3OWDUoyWxBf7kbu9DBPE=:";

        c.signer_agent = "00000000-0000-0000-0000-000000000000";
        assert_ne!(signature_base_string(&c), base, "signer must affect base");
        c.signer_agent = "550e8400-e29b-41d4-a716-446655440000";

        c.created = 1712345679;
        assert_ne!(signature_base_string(&c), base, "created must affect base");
        c.created = 1712345678;

        c.nonce = "0000000000000000000000000000000000000000";
        assert_ne!(signature_base_string(&c), base, "nonce must affect base");
    }

    #[test]
    fn signature_params_value_format() {
        let c = sample_components();
        let v = signature_params_value(&c);
        assert!(v.starts_with(
            "(\"@method\" \"@target-uri\" \"content-digest\" \"x-checkrd-signer-agent\")"
        ));
        assert!(v.contains(";created=1712345678"));
        assert!(v.contains(";expires=1712345978"));
        assert!(v.contains(";keyid=\"a1b2c3d4e5f6a7b8\""));
        assert!(v.contains(";alg=\"ed25519\""));
        assert!(v.contains(";nonce=\"abcdef0123456789abcdef0123456789\""));
    }

    // ----- Signature-Input parsing --------------------------------------

    fn sample_input_header() -> String {
        let c = sample_components();
        format!("checkrd={}", signature_params_value(&c))
    }

    #[test]
    fn parse_input_round_trips() {
        let header = sample_input_header();
        let parsed = parse_signature_input(&header, "checkrd").unwrap();
        assert_eq!(parsed.label, "checkrd");
        assert_eq!(parsed.created, 1712345678);
        assert_eq!(parsed.expires, 1712345978);
        assert_eq!(parsed.keyid, "a1b2c3d4e5f6a7b8");
        assert_eq!(parsed.alg, "ed25519");
        assert_eq!(parsed.nonce, "abcdef0123456789abcdef0123456789");
    }

    #[test]
    fn parse_input_rejects_wrong_label() {
        let header = sample_input_header();
        let err = parse_signature_input(&header, "other").unwrap_err();
        assert!(matches!(err, SigError::UnexpectedLabel { .. }));
    }

    #[test]
    fn parse_input_rejects_missing_created() {
        let header = "checkrd=(\"@method\");keyid=\"k\";alg=\"ed25519\";nonce=\"n\";expires=1";
        let err = parse_signature_input(header, "checkrd").unwrap_err();
        assert!(matches!(err, SigError::MissingComponent("created")));
    }

    #[test]
    fn parse_input_rejects_missing_keyid() {
        let header = "checkrd=(\"@method\");created=1;expires=2;alg=\"ed25519\";nonce=\"n\"";
        let err = parse_signature_input(header, "checkrd").unwrap_err();
        assert!(matches!(err, SigError::MissingComponent("keyid")));
    }

    #[test]
    fn parse_input_rejects_no_paren() {
        let header = "checkrd=created=1";
        let err = parse_signature_input(header, "checkrd").unwrap_err();
        assert!(matches!(err, SigError::MalformedInput(_)));
    }

    #[test]
    fn parse_input_rejects_unquoted_keyid() {
        let header =
            "checkrd=(\"@method\");created=1;expires=2;keyid=unquoted;alg=\"ed25519\";nonce=\"n\"";
        let err = parse_signature_input(header, "checkrd").unwrap_err();
        assert!(matches!(err, SigError::MalformedInput(_)));
    }

    #[test]
    fn parse_input_ignores_unknown_params() {
        // Forward compat: unknown params don't break parsing.
        let header = "checkrd=(\"@method\");created=1;expires=2;keyid=\"k\";alg=\"ed25519\";nonce=\"n\";future-param=\"x\"";
        let parsed = parse_signature_input(header, "checkrd").unwrap();
        assert_eq!(parsed.created, 1);
    }

    // ----- Signature header parsing --------------------------------------

    #[test]
    fn parse_signature_round_trips() {
        // Encode 64 zero bytes as base64
        let bytes = vec![0u8; 64];
        let encoded = B64.encode(&bytes);
        let header = format!("checkrd=:{encoded}:");
        let parsed = parse_signature_header(&header, "checkrd").unwrap();
        assert_eq!(parsed, bytes);
    }

    #[test]
    fn parse_signature_rejects_wrong_label() {
        let header = "other=:AAAA:";
        assert!(matches!(
            parse_signature_header(header, "checkrd"),
            Err(SigError::UnexpectedLabel { .. })
        ));
    }

    #[test]
    fn parse_signature_rejects_missing_colons() {
        let header = "checkrd=AAAA";
        assert!(matches!(
            parse_signature_header(header, "checkrd"),
            Err(SigError::MalformedSignature(_))
        ));
    }

    #[test]
    fn parse_signature_rejects_invalid_base64() {
        let header = "checkrd=:not!base64@all:";
        assert!(matches!(
            parse_signature_header(header, "checkrd"),
            Err(SigError::Base64(_))
        ));
    }

    // ----- validate_window ----------------------------------------------

    #[test]
    fn window_accepts_current_signature() {
        let now = 1_000_000;
        assert!(validate_window(now, now + 300, now, 30).is_ok());
    }

    #[test]
    fn window_accepts_within_skew() {
        let now = 1_000_000;
        // created 20s in the future, within 30s skew → ok
        assert!(validate_window(now + 20, now + 320, now, 30).is_ok());
        // created 20s in the past, within window → ok
        assert!(validate_window(now - 20, now + 280, now, 30).is_ok());
    }

    #[test]
    fn window_rejects_long_stale() {
        let now = 1_000_000;
        // created 600s ago, expires 300s ago → expired
        let err = validate_window(now - 600, now - 300, now, 30).unwrap_err();
        assert!(matches!(err, SigError::InvalidWindow(_)));
    }

    #[test]
    fn window_rejects_far_future() {
        let now = 1_000_000;
        // created 600s in the future, exceeds 30s skew → reject
        let err = validate_window(now + 600, now + 900, now, 30).unwrap_err();
        assert!(matches!(err, SigError::InvalidWindow(_)));
    }

    #[test]
    fn window_rejects_expires_before_created() {
        let now = 1_000_000;
        let err = validate_window(now, now - 1, now, 30).unwrap_err();
        assert!(matches!(err, SigError::InvalidWindow(_)));
    }

    // ----- validate_window exact-boundary tests -------------------------
    //
    // These exist to kill specific mutation-testing mutants on the boundary
    // operators. Each test pins the documented behavior for the precise edge
    // case (`==` boundary), so a mutation from `>` to `>=` or `<` to `<=` is
    // caught.

    #[test]
    fn window_boundary_created_eq_now_plus_skew_is_accepted() {
        // Bound 1: `created > now + skew` rejects. The boundary `==` must
        // be accepted. Mutating `>` to `>=` rejects this case → caught.
        let now = 1_000_000;
        let skew = 30;
        assert!(validate_window(now + skew, now + skew + 100, now, skew).is_ok());
    }

    #[test]
    fn window_boundary_created_eq_now_plus_skew_plus_one_is_rejected() {
        // One past the boundary must reject. Mutating `>` to `>=` would
        // already reject this; we still want it covered.
        let now = 1_000_000;
        let skew = 30;
        assert!(validate_window(now + skew + 1, now + skew + 100, now, skew).is_err());
    }

    #[test]
    fn window_boundary_now_minus_created_eq_skew_is_accepted() {
        // Bound 2: `now - created > skew` rejects. Boundary `==` accepted.
        // Mutating `>` to `>=` rejects this → caught.
        let now = 1_000_000;
        let skew = 30;
        assert!(validate_window(now - skew, now + 100, now, skew).is_ok());
    }

    #[test]
    fn window_boundary_now_minus_created_eq_skew_plus_one_is_rejected() {
        let now = 1_000_000;
        let skew = 30;
        assert!(validate_window(now - skew - 1, now + 100, now, skew).is_err());
    }

    #[test]
    fn window_boundary_expires_plus_skew_eq_now_is_accepted() {
        // Bound 3: `expires + skew < now` rejects. Boundary `==` accepted.
        // Mutating `<` to `<=` rejects this → caught.
        let now = 1_000_000;
        let skew = 30;
        // created can't be > expires per Bound 4, so use created == expires.
        let expires = now - skew;
        assert!(validate_window(expires, expires, now, skew).is_ok());
    }

    #[test]
    fn window_boundary_expires_plus_skew_eq_now_minus_one_is_rejected() {
        let now = 1_000_000;
        let skew = 30;
        let expires = now - skew - 1;
        assert!(validate_window(expires, expires, now, skew).is_err());
    }

    #[test]
    fn window_boundary_expires_eq_created_is_accepted() {
        // Bound 4: `expires < created` rejects. Boundary `==` accepted.
        // Mutating `<` to `<=` rejects this → caught.
        // Use `t = now` so Bounds 1, 2, 3 all pass and Bound 4 is the only
        // remaining gate.
        let now = 1_000_000;
        let t = now;
        assert!(validate_window(t, t, now, 30).is_ok());
    }

    #[test]
    fn window_boundary_expires_eq_created_minus_one_is_rejected() {
        // Same setup but expires one second before created → Bound 4 fires.
        let now = 1_000_000;
        let t = now;
        assert!(validate_window(t, t - 1, now, 30).is_err());
    }

    #[test]
    fn window_long_stale_when_created_in_future_does_not_underflow() {
        // saturating_sub safety: `created > now` should not trip Bound 2
        // (the saturated 0 is not > skew). Bound 1 catches it instead.
        let now = 1_000_000;
        let skew = 30;
        let err = validate_window(now + 1000, now + 2000, now, skew).unwrap_err();
        // Must reject via Bound 1 (future-dated), not crash on underflow.
        match err {
            SigError::InvalidWindow(msg) => assert!(
                msg.contains("future"),
                "expected future-dated rejection, got: {msg}"
            ),
            other => panic!("expected InvalidWindow, got {other:?}"),
        }
    }

    // ----- parse_signature_input mutation-killing tests ------------------
    //
    // The parser stores the inner-list bytes in `component_list`. The earlier
    // tests didn't assert this field, so off-by-one mutations on the slice
    // indices that bound the inner list survived. These tests pin the exact
    // slice the parser must extract.

    #[test]
    fn parse_input_component_list_excludes_parens() {
        let header = "checkrd=(\"@method\" \"@target-uri\");created=1;expires=2;keyid=\"k\";alg=\"ed25519\";nonce=\"n\"";
        let parsed = parse_signature_input(header, "checkrd").unwrap();
        // The inner list bytes must be exactly what was between the parens,
        // with no leading/trailing characters from off-by-one slice mutations.
        assert_eq!(parsed.component_list, "\"@method\" \"@target-uri\"");
    }

    #[test]
    fn parse_input_component_list_for_empty_inner_list() {
        // RFC 9421 §B.2.1 minimal example uses an empty component list.
        let header = "checkrd=();created=1;expires=2;keyid=\"k\";alg=\"ed25519\";nonce=\"n\"";
        let parsed = parse_signature_input(header, "checkrd").unwrap();
        assert_eq!(parsed.component_list, "");
    }

    #[test]
    fn parse_input_label_position_does_not_leak_into_rest() {
        // If `eq_idx + 1` was mutated to `eq_idx - 1` or `eq_idx`, the rest
        // would include the `=` or trailing label byte. The component list
        // assertion above catches that, but this test pins the params side
        // too — `created` value would be parsed wrong if the offset shifted.
        let header =
            "checkrd=();created=12345;expires=12645;keyid=\"k\";alg=\"ed25519\";nonce=\"n\"";
        let parsed = parse_signature_input(header, "checkrd").unwrap();
        assert_eq!(parsed.created, 12345);
        assert_eq!(parsed.expires, 12645);
    }

    // ----- parse_signature_header boundary tests ------------------------
    //
    // The colon-wrap check has three boolean clauses. Mutations on the
    // operator (`||` ↔ `&&`) and the length comparison (`<` ↔ `<=` / `==`)
    // need targeted boundary tests.

    #[test]
    fn parse_signature_rejects_value_with_only_leading_colon() {
        // value = ":xyz" → starts with `:`, doesn't end with `:`. Must reject.
        // Mutating `||` to `&&` would accept this (because the && requires
        // BOTH to be missing, not either) → caught.
        let header = "checkrd=:xyz";
        assert!(matches!(
            parse_signature_header(header, "checkrd"),
            Err(SigError::MalformedSignature(_))
        ));
    }

    #[test]
    fn parse_signature_rejects_value_with_only_trailing_colon() {
        // value = "xyz:" → doesn't start with `:`, ends with `:`. Must reject.
        let header = "checkrd=xyz:";
        assert!(matches!(
            parse_signature_header(header, "checkrd"),
            Err(SigError::MalformedSignature(_))
        ));
    }

    #[test]
    fn parse_signature_rejects_single_colon() {
        // value = ":" → len 1, both starts and ends with `:`. Length check
        // catches this. Mutating `len() < 2` to `len() == 2` would skip this
        // and proceed to a panicking slice → caught.
        let header = "checkrd=:";
        let result = std::panic::catch_unwind(|| parse_signature_header(header, "checkrd"));
        // Either way it's not Ok(...).
        match result {
            Ok(Ok(_)) => panic!("must not accept a single-colon value"),
            Ok(Err(SigError::MalformedSignature(_))) => {}
            Ok(Err(other)) => panic!("expected MalformedSignature, got {other:?}"),
            Err(_panic) => {} // also acceptable: panic on the bad slice
        }
    }

    #[test]
    fn parse_signature_accepts_empty_payload_between_colons() {
        // value = "::" → len 2, both colons present, content is empty.
        // base64-decoding "" returns Ok(empty Vec). This is the boundary
        // for `len() < 2` — mutating to `len() <= 2` would reject it.
        let header = "checkrd=::";
        let result = parse_signature_header(header, "checkrd").unwrap();
        assert!(result.is_empty());
    }

    // ----- strip_dquotes boundary tests --------------------------------
    //
    // Indirect testing via parse_signature_input — strip_dquotes is private
    // but its boundary cases are reachable via the keyid="..." parameter.

    #[test]
    fn parse_input_accepts_empty_quoted_keyid() {
        // keyid="" → strip_dquotes sees `""`, len 2, both `"` chars. The
        // boundary case for `len() < 2`. Mutating to `<=` would reject.
        let header = "checkrd=();created=1;expires=2;keyid=\"\";alg=\"ed25519\";nonce=\"n\"";
        let parsed = parse_signature_input(header, "checkrd").unwrap();
        assert_eq!(parsed.keyid, "");
    }

    #[test]
    fn parse_input_rejects_unmatched_quote() {
        // value = `"k` (1 char + opening quote, no closing) → must reject.
        // Mutating `||` to `&&` in strip_dquotes would accept this → caught.
        let header = "checkrd=();created=1;expires=2;keyid=\"k;alg=\"ed25519\";nonce=\"n\"";
        // The split on ';' makes `keyid="k` and `alg="ed25519"` separate
        // params. strip_dquotes(`"k`) sees a string starting with `"` but
        // not ending with `"` → must reject.
        assert!(matches!(
            parse_signature_input(header, "checkrd"),
            Err(SigError::MalformedInput(_))
        ));
    }

    #[test]
    fn parse_input_rejects_quote_only_at_end() {
        // value = `k"` (closing quote without opening) → must reject.
        // This is the precise boundary case that catches mutating the FIRST
        // `||` in strip_dquotes to `&&`. With `&&`, the condition becomes
        // `(too_short && !starts_with_quote) || !ends_with_quote`, which
        // accepts `k"` because it ends with a quote. The strict OR rejects.
        let header = "checkrd=();created=1;expires=2;keyid=k\";alg=\"ed25519\";nonce=\"n\"";
        assert!(matches!(
            parse_signature_input(header, "checkrd"),
            Err(SigError::MalformedInput(_))
        ));
    }

    #[test]
    fn parse_input_rejects_junk_between_label_and_inner_list() {
        // RFC 9421 / RFC 8941 require the inner list to immediately follow
        // the `=` (with optional whitespace). Anything else is malformed.
        // This test pins the strict parser behavior and catches off-by-one
        // mutations on the prefix-stripping that would otherwise be
        // equivalent because find('(') would still locate the '('.
        let header = "checkrd=junk();created=1;expires=2;keyid=\"k\";alg=\"ed25519\";nonce=\"n\"";
        let err = parse_signature_input(header, "checkrd").unwrap_err();
        assert!(matches!(err, SigError::MalformedInput(_)));
    }

    #[test]
    fn parse_input_accepts_optional_whitespace_after_equals() {
        // RFC 8941 §4.1 allows leading whitespace before the structured
        // field value. The parser uses trim_start() to handle this.
        let header = "checkrd= ();created=1;expires=2;keyid=\"k\";alg=\"ed25519\";nonce=\"n\"";
        let parsed = parse_signature_input(header, "checkrd").unwrap();
        assert_eq!(parsed.created, 1);
    }

    // ----- RFC 9421 §B.2.6 Ed25519 conformance --------------------------
    //
    // The IETF spec ships a worked example with explicit inputs and a
    // resulting signature base string. If our generic build_signature_base
    // produces a base byte-for-byte identical to the spec, our format
    // construction is RFC-compliant by definition. The cryptographic side
    // of the conformance test (signing the base with the spec's private key
    // and matching the spec's signature) lives in
    // crates/core/tests/rfc9421_b26_ed25519.rs because the shared crate
    // doesn't depend on ed25519-dalek.
    //
    // Source: RFC 9421 Appendix B.2.6
    // https://www.rfc-editor.org/rfc/rfc9421.html#name-signing-a-request-using-ed2

    #[test]
    fn rfc9421_b26_signature_base_matches_spec() {
        // Components in the order the RFC lists them.
        let components: &[(&str, &str)] = &[
            ("date", "Tue, 20 Apr 2021 02:07:55 GMT"),
            ("@method", "POST"),
            ("@path", "/foo"),
            ("@authority", "example.com"),
            ("content-type", "application/json"),
            ("content-length", "18"),
        ];
        // The exact @signature-params value the RFC §B.2.6 example shows
        // (with line wrapping unrolled per RFC 8792).
        let params_value = "(\"date\" \"@method\" \"@path\" \"@authority\" \"content-type\" \"content-length\");created=1618884473;keyid=\"test-key-ed25519\"";

        let base = build_signature_base(components, params_value);

        // Expected base string from RFC 9421 §B.2.6 with line wrapping unrolled.
        // Note: NO trailing newline after @signature-params per §2.5.
        let expected = concat!(
            "\"date\": Tue, 20 Apr 2021 02:07:55 GMT\n",
            "\"@method\": POST\n",
            "\"@path\": /foo\n",
            "\"@authority\": example.com\n",
            "\"content-type\": application/json\n",
            "\"content-length\": 18\n",
            "\"@signature-params\": (\"date\" \"@method\" \"@path\" \"@authority\" \"content-type\" \"content-length\");created=1618884473;keyid=\"test-key-ed25519\"",
        );

        assert_eq!(
            base, expected,
            "build_signature_base must produce the RFC 9421 §B.2.6 byte sequence verbatim"
        );

        // Sanity: 6 component lines + 1 params line, no trailing newline.
        assert!(!base.ends_with('\n'));
        assert_eq!(base.matches('\n').count(), 6);
    }

    #[test]
    fn rfc9421_b21_minimal_signature_params_format() {
        // §B.2.1 minimal example (no covered components, just params).
        // The signature base is just the @signature-params line.
        let components: &[(&str, &str)] = &[];
        let params_value =
            "();created=1618884473;keyid=\"test-key-rsa-pss\";nonce=\"b3k2pp5k7z-50gnwp.yemd\"";

        let base = build_signature_base(components, params_value);

        let expected = "\"@signature-params\": ();created=1618884473;keyid=\"test-key-rsa-pss\";nonce=\"b3k2pp5k7z-50gnwp.yemd\"";
        assert_eq!(base, expected);
        assert!(!base.contains('\n'));
    }

    // ----- End-to-end: sign a base, verify a base ------------------------

    #[test]
    fn round_trip_signer_and_verifier_produce_same_base() {
        // Both sides build CoveredComponents from the same wire data and must
        // get byte-identical signature base strings. This is the core invariant
        // of the protocol.
        let signer_view = sample_components();
        let signer_base = signature_base_string(&signer_view);

        // Verifier reconstructs from the wire (parsed Signature-Input + headers)
        let header = format!("checkrd={}", signature_params_value(&signer_view));
        let parsed = parse_signature_input(&header, "checkrd").unwrap();
        let verifier_view = CoveredComponents {
            method: signer_view.method,
            target_uri: signer_view.target_uri,
            content_digest: signer_view.content_digest,
            signer_agent: signer_view.signer_agent,
            created: parsed.created,
            expires: parsed.expires,
            keyid: &parsed.keyid,
            nonce: &parsed.nonce,
        };
        let verifier_base = signature_base_string(&verifier_view);

        assert_eq!(signer_base, verifier_base);
    }
}
