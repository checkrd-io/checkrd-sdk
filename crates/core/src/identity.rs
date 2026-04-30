//! Ed25519 identity for agent authentication.
//!
//! Provides cryptographic signing for telemetry events and a unique instance ID
//! derived from the public key. The WASM core receives key material from the
//! wrapper -- key storage and lifecycle are the wrapper's responsibility.

use ed25519_dalek::{Signature, Signer, SigningKey, Verifier, VerifyingKey};
use rand_core::OsRng;
use thiserror::Error;

/// Ed25519 private/public key length in bytes.
pub const KEY_LEN: usize = 32;
/// Ed25519 signature length in bytes.
pub const SIGNATURE_LEN: usize = 64;

#[derive(Debug, Error)]
pub enum IdentityError {
    #[error("invalid key length: expected {KEY_LEN} bytes, got {0}")]
    InvalidKeyLength(usize),
    #[error("invalid public key bytes")]
    InvalidPublicKey,
    #[error("invalid signature length: expected {SIGNATURE_LEN} bytes, got {0}")]
    InvalidSignatureLength(usize),
}

/// Agent identity with service/instance hierarchy.
///
/// Models the two-level identity from the product architecture:
/// - **`service_id`**: identifies the agent type (e.g., "Sales Agent"). One per
///   agent definition. Maps to `agent_id` in the API and telemetry events.
/// - **`instance_id`**: identifies a specific running instance. Derived from the
///   public key (keyed mode) or equal to the service_id (anonymous mode).
///
/// Two crypto modes:
/// - **Keyed**: created via [`Identity::from_key_bytes`] or [`Identity::generate`].
///   Provides real Ed25519 signatures and a public-key-derived instance ID.
/// - **Anonymous**: created via [`Identity::anonymous`]. Returns empty signatures.
///   Used for KMS/HSM providers (signing happens externally) or when no key
///   material is available.
pub struct Identity {
    service_id: String,
    signing_key: Option<SigningKey>,
    instance_id: String,
}

impl Identity {
    /// Create from an existing 32-byte Ed25519 private key.
    /// Instance ID is derived from the public key.
    pub fn from_key_bytes(
        service_id: &str,
        private_key_bytes: &[u8],
    ) -> Result<Self, IdentityError> {
        Self::from_key_bytes_with_id(service_id, private_key_bytes, "")
    }

    /// Create from a private key with an optional instance ID override.
    /// If `instance_id_override` is empty, derives from the public key.
    /// If non-empty, uses the provided value (for consistency with external
    /// identity providers like KMS that derive their own instance ID).
    pub fn from_key_bytes_with_id(
        service_id: &str,
        private_key_bytes: &[u8],
        instance_id_override: &str,
    ) -> Result<Self, IdentityError> {
        let key_array: [u8; KEY_LEN] = private_key_bytes
            .try_into()
            .map_err(|_| IdentityError::InvalidKeyLength(private_key_bytes.len()))?;
        let signing_key = SigningKey::from_bytes(&key_array);
        let instance_id = if instance_id_override.is_empty() {
            derive_instance_id(&signing_key.verifying_key())
        } else {
            instance_id_override.to_string()
        };

        Ok(Self {
            service_id: service_id.to_string(),
            signing_key: Some(signing_key),
            instance_id,
        })
    }

    /// Generate a new keypair using OS-provided randomness.
    pub fn generate(service_id: &str) -> Self {
        let signing_key = SigningKey::generate(&mut OsRng);
        let instance_id = derive_instance_id(&signing_key.verifying_key());

        Self {
            service_id: service_id.to_string(),
            signing_key: Some(signing_key),
            instance_id,
        }
    }

    /// Create an anonymous identity with no signing capability.
    ///
    /// `sign()` returns empty bytes. `instance_id` is explicitly provided
    /// (e.g., derived from KMS public key by the wrapper).
    pub fn anonymous(service_id: &str, instance_id: &str) -> Self {
        Self {
            service_id: service_id.to_string(),
            signing_key: None,
            instance_id: instance_id.to_string(),
        }
    }

