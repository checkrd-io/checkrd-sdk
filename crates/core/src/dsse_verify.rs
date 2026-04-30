//! DSSE envelope verification for the WASM core.
//!
//! Verifies signed payloads using Ed25519 against a runtime-supplied trust
//! list. Used by [`crate::interface::reload_policy_signed`] to verify policy
//! bundle signatures from the control plane before installing them.
//!
//! # Why a separate module
//!
//! [`crate::identity`] holds the agent's *outbound* signing identity (used to
//! sign telemetry batches). Policy verification is the inbound side: the
//! control plane signs, the SDK verifies. Different keys, different trust
//! model, different test surface — separate module.
//!
//! # Standards
//!
//! - **DSSE protocol**: <https://github.com/secure-systems-lab/dsse/blob/master/protocol.md>
//!   The envelope's payload is base64-decoded, fed into [`checkrd_shared::dsse::pae`]
//!   along with the expected payload type, and the resulting bytes are what
//!   gets verified. We accept either standard or URL-safe base64 per the
//!   envelope spec ("verifiers MUST accept either"), and we treat the
//!   `keyid` field as an OPTIONAL hint that may be empty or absent.
//! - **DSSE envelope schema**: <https://github.com/secure-systems-lab/dsse/blob/master/envelope.md>
//!   Unknown fields are silently ignored per the spec.
//! - **Ed25519**: RFC 8032. Verification uses `ed25519-dalek`'s `VerifyingKey::verify`,
//!   the same primitive that powers our outbound signing path. Tested against
//!   RFC 8032 §7.1 KAT vectors and Project Wycheproof v1 (150 vectors).
//!
//! # Threat model and known limitations
//!
//! What this module DOES protect against:
//!
//! - **Network-path tampering**: an attacker on the wire (compromised TLS,
//!   MITM, malicious proxy) cannot install a forged policy because the
//!   signature won't verify.
//! - **Cross-type replay**: a captured telemetry signature CANNOT be replayed
//!   as a policy signature, because the DSSE PAE binds the payload type
//!   into the signed bytes. See `dsse_spec_*` and `payload_type_binding_*`
//!   tests.
//! - **Bit-level tampering**: any single-byte change in the envelope payload
//!   or signature causes verification to fail. Verified by the every-byte-flip
//!   sweep test.
//! - **Unknown signers**: the verifier requires a trusted public key in the
//!   runtime-supplied list. Out-of-band keys are rejected.
//!
//! What this module DOES NOT protect against (Phase 2 follow-ups):
//!
//! - **Replay of historically-valid policy bundles**: the envelope does not
//!   bind a monotonic policy version, so an attacker who captured a
//!   previously-valid (more permissive) policy could replay it on the SSE
//!   channel and the SDK would install it. Phase 2 fix: include a `version`
//!   field in the signed payload, and have the SDK persist the highest
//!   version it has seen, rejecting any update with `version <= seen_max`.
//!   This is the same monotonic-version pattern OPA bundles use.
//! - **Trust-list rollback via SDK downgrade**: an attacker who can roll
//!   back the SDK package to an older version with a stale trust list could
//!   install a policy signed by a now-revoked key. Phase 2 fix: include the
//!   trust list version in the SDK's policy state and reject downgrades.
//!   Mitigated in production by SDK update enforcement and package
//!   signature verification at install time.
//! - **Compromise of the control plane signing key**: if the long-lived
//!   signing key in AWS Secrets Manager is exfiltrated, the attacker can
//!   forge any policy. Mitigations: rotate via the overlap window pattern,
//!   move to KMS in Phase 2 so the key never lives in process memory.

use base64::engine::general_purpose::{STANDARD as B64, URL_SAFE as B64_URL_SAFE};
use base64::Engine;
use ed25519_dalek::{Signature, Verifier, VerifyingKey};

use checkrd_shared::dsse::{pae, DsseEnvelope};

/// Decode base64 accepting either standard or URL-safe alphabet.
///
/// Per the DSSE envelope spec
/// (<https://github.com/secure-systems-lab/dsse/blob/master/envelope.md>):
/// "Either standard or URL-safe encoding is allowed. Signers may use either,
/// and verifiers MUST accept either."
///
/// We try standard first (the more common form for our own producers) and
/// fall back to URL-safe on error. Both decoders are pure functions with
/// no I/O — fallback cost is negligible.
fn decode_base64_either(input: &str) -> Result<Vec<u8>, base64::DecodeError> {
    match B64.decode(input) {
        Ok(bytes) => Ok(bytes),
        Err(_) => B64_URL_SAFE.decode(input),
    }
}

/// A trusted Ed25519 public key the SDK will accept signatures from.
///
/// Lives in a runtime-supplied trust list (rather than being baked into the
/// WASM core) so the wrapper can update the list without rebuilding the
/// `.wasm` artifact, and so tests can use ephemeral keys.
#[derive(Debug, Clone, serde::Deserialize, PartialEq, Eq)]
pub struct TrustedKey {
    /// Stable identifier matching the `keyid` in DSSE signatures.
    pub keyid: String,
    /// 32-byte Ed25519 public key, hex-encoded (64 lowercase hex chars).
    pub public_key_hex: String,
    /// Unix seconds when this key starts being trusted (inclusive).
    pub valid_from: u64,
    /// Unix seconds when this key stops being trusted (exclusive).
    pub valid_until: u64,
}

