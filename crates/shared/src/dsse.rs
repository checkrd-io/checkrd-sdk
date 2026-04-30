//! Dead Simple Signing Envelope (DSSE) support for telemetry batches.
//!
//! Implements the Pre-Authentication Encoding (PAE) from the DSSE specification:
//! <https://github.com/secure-systems-lab/dsse/blob/master/protocol.md>
//!
//! Why DSSE:
//! - The signer signs exact bytes; the verifier verifies exact bytes. There is no
//!   JSON canonicalization step that could differ between Rust and Python.
//! - Length-prefixed encoding eliminates ambiguity at the boundaries of the
//!   payload type and the payload itself.
//! - Used in production by sigstore, in-toto, and SLSA. Battle-tested.
//!
//! For Checkrd, the SDK signs a DSSE envelope around the canonical telemetry batch
//! JSON, the ingestion service verifies it, and the envelope rides through SQS to
//! the writer where it can be re-verified before being persisted to Aurora. This
//! gives end-to-end non-repudiation: an auditor can prove a batch came from a
//! specific agent by re-running the verification against the registered public key.
//!
//! This module has no I/O dependencies and compiles to `wasm32-wasip1`.

use serde::{Deserialize, Serialize};

/// IANA-style payload type identifier for Checkrd telemetry batches.
///
/// Bound into the signed bytes via PAE so a signature on a batch cannot be
/// confused with a signature on any other Checkrd payload type. Future payload
/// types (policy bundles, audit events) get their own constants.
pub const TELEMETRY_BATCH_PAYLOAD_TYPE: &str = "application/vnd.checkrd.telemetry-batch+json";

/// IANA-style payload type identifier for Checkrd policy bundles.
///
/// Distinct from `TELEMETRY_BATCH_PAYLOAD_TYPE` so the PAE prefix bytes
/// differ — this makes a captured telemetry signature impossible to replay
/// as a policy signature, even if the underlying YAML/JSON bytes happened
/// to collide. The `vnd.*` form follows RFC 6838 §3.2 for vendor-specific
/// media types.
pub const POLICY_BUNDLE_PAYLOAD_TYPE: &str = "application/vnd.checkrd.policy-bundle+yaml";

/// DSSE envelope as it appears on the wire and in storage.
///
/// Stored inside `TelemetryBatchMessage.dsse_envelope` so the writer can
/// re-verify before insert. The `payload` field is the base64 of the exact
/// canonical batch bytes that were signed; this is what the verifier feeds
/// back into [`pae`] to reconstruct the signed input.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct DsseEnvelope {
    #[serde(rename = "payloadType")]
    pub payload_type: String,
    /// Base64 (standard, padded) encoding of the canonical payload bytes.
    pub payload: String,
    pub signatures: Vec<DsseSignature>,
}

/// Single signature inside a DSSE envelope.
///
/// `keyid` is OPTIONAL per the DSSE envelope spec
/// (<https://github.com/secure-systems-lab/dsse/blob/master/envelope.md#parsing-rules>):
/// "The following fields are OPTIONAL and MAY be unset: `signature.keyid`.
/// An unset field MUST be treated the same as set-but-empty."
///
/// We use `#[serde(default)]` so missing keyid deserializes to `""`. The
/// verifier then treats empty keyid as "try all trusted keys" rather than
/// rejecting outright — matching the spec's note that keyid is "an
/// unauthenticated hint... it MUST NOT be used for security decisions; it
/// may only be used to narrow the selection of possible keys to try."
///
/// `sig` is REQUIRED and contains the base64-encoded signature bytes. Per
/// the spec, "Either standard or URL-safe encoding is allowed."
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct DsseSignature {
    /// Optional keyid hint per DSSE envelope spec. Empty string means
    /// "verifier should try all trusted keys."
    #[serde(default)]
    pub keyid: String,
    /// Base64-encoded signature bytes. Verifiers MUST accept either standard
    /// or URL-safe base64 per the DSSE envelope spec.
    pub sig: String,
}