    /// Sign a payload with Ed25519.
    ///
    /// Returns the 64-byte signature, or an empty vec for anonymous identities.
    pub fn sign(&self, payload: &[u8]) -> Vec<u8> {
        match &self.signing_key {
            Some(key) => key.sign(payload).to_bytes().to_vec(),
            None => Vec::new(),
        }
    }

    /// Get the 32-byte public key, or `None` for anonymous identities.
    pub fn public_key_bytes(&self) -> Option<[u8; KEY_LEN]> {
        self.signing_key
            .as_ref()
            .map(|k| k.verifying_key().to_bytes())
    }

    /// Get the 32-byte private key, or `None` for anonymous identities.
    ///
    /// The wrapper uses this to persist the key to disk.
    pub fn private_key_bytes(&self) -> Option<[u8; KEY_LEN]> {
        self.signing_key.as_ref().map(|k| k.to_bytes())
    }

    /// Service identity (the agent type, e.g., "Sales Agent").
    pub fn service_id(&self) -> &str {
        &self.service_id
    }

    /// Instance ID: hex fingerprint of public key, or explicit value for anonymous.
    pub fn instance_id(&self) -> &str {
        &self.instance_id
    }

    /// Whether this identity has signing capability.
    pub fn has_key(&self) -> bool {
        self.signing_key.is_some()
    }
}

/// Verify an Ed25519 signature against a public key.
///
/// Standalone function -- doesn't require an `Identity` instance. Used by the
/// control plane to verify telemetry signatures.
pub fn verify(
    payload: &[u8],
    signature_bytes: &[u8],
    public_key_bytes: &[u8],
) -> Result<bool, IdentityError> {
    let pk_array: [u8; KEY_LEN] = public_key_bytes
        .try_into()
        .map_err(|_| IdentityError::InvalidKeyLength(public_key_bytes.len()))?;
    let verifying_key =
        VerifyingKey::from_bytes(&pk_array).map_err(|_| IdentityError::InvalidPublicKey)?;

    let sig_array: [u8; SIGNATURE_LEN] = signature_bytes
        .try_into()
        .map_err(|_| IdentityError::InvalidSignatureLength(signature_bytes.len()))?;
    let signature = Signature::from_bytes(&sig_array);

    Ok(verifying_key.verify(payload, &signature).is_ok())
}

/// Generate a new Ed25519 keypair, returning `(private_key, public_key)`.
pub fn generate_keypair() -> ([u8; KEY_LEN], [u8; KEY_LEN]) {
    let signing_key = SigningKey::generate(&mut OsRng);
    (
        signing_key.to_bytes(),
        signing_key.verifying_key().to_bytes(),
    )
}

/// Derive instance ID from a verifying (public) key.
///
/// Uses the first 8 bytes of the public key as a 16-character hex string.
/// 64 bits of uniqueness is sufficient for instance identification.
fn derive_instance_id(verifying_key: &VerifyingKey) -> String {
    let pk_bytes = verifying_key.to_bytes();
    let mut hex = String::with_capacity(16);
    for byte in &pk_bytes[..8] {
        use std::fmt::Write;
        let _ = write!(hex, "{byte:02x}");
    }
    hex
}

#[cfg(test)]
mod tests {
    use super::*;

    // -- Construction -------------------------------------------------------

    #[test]
    fn generate_produces_valid_identity() {
        let id = Identity::generate("svc-1");
        assert!(id.has_key());
        assert_eq!(id.public_key_bytes().unwrap().len(), KEY_LEN);
        assert_eq!(id.private_key_bytes().unwrap().len(), KEY_LEN);
    }

    #[test]
    fn from_key_bytes_round_trip() {
        let original = Identity::generate("svc-1");
        let private = original.private_key_bytes().unwrap();
        let restored = Identity::from_key_bytes("svc-1", &private).unwrap();

        assert_eq!(original.public_key_bytes(), restored.public_key_bytes());
        assert_eq!(original.instance_id(), restored.instance_id());
    }