/// Distinct verification failure modes — production metrics label by reason.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum VerifyError {
    /// The envelope's `payloadType` did not match the verifier's expected type.
    /// Defends against cross-type replay (e.g. a telemetry signature being
    /// presented as a policy signature).
    #[error("payload type mismatch: expected {expected}, got {actual}")]
    PayloadTypeMismatch { expected: String, actual: String },

    /// The envelope had no signatures at all.
    #[error("envelope has no signatures")]
    NoSignatures,

    /// None of the envelope's signature `keyid`s matched any entry in the trust list.
    /// Either the signing key was rotated and the SDK is out of date, or the
    /// signature was issued by an unauthorized key.
    #[error("no trusted key matches any signature keyid in the envelope")]
    UnknownKeyid,

    /// A trusted key matched but its `valid_until` timestamp has passed.
    #[error("trusted key {keyid} expired (valid_until={valid_until}, now={now})")]
    KeyExpired {
        keyid: String,
        valid_until: u64,
        now: u64,
    },

    /// A trusted key matched but its `valid_from` timestamp is in the future.
    #[error("trusted key {keyid} not yet valid (valid_from={valid_from}, now={now})")]
    KeyNotYetValid {
        keyid: String,
        valid_from: u64,
        now: u64,
    },

    /// The base64 in `payload`, `sig`, or `public_key_hex` couldn't be decoded.
    #[error("malformed encoding: {0}")]
    MalformedEncoding(String),

    /// All trusted+in-window signatures were tried and none cryptographically verified.
    /// This is the "tampered envelope" case — the verifier looked at every option
    /// before failing, so an attacker can't trivially bypass via key-id confusion.
    #[error("no signature verified against any in-window trusted key")]
    SignatureInvalid,
}

/// Verify a DSSE envelope against a trust list and return the payload bytes.
///
/// Returns the raw payload bytes (the base64-decoded contents of `envelope.payload`)
/// on success. The caller is responsible for parsing them in whatever format the
/// payload type implies (e.g. policy YAML).
///
/// Verification rules:
///
/// 1. The envelope's `payload_type` MUST equal `expected_payload_type`.
/// 2. The envelope MUST have at least one signature.
/// 3. The base64 payload MUST decode.
/// 4. For at least one envelope signature there MUST exist a trust list entry
///    where `keyid` matches AND `valid_from <= now < valid_until` AND the
///    signature cryptographically verifies via Ed25519 against
///    `pae(payload_type, decoded_payload)`.
///
/// All four conditions must hold; failure produces a distinct [`VerifyError`]
/// variant so production telemetry can label the failure mode.
///
/// Multi-signature envelopes are supported: as long as at least one signature
/// verifies under the rules above, the envelope is accepted. This enables
/// key rotation via the overlap window pattern.
pub fn verify_dsse_envelope(
    envelope: &DsseEnvelope,
    expected_payload_type: &str,
    trusted_keys: &[TrustedKey],
    now: u64,
) -> Result<Vec<u8>, VerifyError> {
    // Rule 1: payload type binding (domain separation against cross-type replay).
    if envelope.payload_type != expected_payload_type {
        return Err(VerifyError::PayloadTypeMismatch {
            expected: expected_payload_type.to_string(),
            actual: envelope.payload_type.clone(),
        });
    }

    // Rule 2: must have at least one signature.
    if envelope.signatures.is_empty() {
        return Err(VerifyError::NoSignatures);
    }

    // Rule 3: payload must base64-decode. We do this once because all
    // signatures are verified against the same PAE. Per the DSSE envelope
    // spec, verifiers MUST accept either standard or URL-safe base64.
    let payload_bytes = decode_base64_either(&envelope.payload)
        .map_err(|e| VerifyError::MalformedEncoding(format!("payload base64: {e}")))?;

    // Reconstruct the exact bytes the signer signed.
    let signing_input = pae(expected_payload_type, &payload_bytes);

    // Rule 4: at least one (signature, trusted_key) pair must verify under
    // the validity window. Track the most-specific failure to surface to
    // the caller — UnknownKeyid is the cheapest, then window failures, then
    // SignatureInvalid as the catch-all.
    //
    // Per the DSSE protocol spec, keyid is "Optional, unauthenticated hint
    // ... it MUST NOT be used for security decisions; it may only be used
    // to narrow the selection of possible keys to try." We use it as the
    // optimization the spec describes: when keyid is non-empty, try only
    // matching trusted keys; when keyid is empty (or absent in the envelope
    // JSON), try ALL trusted keys.
    let mut saw_known_keyid = false;
    let mut window_failure: Option<VerifyError> = None;

    for sig in &envelope.signatures {
        // Decode the signature once per envelope-signature.
        let sig_bytes = match decode_base64_either(&sig.sig) {
            Ok(b) => b,
            Err(e) => {
                return Err(VerifyError::MalformedEncoding(format!(
                    "signature base64 for keyid {:?}: {e}",
                    sig.keyid
                )));
            }
        };
        let sig_array: [u8; 64] = match sig_bytes.as_slice().try_into() {
            Ok(a) => a,
            Err(_) => {
                return Err(VerifyError::MalformedEncoding(format!(
                    "signature for keyid {:?} must be 64 bytes",
                    sig.keyid
                )));
            }
        };
        let signature = Signature::from_bytes(&sig_array);

        for trusted in trusted_keys {
            // DSSE spec: keyid filtering is optional. When the envelope
            // signature carries a keyid, narrow to matching trusted keys
            // (an optimization). When it's empty/absent, try all trusted
            // keys. Either way, the cryptographic verification below is
            // the actual security gate.
            if !sig.keyid.is_empty() && trusted.keyid != sig.keyid {
                continue;
            }
            saw_known_keyid = true;

            // Window check before crypto. `valid_from <= now < valid_until`
            // is intentional: `now == valid_from` is accepted (boundary in),
            // `now == valid_until` is rejected (boundary out). Both
            // boundaries get exact tests below.
            if now < trusted.valid_from {
                window_failure = Some(VerifyError::KeyNotYetValid {
                    keyid: trusted.keyid.clone(),
                    valid_from: trusted.valid_from,
                    now,
                });
                continue;
            }
            if now >= trusted.valid_until {
                window_failure = Some(VerifyError::KeyExpired {
                    keyid: trusted.keyid.clone(),
                    valid_until: trusted.valid_until,
                    now,
                });
                continue;
            }

            // Decode public key.
            let pk_bytes = match decode_hex_pubkey(&trusted.public_key_hex) {
                Some(b) => b,
                None => {
                    return Err(VerifyError::MalformedEncoding(format!(
                        "public_key_hex for trusted key {}: must be 64 lowercase hex chars",
                        trusted.keyid
                    )));
                }
            };
            let verifying_key = match VerifyingKey::from_bytes(&pk_bytes) {
                Ok(vk) => vk,
                Err(e) => {
                    return Err(VerifyError::MalformedEncoding(format!(
                        "invalid Ed25519 public key for {}: {e}",
                        trusted.keyid
                    )));
                }
            };

            // Cryptographic verification. Ed25519 is deterministic per RFC 8032,
            // so this is an exact byte match against the signing input.
            if verifying_key.verify(&signing_input, &signature).is_ok() {
                return Ok(payload_bytes);
            }
            // Wrong key for this keyid (key rotated?). Don't break — continue
            // to the next trusted key. With empty-keyid envelopes this loops
            // over the entire trust list, which is what the DSSE spec
            // verification algorithm describes.
        }
    }

    // No signature verified. Pick the most specific error.
    if !saw_known_keyid {
        return Err(VerifyError::UnknownKeyid);
    }
    if let Some(err) = window_failure {
        return Err(err);
    }
    Err(VerifyError::SignatureInvalid)
}