/// Pre-Authentication Encoding per the DSSE specification.
///
/// Returns the byte string `"DSSEv1 " <len(type)> " " <type> " " <len(payload)> " " <payload>`
/// where lengths are ASCII-encoded base-10 integers. This is what gets signed
/// and what the verifier reconstructs to check the signature.
///
/// The length prefixes are what makes DSSE bulletproof against canonicalization
/// bugs: there is exactly one PAE for any given (payload_type, payload) pair,
/// and there is no parsing involved on either side.
///
/// # Example
///
/// ```
/// use checkrd_shared::dsse::pae;
/// let encoded = pae("text/plain", b"hello");
/// assert_eq!(encoded, b"DSSEv1 10 text/plain 5 hello");
/// ```
pub fn pae(payload_type: &str, payload: &[u8]) -> Vec<u8> {
    let type_len_str = payload_type.len().to_string();
    let payload_len_str = payload.len().to_string();
    // Capacity: "DSSEv1 " (7) + type_len digits + " " (1) + type + " " (1)
    //           + payload_len digits + " " (1) + payload
    let mut out = Vec::with_capacity(
        10 + type_len_str.len() + payload_type.len() + payload_len_str.len() + payload.len(),
    );
    out.extend_from_slice(b"DSSEv1 ");
    out.extend_from_slice(type_len_str.as_bytes());
    out.push(b' ');
    out.extend_from_slice(payload_type.as_bytes());
    out.push(b' ');
    out.extend_from_slice(payload_len_str.as_bytes());
    out.push(b' ');
    out.extend_from_slice(payload);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    // ----- PAE format -----------------------------------------------------

    #[test]
    fn pae_matches_dsse_spec_example() {
        // From the DSSE spec README:
        // PAE("hello", "world") = "DSSEv1 5 hello 5 world"
        let encoded = pae("hello", b"world");
        assert_eq!(encoded, b"DSSEv1 5 hello 5 world");
    }

    #[test]
    fn pae_matches_dsse_protocol_md_canonical_example() {
        // The canonical example from the DSSE protocol spec at
        // https://github.com/secure-systems-lab/dsse/blob/master/protocol.md
        //
        // Inputs (per the spec text):
        //   PAYLOAD_TYPE    = "http://example.com/HelloWorld"  (29 bytes)
        //   SERIALIZED_BODY = "hello world"                    (11 bytes)
        //
        // Expected PAE output (per the spec text):
        //   "DSSEv1 29 http://example.com/HelloWorld 11 hello world"
        //
        // This is the spec's authoritative worked example. If our PAE
        // produces these exact bytes, our implementation is interoperable
        // with sigstore, in-toto, and any other DSSE consumer.
        let encoded = pae("http://example.com/HelloWorld", b"hello world");
        assert_eq!(
            encoded,
            b"DSSEv1 29 http://example.com/HelloWorld 11 hello world"
        );
        // Also assert the byte lengths match the spec's length annotations.
        assert_eq!("http://example.com/HelloWorld".len(), 29);
        assert_eq!(b"hello world".len(), 11);
    }

    // ----- Domain separation: telemetry vs policy payload types ---------

    #[test]
    fn payload_type_binding_prevents_cross_type_replay() {
        // The single most important test for the policy signing feature.
        // Proves that signing the SAME payload bytes under TELEMETRY_BATCH_PAYLOAD_TYPE
        // produces a DIFFERENT PAE than signing them under POLICY_BUNDLE_PAYLOAD_TYPE.
        //
        // Without this property, an attacker who captures a valid telemetry
        // signature could try to install a malicious policy by claiming the
        // same bytes are a policy. The PAE length-prefix and type-binding
        // construction makes this attack impossible by spec.
        let same_payload = b"agent: sales-agent\ndefault: deny\n";

        let telemetry_pae = pae(TELEMETRY_BATCH_PAYLOAD_TYPE, same_payload);
        let policy_pae = pae(POLICY_BUNDLE_PAYLOAD_TYPE, same_payload);

        assert_ne!(
            telemetry_pae, policy_pae,
            "PAE must differ when payload type differs (domain separation)"
        );

        // The first divergence is in the type-length field. Both the
        // type byte length and the type bytes themselves are part of
        // the PAE prefix, so the prefixes diverge before any payload
        // bytes are even appended.
        let expected_telemetry_len = TELEMETRY_BATCH_PAYLOAD_TYPE.len();
        let expected_policy_len = POLICY_BUNDLE_PAYLOAD_TYPE.len();
        assert_eq!(expected_telemetry_len, 44);
        assert_eq!(expected_policy_len, 42);
        assert!(telemetry_pae.starts_with(format!("DSSEv1 {expected_telemetry_len} ").as_bytes()));
        assert!(policy_pae.starts_with(format!("DSSEv1 {expected_policy_len} ").as_bytes()));
    }

    #[test]
    fn payload_type_constants_are_distinct_and_well_formed() {
        // Both follow RFC 6838 vnd.* tree convention. Distinctness is what
        // gives us domain separation; the format check is a sanity gate
        // against accidental edits.
        assert_ne!(TELEMETRY_BATCH_PAYLOAD_TYPE, POLICY_BUNDLE_PAYLOAD_TYPE);
        for ty in &[TELEMETRY_BATCH_PAYLOAD_TYPE, POLICY_BUNDLE_PAYLOAD_TYPE] {
            assert!(ty.starts_with("application/vnd.checkrd."), "type: {ty}");
            assert!(ty.contains('+'), "missing structured suffix: {ty}");
            assert!(ty.is_ascii(), "media types must be ASCII: {ty}");
        }
    }

    #[test]
    fn dsse_envelope_payload_is_base64_encoded_per_spec() {
        // Per the DSSE protocol spec, the envelope's `payload` field is the
        // base64 encoding of the serialized body. The spec example uses:
        //   "payload": "aGVsbG8gd29ybGQ="  (= base64("hello world"))
        //   "payloadType": "http://example.com/HelloWorld"
        use base64::engine::general_purpose::STANDARD as B64;
        use base64::Engine;

        let body = b"hello world";
        let envelope = DsseEnvelope {
            payload_type: "http://example.com/HelloWorld".to_string(),
            payload: B64.encode(body),
            signatures: vec![DsseSignature {
                keyid: "test-key".to_string(),
                sig: "AAAA".to_string(),
            }],
        };

        // Serialize and confirm the spec-mandated field name and base64 form.
        let json = serde_json::to_value(&envelope).unwrap();
        assert_eq!(json["payload"], "aGVsbG8gd29ybGQ=");
        assert_eq!(json["payloadType"], "http://example.com/HelloWorld");
    }

    #[test]
    fn pae_handles_empty_payload() {
        let encoded = pae("text/plain", b"");
        assert_eq!(encoded, b"DSSEv1 10 text/plain 0 ");
    }

    #[test]
    fn pae_handles_empty_type() {
        // Degenerate but well-defined: empty type, length zero.
        let encoded = pae("", b"data");
        assert_eq!(encoded, b"DSSEv1 0  4 data");
    }

    #[test]
    fn pae_uses_byte_length_not_char_length() {
        // Multi-byte UTF-8: "héllo" is 6 bytes (h, é = 0xC3 0xA9, l, l, o), not 5 chars.
        let encoded = pae("text", "héllo".as_bytes());
        assert_eq!(encoded, b"DSSEv1 4 text 6 h\xc3\xa9llo");
    }

    #[test]
    fn pae_handles_binary_payload() {
        // Binary bytes including nulls — must round-trip exactly.
        let payload = vec![0u8, 1, 2, 0xff, 0, 0xfe];
        let encoded = pae("application/octet-stream", &payload);
        // Prefix: "DSSEv1 24 application/octet-stream 6 "
        let prefix = b"DSSEv1 24 application/octet-stream 6 ";
        assert_eq!(&encoded[..prefix.len()], prefix);
        assert_eq!(&encoded[prefix.len()..], payload.as_slice());
    }

    #[test]
    fn pae_telemetry_batch_payload_type_is_used() {
        let body = br#"{"events":[]}"#;
        let encoded = pae(TELEMETRY_BATCH_PAYLOAD_TYPE, body);
        let expected_prefix = format!(
            "DSSEv1 {} {} {} ",
            TELEMETRY_BATCH_PAYLOAD_TYPE.len(),
            TELEMETRY_BATCH_PAYLOAD_TYPE,
            body.len(),
        );
        assert!(encoded.starts_with(expected_prefix.as_bytes()));
        assert!(encoded.ends_with(body));
    }

    #[test]
    fn pae_distinct_inputs_produce_distinct_outputs() {
        // Domain separation: same payload bytes with different types must
        // never collide. This is the entire point of binding payload_type.
        let a = pae("type/a", b"same");
        let b = pae("type/b", b"same");
        assert_ne!(a, b);
    }

    #[test]
    fn pae_length_prefix_prevents_concatenation_collision() {
        // (type="ab", payload="cd") vs (type="abc", payload="d") would collide
        // without length prefixes. With them, they differ.
        let a = pae("ab", b"cd");
        let b = pae("abc", b"d");
        assert_ne!(a, b);
        assert_eq!(a, b"DSSEv1 2 ab 2 cd");
        assert_eq!(b, b"DSSEv1 3 abc 1 d");
    }

    #[test]
    fn pae_large_payload() {
        // 1MB payload — sanity check capacity hint and correctness at scale.
        let payload = vec![0xABu8; 1_000_000];
        let encoded = pae("application/octet-stream", &payload);
        assert_eq!(
            encoded.len(),
            "DSSEv1 24 application/octet-stream 1000000 ".len() + payload.len()
        );
        assert!(encoded.starts_with(b"DSSEv1 24 application/octet-stream 1000000 "));
    }

    // ----- DSSE envelope serialization -----------------------------------

    #[test]
    fn envelope_round_trips_through_serde_json() {
        let env = DsseEnvelope {
            payload_type: TELEMETRY_BATCH_PAYLOAD_TYPE.to_string(),
            payload: "eyJldmVudHMiOltdfQ==".to_string(),
            signatures: vec![DsseSignature {
                keyid: "a1b2c3d4e5f6a7b8".to_string(),
                sig: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA".to_string(),
            }],
        };
        let json = serde_json::to_string(&env).unwrap();
        // Confirm the spec-mandated camelCase rename.
        assert!(
            json.contains("\"payloadType\""),
            "must serialize as camelCase per DSSE spec: {json}"
        );
        let parsed: DsseEnvelope = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed, env);
    }

    #[test]
    fn envelope_supports_multiple_signatures() {
        let env = DsseEnvelope {
            payload_type: TELEMETRY_BATCH_PAYLOAD_TYPE.to_string(),
            payload: "eyJ9".to_string(),
            signatures: vec![
                DsseSignature {
                    keyid: "key1".to_string(),
                    sig: "AAAA".to_string(),
                },
                DsseSignature {
                    keyid: "key2".to_string(),
                    sig: "BBBB".to_string(),
                },
            ],
        };
        let json = serde_json::to_string(&env).unwrap();
        let parsed: DsseEnvelope = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed.signatures.len(), 2);
    }
}