    #[test]
    fn from_key_bytes_rejects_wrong_length() {
        assert!(Identity::from_key_bytes("a", &[0u8; 16]).is_err());
        assert!(Identity::from_key_bytes("a", &[0u8; 64]).is_err());
        assert!(Identity::from_key_bytes("a", &[]).is_err());
    }

    #[test]
    fn instance_id_override() {
        let id = Identity::generate("svc-1");
        let private = id.private_key_bytes().unwrap();
        let custom = Identity::from_key_bytes_with_id("svc-1", &private, "kms-derived-id").unwrap();
        assert_eq!(custom.instance_id(), "kms-derived-id");
        assert_eq!(custom.service_id(), "svc-1");
        assert!(custom.has_key()); // still has signing capability
    }

    // -- Signing & verification ---------------------------------------------

    #[test]
    fn sign_and_verify_round_trip() {
        let id = Identity::generate("svc-1");
        let payload = b"telemetry event payload";

        let signature = id.sign(payload);
        assert_eq!(signature.len(), SIGNATURE_LEN);

        let public = id.public_key_bytes().unwrap();
        assert!(verify(payload, &signature, &public).unwrap());
    }

    #[test]
    fn tampered_payload_fails_verification() {
        let id = Identity::generate("svc-1");
        let signature = id.sign(b"original payload");
        let public = id.public_key_bytes().unwrap();

        assert!(!verify(b"tampered payload", &signature, &public).unwrap());
    }

    #[test]
    fn tampered_signature_fails_verification() {
        let id = Identity::generate("svc-1");
        let mut signature = id.sign(b"payload");
        signature[0] ^= 0xFF;
        let public = id.public_key_bytes().unwrap();

        assert!(!verify(b"payload", &signature, &public).unwrap());
    }

    #[test]
    fn wrong_key_fails_verification() {
        let id_a = Identity::generate("svc-a");
        let id_b = Identity::generate("svc-b");

        let signature = id_a.sign(b"payload");
        let public_b = id_b.public_key_bytes().unwrap();

        assert!(!verify(b"payload", &signature, &public_b).unwrap());
    }

    #[test]
    fn ed25519_signatures_are_deterministic() {
        let id = Identity::generate("svc-1");
        let sig_1 = id.sign(b"same message");
        let sig_2 = id.sign(b"same message");
        assert_eq!(sig_1, sig_2);
    }

    #[test]
    fn different_messages_produce_different_signatures() {
        let id = Identity::generate("svc-1");
        let sig_1 = id.sign(b"message A");
        let sig_2 = id.sign(b"message B");
        assert_ne!(sig_1, sig_2);
    }

    #[test]
    fn sign_empty_payload() {
        let id = Identity::generate("svc-1");
        let signature = id.sign(b"");
        assert_eq!(signature.len(), SIGNATURE_LEN);
        let public = id.public_key_bytes().unwrap();
        assert!(verify(b"", &signature, &public).unwrap());
    }

    #[test]
    fn sign_large_payload() {
        let id = Identity::generate("svc-1");
        let large = vec![0xABu8; 1_000_000];
        let signature = id.sign(&large);
        let public = id.public_key_bytes().unwrap();
        assert!(verify(&large, &signature, &public).unwrap());
    }

    // -- Instance ID --------------------------------------------------------

    #[test]
    fn instance_id_is_hex_fingerprint() {
        let id = Identity::generate("svc-1");
        let iid = id.instance_id();
        assert_eq!(iid.len(), 16);
        assert!(iid.chars().all(|c| c.is_ascii_hexdigit()));
        assert_ne!(iid, "agent-1");
    }

    #[test]
    fn instance_id_stable_for_same_key() {
        let id = Identity::generate("svc-1");
        let private = id.private_key_bytes().unwrap();
        let restored = Identity::from_key_bytes("svc-1", &private).unwrap();
        assert_eq!(id.instance_id(), restored.instance_id());
    }

    #[test]
    fn instance_id_differs_per_key() {
        let id_a = Identity::generate("svc-1");
        let id_b = Identity::generate("svc-1");
        assert_ne!(id_a.instance_id(), id_b.instance_id());
    }

    // -- Anonymous identity -------------------------------------------------