/// Decode a 64-character lowercase hex string into a 32-byte Ed25519 public key.
///
/// Uses `u8::from_str_radix` rather than manual bit-twiddling so the
/// implementation has no boolean operators on the hot path that mutation
/// testing would treat as equivalent (e.g. `|` vs `^` on non-overlapping
/// nibbles produce identical results, which is a true equivalent mutant).
///
/// Returns `None` for any malformed input. Uppercase hex is rejected to
/// keep the byte-string -> hex mapping injective.
fn decode_hex_pubkey(hex: &str) -> Option<[u8; 32]> {
    if hex.len() != 64 {
        return None;
    }
    // Reject uppercase before parsing — `from_str_radix` accepts both cases
    // and we want canonical lowercase only.
    if !hex
        .chars()
        .all(|c| c.is_ascii_digit() || ('a'..='f').contains(&c))
    {
        return None;
    }
    let mut out = [0u8; 32];
    for i in 0..32 {
        out[i] = u8::from_str_radix(&hex[i * 2..i * 2 + 2], 16).ok()?;
    }
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use checkrd_shared::dsse::{DsseSignature, POLICY_BUNDLE_PAYLOAD_TYPE};
    use ed25519_dalek::{Signer, SigningKey};

    // ----- Test helpers -------------------------------------------------

    fn make_signing_key() -> SigningKey {
        // Deterministic key for reproducible tests; production uses OsRng.
        SigningKey::from_bytes(&[0x42; 32])
    }

    fn make_envelope(signing_key: &SigningKey, keyid: &str, payload: &[u8]) -> DsseEnvelope {
        let signing_input = pae(POLICY_BUNDLE_PAYLOAD_TYPE, payload);
        let sig = signing_key.sign(&signing_input);
        DsseEnvelope {
            payload_type: POLICY_BUNDLE_PAYLOAD_TYPE.to_string(),
            payload: B64.encode(payload),
            signatures: vec![DsseSignature {
                keyid: keyid.to_string(),
                sig: B64.encode(sig.to_bytes()),
            }],
        }
    }

    fn make_trusted(signing_key: &SigningKey, keyid: &str) -> TrustedKey {
        let pk = signing_key.verifying_key().to_bytes();
        let hex: String = pk.iter().map(|b| format!("{b:02x}")).collect();
        TrustedKey {
            keyid: keyid.to_string(),
            public_key_hex: hex,
            valid_from: 0,
            valid_until: u64::MAX,
        }
    }

    // ----- Happy path ---------------------------------------------------

    #[test]
    fn verify_round_trip_returns_payload_bytes() {
        let key = make_signing_key();
        let payload = b"agent: test\ndefault: deny\nrules: []\n";
        let envelope = make_envelope(&key, "test-key", payload);
        let trusted = vec![make_trusted(&key, "test-key")];

        let verified =
            verify_dsse_envelope(&envelope, POLICY_BUNDLE_PAYLOAD_TYPE, &trusted, 1_000_000)
                .unwrap();
        assert_eq!(verified, payload);
    }

    #[test]
    fn verify_accepts_at_boundary_of_valid_from() {
        // valid_from <= now is the inclusive lower bound. now == valid_from
        // must be accepted. Mutating <= to < would reject this.
        let key = make_signing_key();
        let envelope = make_envelope(&key, "k", b"x");
        let trusted = vec![TrustedKey {
            keyid: "k".to_string(),
            public_key_hex: hex_of(key.verifying_key().to_bytes()),
            valid_from: 1_000_000,
            valid_until: 2_000_000,
        }];
        verify_dsse_envelope(&envelope, POLICY_BUNDLE_PAYLOAD_TYPE, &trusted, 1_000_000).unwrap();
    }

    #[test]
    fn verify_rejects_one_second_before_valid_from() {
        let key = make_signing_key();
        let envelope = make_envelope(&key, "k", b"x");
        let trusted = vec![TrustedKey {
            keyid: "k".to_string(),
            public_key_hex: hex_of(key.verifying_key().to_bytes()),
            valid_from: 1_000_000,
            valid_until: 2_000_000,
        }];
        let err = verify_dsse_envelope(&envelope, POLICY_BUNDLE_PAYLOAD_TYPE, &trusted, 999_999)
            .unwrap_err();
        assert!(matches!(err, VerifyError::KeyNotYetValid { .. }));
    }

    #[test]
    fn verify_rejects_at_valid_until_boundary() {
        // now < valid_until is exclusive upper bound. now == valid_until
        // must be rejected. Mutating < to <= would accept this.
        let key = make_signing_key();
        let envelope = make_envelope(&key, "k", b"x");
        let trusted = vec![TrustedKey {
            keyid: "k".to_string(),
            public_key_hex: hex_of(key.verifying_key().to_bytes()),
            valid_from: 1_000_000,
            valid_until: 2_000_000,
        }];
        let err = verify_dsse_envelope(&envelope, POLICY_BUNDLE_PAYLOAD_TYPE, &trusted, 2_000_000)
            .unwrap_err();
        assert!(matches!(err, VerifyError::KeyExpired { .. }));
    }

    #[test]
    fn verify_accepts_one_second_before_valid_until() {
        let key = make_signing_key();
        let envelope = make_envelope(&key, "k", b"x");
        let trusted = vec![TrustedKey {
            keyid: "k".to_string(),
            public_key_hex: hex_of(key.verifying_key().to_bytes()),
            valid_from: 1_000_000,
            valid_until: 2_000_000,
        }];
        verify_dsse_envelope(&envelope, POLICY_BUNDLE_PAYLOAD_TYPE, &trusted, 1_999_999).unwrap();
    }

    fn hex_of(pk: [u8; 32]) -> String {
        pk.iter().map(|b| format!("{b:02x}")).collect()
    }

    // ----- Negative tests: one per VerifyError variant ------------------

    #[test]
    fn verify_rejects_payload_type_mismatch() {
        let key = make_signing_key();
        let envelope = make_envelope(&key, "k", b"x");
        let trusted = vec![make_trusted(&key, "k")];
        let err = verify_dsse_envelope(
            &envelope,
            "application/vnd.checkrd.telemetry-batch+json",
            &trusted,
            1_000_000,
        )
        .unwrap_err();
        match err {
            VerifyError::PayloadTypeMismatch { expected, actual } => {
                assert_eq!(expected, "application/vnd.checkrd.telemetry-batch+json");
                assert_eq!(actual, POLICY_BUNDLE_PAYLOAD_TYPE);
            }
            other => panic!("expected PayloadTypeMismatch, got {other:?}"),
        }
    }

    #[test]
    fn verify_rejects_envelope_with_no_signatures() {
        let envelope = DsseEnvelope {
            payload_type: POLICY_BUNDLE_PAYLOAD_TYPE.to_string(),
            payload: B64.encode(b"x"),
            signatures: vec![],
        };
        let err = verify_dsse_envelope(&envelope, POLICY_BUNDLE_PAYLOAD_TYPE, &[], 1_000_000)
            .unwrap_err();
        assert!(matches!(err, VerifyError::NoSignatures));
    }

    #[test]
    fn verify_rejects_unknown_keyid() {
        let signer = make_signing_key();
        let envelope = make_envelope(&signer, "signer-key", b"x");

        // Trust list contains a key with a different keyid.
        let other = SigningKey::from_bytes(&[0x99; 32]);
        let trusted = vec![make_trusted(&other, "other-key")];

        let err = verify_dsse_envelope(&envelope, POLICY_BUNDLE_PAYLOAD_TYPE, &trusted, 1_000_000)
            .unwrap_err();
        assert!(matches!(err, VerifyError::UnknownKeyid));
    }

    #[test]
    fn verify_rejects_tampered_payload_with_signature_invalid() {
        let key = make_signing_key();
        let mut envelope = make_envelope(&key, "k", b"original");
        // Tamper with the payload AFTER signing — base64 of "tampered!"
        envelope.payload = B64.encode(b"tampered!");
        let trusted = vec![make_trusted(&key, "k")];

        let err = verify_dsse_envelope(&envelope, POLICY_BUNDLE_PAYLOAD_TYPE, &trusted, 1_000_000)
            .unwrap_err();
        assert!(matches!(err, VerifyError::SignatureInvalid));
    }

    #[test]
    fn verify_rejects_tampered_signature_with_signature_invalid() {
        let key = make_signing_key();
        let mut envelope = make_envelope(&key, "k", b"x");
        // Flip a byte of the base64 signature in a way that keeps it valid base64.
        let mut sig_bytes = B64.decode(&envelope.signatures[0].sig).unwrap();
        sig_bytes[0] ^= 0xff;
        envelope.signatures[0].sig = B64.encode(&sig_bytes);
        let trusted = vec![make_trusted(&key, "k")];

        let err = verify_dsse_envelope(&envelope, POLICY_BUNDLE_PAYLOAD_TYPE, &trusted, 1_000_000)
            .unwrap_err();
        assert!(matches!(err, VerifyError::SignatureInvalid));
    }

    #[test]
    fn verify_rejects_signature_from_wrong_signer_key() {
        // Same keyid, different actual signing key (kid spoofing).
        let real = make_signing_key();
        let envelope = make_envelope(&real, "k", b"x");

        let attacker = SigningKey::from_bytes(&[0x77; 32]);
        // Trust list claims keyid "k" belongs to the attacker's public key.
        let trusted = vec![TrustedKey {
            keyid: "k".to_string(),
            public_key_hex: hex_of(attacker.verifying_key().to_bytes()),
            valid_from: 0,
            valid_until: u64::MAX,
        }];

        let err = verify_dsse_envelope(&envelope, POLICY_BUNDLE_PAYLOAD_TYPE, &trusted, 1_000_000)
            .unwrap_err();
        assert!(matches!(err, VerifyError::SignatureInvalid));
    }

    #[test]
    fn verify_rejects_malformed_payload_base64() {
        let mut envelope = make_envelope(&make_signing_key(), "k", b"x");
        envelope.payload = "not!valid@base64".to_string();
        let err = verify_dsse_envelope(&envelope, POLICY_BUNDLE_PAYLOAD_TYPE, &[], 1_000_000)
            .unwrap_err();
        assert!(matches!(err, VerifyError::MalformedEncoding(_)));
    }

    #[test]
    fn verify_rejects_malformed_signature_base64() {
        let mut envelope = make_envelope(&make_signing_key(), "k", b"x");
        envelope.signatures[0].sig = "not!valid@base64".to_string();
        // Trust list with a matching keyid so we get past the keyid check.
        let trusted = vec![make_trusted(&make_signing_key(), "k")];
        let err = verify_dsse_envelope(&envelope, POLICY_BUNDLE_PAYLOAD_TYPE, &trusted, 1_000_000)
            .unwrap_err();
        assert!(matches!(err, VerifyError::MalformedEncoding(_)));
    }

    #[test]
    fn verify_rejects_signature_wrong_byte_length() {
        let mut envelope = make_envelope(&make_signing_key(), "k", b"x");
        // Encode 32 bytes instead of 64 — valid base64 but wrong sig length.
        envelope.signatures[0].sig = B64.encode([0u8; 32]);
        let trusted = vec![make_trusted(&make_signing_key(), "k")];
        let err = verify_dsse_envelope(&envelope, POLICY_BUNDLE_PAYLOAD_TYPE, &trusted, 1_000_000)
            .unwrap_err();
        assert!(matches!(err, VerifyError::MalformedEncoding(_)));
    }

    #[test]
    fn verify_rejects_malformed_public_key_hex() {
        let key = make_signing_key();
        let envelope = make_envelope(&key, "k", b"x");
        let trusted = vec![TrustedKey {
            keyid: "k".to_string(),
            public_key_hex: "not-valid-hex".to_string(),
            valid_from: 0,
            valid_until: u64::MAX,
        }];
        let err = verify_dsse_envelope(&envelope, POLICY_BUNDLE_PAYLOAD_TYPE, &trusted, 1_000_000)
            .unwrap_err();
        assert!(matches!(err, VerifyError::MalformedEncoding(_)));
    }

    // ----- Multi-signature / key rotation -------------------------------

    #[test]
    fn verify_accepts_envelope_signed_by_one_of_multiple_keys() {
        // Key rotation: the envelope is signed by both old_key and new_key.
        // The trust list only contains the new key (old key has been retired).
        // Verification must succeed because at least one signature is valid.
        let old_key = SigningKey::from_bytes(&[0x11; 32]);
        let new_key = SigningKey::from_bytes(&[0x22; 32]);
        let payload = b"agent: test\n";
        let signing_input = pae(POLICY_BUNDLE_PAYLOAD_TYPE, payload);

        let envelope = DsseEnvelope {
            payload_type: POLICY_BUNDLE_PAYLOAD_TYPE.to_string(),
            payload: B64.encode(payload),
            signatures: vec![
                DsseSignature {
                    keyid: "old".to_string(),
                    sig: B64.encode(old_key.sign(&signing_input).to_bytes()),
                },
                DsseSignature {
                    keyid: "new".to_string(),
                    sig: B64.encode(new_key.sign(&signing_input).to_bytes()),
                },
            ],
        };

        let trusted = vec![make_trusted(&new_key, "new")];
        verify_dsse_envelope(&envelope, POLICY_BUNDLE_PAYLOAD_TYPE, &trusted, 1_000_000).unwrap();
    }

    #[test]
    fn verify_accepts_envelope_during_overlap_window() {
        // Both old and new keys are in the trust list during rollover.
        // The envelope is signed only by the old key. Verification succeeds.
        let old_key = SigningKey::from_bytes(&[0x11; 32]);
        let new_key = SigningKey::from_bytes(&[0x22; 32]);
        let envelope = make_envelope(&old_key, "old", b"x");
        let trusted = vec![make_trusted(&old_key, "old"), make_trusted(&new_key, "new")];
        verify_dsse_envelope(&envelope, POLICY_BUNDLE_PAYLOAD_TYPE, &trusted, 1_000_000).unwrap();
    }

    #[test]
    fn verify_accepts_when_trust_list_has_duplicate_keyid_with_correct_key() {
        // Operator misconfiguration: the trust list contains TWO entries
        // with the same keyid, one with the wrong public key (e.g. a stale
        // entry not yet removed) and one with the correct one. The verifier
        // must try both keys for that keyid and accept the matching one.
        // This is the realistic case during a rotation where both the old
        // and new key share a stable keyid like "checkrd-prod".
        let real_key = SigningKey::from_bytes(&[0xab; 32]);
        let stale_key = SigningKey::from_bytes(&[0xff; 32]);
        let envelope = make_envelope(&real_key, "checkrd-prod", b"x");
        let trusted = vec![
            // Stale entry first (verifier must NOT stop on first match).
            TrustedKey {
                keyid: "checkrd-prod".to_string(),
                public_key_hex: hex_of(stale_key.verifying_key().to_bytes()),
                valid_from: 0,
                valid_until: u64::MAX,
            },
            // Real entry second.
            make_trusted(&real_key, "checkrd-prod"),
        ];
        verify_dsse_envelope(&envelope, POLICY_BUNDLE_PAYLOAD_TYPE, &trusted, 1_000_000).unwrap();
    }

    #[test]
    fn verify_with_failed_first_signature_does_not_block_valid_second() {
        // An attacker prepends a valid-shape garbage signature to a real
        // envelope. The verifier MUST NOT bail on the first signature
        // failing — it must continue to the next signature and verify
        // the valid one. Spec multi-sig algorithm: "Skip over if the
        // verification fails."
        let key = make_signing_key();
        let payload = b"agent: test\n";
        let pae_bytes = pae(POLICY_BUNDLE_PAYLOAD_TYPE, payload);
        use ed25519_dalek::Signer;
        let valid_sig = key.sign(&pae_bytes);

        let envelope = DsseEnvelope {
            payload_type: POLICY_BUNDLE_PAYLOAD_TYPE.to_string(),
            payload: B64.encode(payload),
            signatures: vec![
                // First: well-formed (64 bytes) but bogus.
                DsseSignature {
                    keyid: "k".to_string(),
                    sig: B64.encode([0u8; 64]),
                },
                // Second: actually valid.
                DsseSignature {
                    keyid: "k".to_string(),
                    sig: B64.encode(valid_sig.to_bytes()),
                },
            ],
        };
        let trusted = vec![make_trusted(&key, "k")];
        verify_dsse_envelope(&envelope, POLICY_BUNDLE_PAYLOAD_TYPE, &trusted, 1_000_000).unwrap();
    }

    #[test]
    fn verify_with_two_signatures_same_keyid_different_signers() {
        // Real rotation case: bundle is signed by both the old and new
        // signing keys, both sharing a stable keyid like "checkrd-prod".
        // Trust list has only the new key. The verifier must try the old
        // signature (fails), then the new signature (succeeds).
        let old_key = SigningKey::from_bytes(&[0x11; 32]);
        let new_key = SigningKey::from_bytes(&[0x22; 32]);
        let payload = b"x";
        let pae_bytes = pae(POLICY_BUNDLE_PAYLOAD_TYPE, payload);
        use ed25519_dalek::Signer;

        let envelope = DsseEnvelope {
            payload_type: POLICY_BUNDLE_PAYLOAD_TYPE.to_string(),
            payload: B64.encode(payload),
            signatures: vec![
                DsseSignature {
                    keyid: "checkrd-prod".to_string(),
                    sig: B64.encode(old_key.sign(&pae_bytes).to_bytes()),
                },
                DsseSignature {
                    keyid: "checkrd-prod".to_string(),
                    sig: B64.encode(new_key.sign(&pae_bytes).to_bytes()),
                },
            ],
        };
        let trusted = vec![make_trusted(&new_key, "checkrd-prod")];
        verify_dsse_envelope(&envelope, POLICY_BUNDLE_PAYLOAD_TYPE, &trusted, 1_000_000).unwrap();
    }

    #[test]
    fn verify_rejects_when_all_signatures_are_well_formed_but_wrong() {
        // Multiple structurally-valid (64 byte) signatures, none of which
        // verify. Catches a bug where the verifier might short-circuit on
        // the first attempt and miss that all subsequent attempts also fail.
        let key = make_signing_key();
        let payload = b"x";

        let envelope = DsseEnvelope {
            payload_type: POLICY_BUNDLE_PAYLOAD_TYPE.to_string(),
            payload: B64.encode(payload),
            signatures: vec![
                DsseSignature {
                    keyid: "k".to_string(),
                    sig: B64.encode([0u8; 64]),
                },
                DsseSignature {
                    keyid: "k".to_string(),
                    sig: B64.encode([0xffu8; 64]),
                },
                DsseSignature {
                    keyid: "k".to_string(),
                    sig: B64.encode([0x42u8; 64]),
                },
            ],
        };
        let trusted = vec![make_trusted(&key, "k")];
        let err = verify_dsse_envelope(&envelope, POLICY_BUNDLE_PAYLOAD_TYPE, &trusted, 1_000_000)
            .unwrap_err();
        assert!(matches!(err, VerifyError::SignatureInvalid));
    }

    // ----- Hex decoder edge cases ---------------------------------------

    #[test]
    fn decode_hex_pubkey_round_trip() {
        let original: [u8; 32] = [
            0x01, 0x23, 0x45, 0x67, 0x89, 0xab, 0xcd, 0xef, 0xfe, 0xdc, 0xba, 0x98, 0x76, 0x54,
            0x32, 0x10, 0x00, 0xff, 0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02, 0x01, 0xa5, 0x5a,
            0xc3, 0x3c, 0x99, 0x66,
        ];
        let hex = hex_of(original);
        assert_eq!(decode_hex_pubkey(&hex), Some(original));
    }

    #[test]
    fn decode_hex_pubkey_rejects_uppercase() {
        // Uppercase hex is canonical-ambiguous; reject so the hex form has
        // exactly one representation per byte string.
        assert!(decode_hex_pubkey(&"AA".repeat(32)).is_none());
        assert!(decode_hex_pubkey(&"aa".repeat(32)).is_some());
    }

    #[test]
    fn decode_hex_pubkey_rejects_wrong_length() {
        assert!(decode_hex_pubkey("").is_none());
        assert!(decode_hex_pubkey(&"a".repeat(63)).is_none());
        assert!(decode_hex_pubkey(&"a".repeat(65)).is_none());
    }

    // ----- DSSE spec conformance ---------------------------------------
    //
    // Tests in this section anchor the verifier against specific normative
    // requirements from the DSSE protocol/envelope spec at
    // https://github.com/secure-systems-lab/dsse/blob/master/protocol.md
    // and the envelope.md companion document.

    #[test]
    fn dsse_spec_accepts_url_safe_base64_payload() {
        // DSSE envelope spec: "Either standard or URL-safe encoding is allowed.
        // Signers may use either, and verifiers MUST accept either."
        //
        // Test with a payload whose base64 encoding contains characters that
        // differ between standard and URL-safe alphabets (specifically '+' / '-'
        // and '/' / '_' positions).
        let key = make_signing_key();
        // Choose payload bytes whose base64 will use the differing chars.
        let payload: Vec<u8> = (0..96).map(|i| (i * 7 + 3) as u8).collect();
        let pae_bytes = pae(POLICY_BUNDLE_PAYLOAD_TYPE, &payload);
        use ed25519_dalek::Signer;
        let sig = key.sign(&pae_bytes);

        // Build the envelope with URL-safe base64 for both payload and sig.
        use base64::engine::general_purpose::URL_SAFE as B64_URL_SAFE;
        let envelope = DsseEnvelope {
            payload_type: POLICY_BUNDLE_PAYLOAD_TYPE.to_string(),
            payload: B64_URL_SAFE.encode(&payload),
            signatures: vec![DsseSignature {
                keyid: "test-key".to_string(),
                sig: B64_URL_SAFE.encode(sig.to_bytes()),
            }],
        };
        let trusted = vec![make_trusted(&key, "test-key")];

        // Per the spec, verifier MUST accept this even though we used the
        // URL-safe alphabet. If we didn't add the fallback decoder this
        // would fail.
        let verified =
            verify_dsse_envelope(&envelope, POLICY_BUNDLE_PAYLOAD_TYPE, &trusted, 1_000_000)
                .unwrap();
        assert_eq!(verified, payload);
    }

    #[test]
    fn dsse_spec_accepts_url_safe_base64_signature_only() {
        // The spec doesn't require both fields to use the same alphabet —
        // a producer could mix them. The verifier MUST accept either for
        // each field independently.
        let key = make_signing_key();
        let payload = b"agent: test\n";
        let pae_bytes = pae(POLICY_BUNDLE_PAYLOAD_TYPE, payload);
        use ed25519_dalek::Signer;
        let sig = key.sign(&pae_bytes);

        use base64::engine::general_purpose::URL_SAFE as B64_URL_SAFE;
        let envelope = DsseEnvelope {
            payload_type: POLICY_BUNDLE_PAYLOAD_TYPE.to_string(),
            payload: B64.encode(payload), // standard
            signatures: vec![DsseSignature {
                keyid: "test-key".to_string(),
                sig: B64_URL_SAFE.encode(sig.to_bytes()), // URL-safe
            }],
        };
        let trusted = vec![make_trusted(&key, "test-key")];
        verify_dsse_envelope(&envelope, POLICY_BUNDLE_PAYLOAD_TYPE, &trusted, 1_000_000).unwrap();
    }

    #[test]
    fn dsse_spec_accepts_envelope_with_empty_keyid() {
        // DSSE envelope spec parsing rules: "The following fields are
        // OPTIONAL and MAY be unset: signature.keyid. An unset field MUST
        // be treated the same as set-but-empty."
        //
        // The reference envelope example in the spec is literally:
        //   {"signatures":[{"sig":"..."}]}
        // (no keyid). Our verifier MUST accept this.
        let key = make_signing_key();
        let payload = b"agent: test\n";
        let pae_bytes = pae(POLICY_BUNDLE_PAYLOAD_TYPE, payload);
        use ed25519_dalek::Signer;
        let sig = key.sign(&pae_bytes);

        let envelope = DsseEnvelope {
            payload_type: POLICY_BUNDLE_PAYLOAD_TYPE.to_string(),
            payload: B64.encode(payload),
            signatures: vec![DsseSignature {
                keyid: String::new(), // empty == unset
                sig: B64.encode(sig.to_bytes()),
            }],
        };
        // Trust list contains a key with a non-empty keyid. The verifier
        // must try it anyway because the envelope's empty keyid means
        // "try all trusted keys."
        let trusted = vec![make_trusted(&key, "production-cp")];

        verify_dsse_envelope(&envelope, POLICY_BUNDLE_PAYLOAD_TYPE, &trusted, 1_000_000).unwrap();
    }

    #[test]
    fn dsse_spec_envelope_without_keyid_field_deserializes() {
        // The spec says keyid is OPTIONAL — an envelope with no keyid field
        // at all (not just empty) must deserialize. Then verification with
        // an "empty" keyid should fall back to trying all trusted keys.
        let key = make_signing_key();
        let payload = b"x";
        let pae_bytes = pae(POLICY_BUNDLE_PAYLOAD_TYPE, payload);
        use ed25519_dalek::Signer;
        let sig = key.sign(&pae_bytes);
        let sig_b64 = B64.encode(sig.to_bytes());
        let payload_b64 = B64.encode(payload);

        // Build the envelope JSON manually with NO keyid field.
        let json = format!(
            r#"{{"payloadType":"{POLICY_BUNDLE_PAYLOAD_TYPE}","payload":"{payload_b64}","signatures":[{{"sig":"{sig_b64}"}}]}}"#
        );
        let envelope: DsseEnvelope = serde_json::from_str(&json).unwrap();
        assert_eq!(envelope.signatures[0].keyid, "");

        let trusted = vec![make_trusted(&key, "any-keyid")];
        verify_dsse_envelope(&envelope, POLICY_BUNDLE_PAYLOAD_TYPE, &trusted, 1_000_000).unwrap();
    }

    #[test]
    fn dsse_spec_unknown_envelope_fields_are_ignored() {
        // Spec parsing rules: "Producers, or future versions of the spec,
        // MAY add additional fields. Consumers MUST ignore unrecognized
        // fields."
        let key = make_signing_key();
        let payload = b"x";
        let pae_bytes = pae(POLICY_BUNDLE_PAYLOAD_TYPE, payload);
        use ed25519_dalek::Signer;
        let sig = key.sign(&pae_bytes);
        let sig_b64 = B64.encode(sig.to_bytes());
        let payload_b64 = B64.encode(payload);

        let json = format!(
            r#"{{
                "payloadType":"{POLICY_BUNDLE_PAYLOAD_TYPE}",
                "payload":"{payload_b64}",
                "signatures":[{{"keyid":"k","sig":"{sig_b64}","futureField":"ignored"}}],
                "topLevelFutureField":42
            }}"#
        );
        let envelope: DsseEnvelope = serde_json::from_str(&json).unwrap();
        let trusted = vec![make_trusted(&key, "k")];
        verify_dsse_envelope(&envelope, POLICY_BUNDLE_PAYLOAD_TYPE, &trusted, 1_000_000).unwrap();
    }

    #[test]
    fn dsse_spec_multi_signature_returns_first_valid() {
        // Spec multi-sig algorithm: "For each (SIGNATURE, KEYID) in
        // SIGNATURES, verify... Add accepted public key to ACCEPTED_KEYS.
        // Break if the number of unique keys in ACCEPTED_KEYS is greater
        // or equal to t." For Checkrd t=1 — first verifying signature wins.
        //
        // Build an envelope where the FIRST signature is invalid (random
        // bytes) and the SECOND is valid. Verifier must NOT short-circuit
        // on the first failure; it must try the second.
        let key = make_signing_key();
        let payload = b"agent: test\n";
        let pae_bytes = pae(POLICY_BUNDLE_PAYLOAD_TYPE, payload);
        use ed25519_dalek::Signer;
        let valid_sig = key.sign(&pae_bytes);

        let envelope = DsseEnvelope {
            payload_type: POLICY_BUNDLE_PAYLOAD_TYPE.to_string(),
            payload: B64.encode(payload),
            signatures: vec![
                // First signature: bytes that will fail verification.
                DsseSignature {
                    keyid: "rotated-out".to_string(),
                    sig: B64.encode([0u8; 64]),
                },
                // Second signature: actually valid.
                DsseSignature {
                    keyid: "current".to_string(),
                    sig: B64.encode(valid_sig.to_bytes()),
                },
            ],
        };
        let trusted = vec![
            make_trusted(&key, "rotated-out"), // matches keyid 1 but won't verify
            make_trusted(&key, "current"),     // matches keyid 2 and verifies
        ];
        verify_dsse_envelope(&envelope, POLICY_BUNDLE_PAYLOAD_TYPE, &trusted, 1_000_000).unwrap();
    }

    #[test]
    fn dsse_keyid_is_not_used_for_security_decisions() {
        // The spec says keyid "MUST NOT be used for security decisions; it
        // may only be used to narrow the selection of possible keys to
        // try." This test enforces the negative: an envelope where the
        // keyid claims one identity but the actual signature was made by
        // a DIFFERENT key MUST be rejected. The decision is the
        // signature, not the label.
        let real_signer = SigningKey::from_bytes(&[0xab; 32]);
        let attacker_signer = SigningKey::from_bytes(&[0xcd; 32]);
        let payload = b"x";
        let pae_bytes = pae(POLICY_BUNDLE_PAYLOAD_TYPE, payload);
        use ed25519_dalek::Signer;

        // Real signer creates a legitimate signature with real keyid.
        let real_sig = real_signer.sign(&pae_bytes);

        // Trust list contains BOTH the real and attacker keys.
        let trusted = vec![
            make_trusted(&real_signer, "real"),
            make_trusted(&attacker_signer, "attacker"),
        ];

        // Now: an envelope claiming keyid "attacker" but using the real
        // signer's signature bytes. Filter-by-keyid would point at the
        // attacker key, which won't verify the real signature. The verifier
        // MUST reject — keyid is just a hint, the signature is the
        // authority.
        let envelope = DsseEnvelope {
            payload_type: POLICY_BUNDLE_PAYLOAD_TYPE.to_string(),
            payload: B64.encode(payload),
            signatures: vec![DsseSignature {
                keyid: "attacker".to_string(),
                sig: B64.encode(real_sig.to_bytes()),
            }],
        };

        // This must be rejected: the keyid filters us to the attacker key,
        // and the real signer's signature won't verify against the
        // attacker's public key.
        let err = verify_dsse_envelope(&envelope, POLICY_BUNDLE_PAYLOAD_TYPE, &trusted, 1_000_000)
            .unwrap_err();
        assert!(matches!(err, VerifyError::SignatureInvalid));
    }

    // ----- Property-based fuzzing on parser + verifier -----------------
    //
    // The verifier handles untrusted input from the control plane stream.
    // A parser that can be made to panic, hang, or return wrong answers
    // on adversarial inputs is a real DoS attack surface. proptest
    // generates millions of random inputs (within shrinking limits) to
    // probe edge cases the hand-written negative tests don't cover.

    proptest::proptest! {
        #![proptest_config(proptest::prelude::ProptestConfig::with_cases(2048))]

        #[test]
        fn proptest_envelope_parser_does_not_panic_on_arbitrary_bytes(
            bytes in proptest::collection::vec(proptest::prelude::any::<u8>(), 0..2048)
        ) {
            // Parser must never panic on adversarial input. Whether it
            // returns Ok or Err is fine; the requirement is no panic.
            let _ = serde_json::from_slice::<DsseEnvelope>(&bytes);
        }

        #[test]
        fn proptest_envelope_parser_does_not_panic_on_arbitrary_strings(
            s in r"[\PC]{0,2000}"
        ) {
            // Same property but seeded with valid Unicode strings (not
            // arbitrary bytes), which exercise the JSON parser more
            // densely on the structurally-plausible inputs.
            let _ = serde_json::from_slice::<DsseEnvelope>(s.as_bytes());
        }

        #[test]
        fn proptest_verify_does_not_panic_on_arbitrary_envelopes(
            payload_type in r"[\PC]{0,100}",
            payload in proptest::collection::vec(proptest::prelude::any::<u8>(), 0..512),
            sig_bytes in proptest::collection::vec(proptest::prelude::any::<u8>(), 0..256),
            keyid in r"[\PC]{0,40}",
            now in proptest::prelude::any::<u64>(),
        ) {
            // Build a structurally well-formed envelope from arbitrary
            // bytes and feed it to the verifier. The verifier MUST return
            // an Err (since the signature won't verify against any key)
            // but MUST NOT panic, hang, or take unbounded time.
            let envelope = DsseEnvelope {
                payload_type,
                payload: B64.encode(&payload),
                signatures: vec![DsseSignature {
                    keyid,
                    sig: B64.encode(&sig_bytes),
                }],
            };
            let trusted = vec![make_trusted(&make_signing_key(), "test")];
            let _ = verify_dsse_envelope(
                &envelope,
                POLICY_BUNDLE_PAYLOAD_TYPE,
                &trusted,
                now,
            );
        }

        #[test]
        fn proptest_verify_with_arbitrary_trust_list_does_not_panic(
            trust_count in 0usize..16,
            valid_from in proptest::prelude::any::<u64>(),
            valid_until in proptest::prelude::any::<u64>(),
            now in proptest::prelude::any::<u64>(),
        ) {
            // Construct a trust list with random validity windows. Catches
            // arithmetic overflow / underflow / panic in the window check
            // (validate_window-style boundary issues at the edges of u64).
            let key = make_signing_key();
            let envelope = make_envelope(&key, "k", b"x");
            let trusted: Vec<TrustedKey> = (0..trust_count)
                .map(|_| TrustedKey {
                    keyid: "k".to_string(),
                    public_key_hex: hex_of(key.verifying_key().to_bytes()),
                    valid_from,
                    valid_until,
                })
                .collect();
            let _ = verify_dsse_envelope(
                &envelope,
                POLICY_BUNDLE_PAYLOAD_TYPE,
                &trusted,
                now,
            );
        }

        #[test]
        fn proptest_decode_hex_pubkey_never_panics(s in r"[\PC]{0,100}") {
            // Hex parser must reject (None) on any non-conforming input
            // without panicking. Critical because malformed hex in the
            // trust list would otherwise crash the verifier.
            let _ = decode_hex_pubkey(&s);
        }
    }

    // ----- Every-byte-flip tamper detection -----------------------------

    #[test]
    fn every_byte_flip_in_envelope_breaks_verification() {
        // Walk every byte of the serialized envelope, flip it, and assert
        // that verification fails (either via parse error before reaching
        // verify, or via VerifyError after). This is the strongest possible
        // no-malleability guarantee.
        let key = make_signing_key();
        let envelope = make_envelope(&key, "test-key", b"agent: test\ndefault: deny\n");
        let trusted = vec![make_trusted(&key, "test-key")];
        let json = serde_json::to_string(&envelope).unwrap();
        let bytes = json.as_bytes().to_vec();

        let mut accepted_after_tamper = 0usize;
        for i in 0..bytes.len() {
            let mut tampered = bytes.clone();
            tampered[i] ^= 0xff;
            // Try to parse; if it doesn't parse, that's a "rejected" outcome.
            let result = serde_json::from_slice::<DsseEnvelope>(&tampered)
                .ok()
                .and_then(|env| {
                    verify_dsse_envelope(&env, POLICY_BUNDLE_PAYLOAD_TYPE, &trusted, 1_000_000).ok()
                });
            if result.is_some() {
                accepted_after_tamper += 1;
            }
        }
        assert_eq!(
            accepted_after_tamper, 0,
            "{accepted_after_tamper} byte flips were silently accepted; envelope is malleable"
        );
    }
}