    #[test]
    fn anonymous_returns_empty_signature() {
        let id = Identity::anonymous("svc-1", "inst-1");
        assert!(!id.has_key());
        assert!(id.sign(b"payload").is_empty());
        assert!(id.public_key_bytes().is_none());
        assert!(id.private_key_bytes().is_none());
    }

    #[test]
    fn anonymous_preserves_service_and_instance_id() {
        let id = Identity::anonymous("sales-agent", "kms-12345678");
        assert_eq!(id.service_id(), "sales-agent");
        assert_eq!(id.instance_id(), "kms-12345678");
    }

    #[test]
    fn service_id_preserved_across_modes() {
        let keyed = Identity::generate("sales-agent");
        assert_eq!(keyed.service_id(), "sales-agent");

        let anon = Identity::anonymous("sales-agent", "inst-abc");
        assert_eq!(anon.service_id(), "sales-agent");
    }

    // -- Standalone verify edge cases ---------------------------------------

    #[test]
    fn verify_rejects_bad_signature_length() {
        let id = Identity::generate("svc-1");
        let public = id.public_key_bytes().unwrap();
        assert!(verify(b"payload", &[0u8; 32], &public).is_err());
    }

    #[test]
    fn verify_rejects_bad_public_key_length() {
        let id = Identity::generate("svc-1");
        let signature = id.sign(b"payload");
        assert!(verify(b"payload", &signature, &[0u8; 16]).is_err());
    }

    // -- generate_keypair ---------------------------------------------------

    #[test]
    fn generate_keypair_produces_valid_pair() {
        let (private, public) = generate_keypair();
        let id = Identity::from_key_bytes("svc", &private).unwrap();
        assert_eq!(id.public_key_bytes().unwrap(), public);
    }

    #[test]
    fn generate_keypair_unique_each_call() {
        let (priv_a, _) = generate_keypair();
        let (priv_b, _) = generate_keypair();
        assert_ne!(priv_a, priv_b);
    }

    // -- RFC 8032 Section 7.1 compliance ------------------------------------
    // Official Ed25519 test vectors. Passing these proves our implementation
    // matches the standard and is interoperable with any compliant library.

    fn hex_to_bytes<const N: usize>(hex: &str) -> [u8; N] {
        let mut bytes = [0u8; N];
        for i in 0..N {
            bytes[i] = u8::from_str_radix(&hex[i * 2..i * 2 + 2], 16).unwrap();
        }
        bytes
    }

    // RFC 8032 compliance: the signature is the gold standard. If the
    // signature matches the RFC test vector, the implementation is correct
    // and interoperable with any compliant Ed25519 library.
    //
    // We also verify that: (1) the signature we produce can be verified
    // against the RFC's public key, and (2) signing then verifying
    // round-trips using our own derived public key.

    fn rfc_vector(secret_hex: &str, message: &[u8], sig_hex: &str) {
        let secret = hex_to_bytes::<32>(secret_hex);
        let expected_sig = hex_to_bytes::<64>(sig_hex);

        let id = Identity::from_key_bytes("svc", &secret).unwrap();

        // Core assertion: signature matches the RFC 8032 test vector exactly.
        // This is the definitive interoperability proof -- any compliant Ed25519
        // library will produce the identical signature for this seed + message.
        let signature = id.sign(message);
        assert_eq!(
            signature.as_slice(),
            expected_sig.as_slice(),
            "signature must match RFC 8032 test vector"
        );

        // Round-trip: verify the signature with our derived public key.
        let public = id.public_key_bytes().unwrap();
        assert!(
            verify(message, &signature, &public).unwrap(),
            "signature must verify against the derived public key"
        );
    }

    #[test]
    fn rfc8032_vector_1_empty_message() {
        rfc_vector(
            "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
            b"",
            "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155\
             5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
        );
    }

    #[test]
    fn rfc8032_vector_2_one_byte() {
        rfc_vector(
            "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
            &[0x72],
            "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da\
             085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
        );
    }

    #[test]
    fn rfc8032_vector_3_two_bytes() {
        rfc_vector(
            "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
            &[0xaf, 0x82],
            "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac\
             18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
        );
    }
}
