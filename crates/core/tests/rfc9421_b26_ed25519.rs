//! RFC 9421 Appendix B.2.6 — full cryptographic conformance.
//!
//! The IETF spec ships an end-to-end Ed25519 worked example: a request,
//! a private key, the resulting signature base, and the resulting signature
//! value. This test runs the entire pipeline:
//!
//! 1. Decode the RFC 9421 §B.1.4 Ed25519 private key from its JWK base64url
//!    representation.
//! 2. Build the §B.2.6 signature base via the shared `build_signature_base`
//!    primitive (the same function the Checkrd telemetry path uses).
//! 3. Sign the base with our `Identity::sign` (Ed25519, no prehash, per
//!    §3.3.6).
//! 4. Assert the resulting 64-byte signature, base64-encoded, equals the
//!    RFC's published signature value byte-for-byte.
//!
//! Ed25519 is deterministic (RFC 8032), so this is an exact-match test.
//! Passing it proves both:
//!   - Our signature base format is RFC 9421 conformant.
//!   - Our Ed25519 signing pipeline is RFC 9421 §3.3.6 conformant.
//!
//! Combined with the Wycheproof primitive vectors and the RFC 8032 KAT
//! tests, this completes the standards-conformance gate.
//!
//! Source: <https://www.rfc-editor.org/rfc/rfc9421.html#name-signing-a-request-using-ed2>

use base64::engine::general_purpose::{STANDARD as B64, URL_SAFE_NO_PAD as B64URL};
use base64::Engine;

use checkrd_core::identity::Identity;
use checkrd_shared::http_sig::build_signature_base;

/// RFC 9421 §B.1.4 — `test-key-ed25519` private key in JWK base64url form.
const RFC_TEST_KEY_PRIVATE_B64URL: &str = "n4Ni-HpISpVObnQMW0wOhCKROaIKqKtW_2ZYb2p9KcU";

/// RFC 9421 §B.1.4 — `test-key-ed25519` public key in JWK base64url form.
const RFC_TEST_KEY_PUBLIC_B64URL: &str = "JrQLj5P_89iXES9-vFgrIy29clF9CC_oPPsw3c5D0bs";

/// RFC 9421 §B.2.6 — signature value the spec expects from signing the
/// §B.2.6 base with `test-key-ed25519`.
const RFC_B26_EXPECTED_SIGNATURE_B64: &str =
    "wqcAqbmYJ2ji2glfAMaRy4gruYYnx2nEFN2HN6jrnDnQCK1u02Gb04v9EDgwUPiu4A0w6vuQv5lIp5WPpBKRCw==";

#[test]
fn rfc9421_b26_ed25519_full_signature_round_trip() {
    // ----- 1. Decode the RFC's Ed25519 private key from JWK ------------
    let private_key = B64URL
        .decode(RFC_TEST_KEY_PRIVATE_B64URL)
        .expect("RFC 9421 §B.1.4 private key must be valid base64url");
    assert_eq!(
        private_key.len(),
        32,
        "Ed25519 private key must be exactly 32 bytes"
    );

    let identity = Identity::from_key_bytes("test-key-ed25519", &private_key)
        .expect("from_key_bytes must accept the RFC 9421 test key");

    // Sanity: the public key derived from the private key matches §B.1.4.
    let derived_public = identity
        .public_key_bytes()
        .expect("keyed identity must have a public key");
    let expected_public = B64URL
        .decode(RFC_TEST_KEY_PUBLIC_B64URL)
        .expect("RFC 9421 §B.1.4 public key must be valid base64url");
    assert_eq!(
        derived_public.as_slice(),
        expected_public.as_slice(),
        "derived public key must match RFC 9421 §B.1.4"
    );

    // ----- 2. Build the §B.2.6 signature base via shared primitive ----
    let components: &[(&str, &str)] = &[
        ("date", "Tue, 20 Apr 2021 02:07:55 GMT"),
        ("@method", "POST"),
        ("@path", "/foo"),
        ("@authority", "example.com"),
        ("content-type", "application/json"),
        ("content-length", "18"),
    ];
    let params_value = concat!(
        "(\"date\" \"@method\" \"@path\" \"@authority\" \"content-type\" \"content-length\")",
        ";created=1618884473;keyid=\"test-key-ed25519\""
    );
    let base = build_signature_base(components, params_value);

    // Defensive: re-check the base bytes against the RFC text.
    let expected_base = concat!(
        "\"date\": Tue, 20 Apr 2021 02:07:55 GMT\n",
        "\"@method\": POST\n",
        "\"@path\": /foo\n",
        "\"@authority\": example.com\n",
        "\"content-type\": application/json\n",
        "\"content-length\": 18\n",
        "\"@signature-params\": ",
        "(\"date\" \"@method\" \"@path\" \"@authority\" \"content-type\" \"content-length\")",
        ";created=1618884473;keyid=\"test-key-ed25519\"",
    );
    assert_eq!(
        base, expected_base,
        "signature base must match RFC 9421 §B.2.6 byte-for-byte"
    );

    // ----- 3. Sign the base bytes -------------------------------------
    // Per §3.3.6: "The signature base is taken as the input message (M)
    // with no prehash function." Identity::sign uses ed25519-dalek's
    // SigningKey::sign which is exactly that.
    let signature = identity.sign(base.as_bytes());
    assert_eq!(
        signature.len(),
        64,
        "Ed25519 signature must be 64 octets per RFC 8032"
    );

    // ----- 4. Compare against the RFC's published signature value -----
    let actual_b64 = B64.encode(&signature);
    assert_eq!(
        actual_b64, RFC_B26_EXPECTED_SIGNATURE_B64,
        "Signing the §B.2.6 base with the §B.1.4 key MUST produce the \
         signature value the RFC publishes. Ed25519 is deterministic per \
         RFC 8032, so this is an exact-match assertion. If it fails, either \
         our base string format diverges from the RFC or the Ed25519 wiring \
         is broken."
    );
}

#[test]
fn rfc9421_b26_ed25519_verifies_with_public_key_only() {
    // The verifier-side conformance: given the RFC's public key, the RFC's
    // base string, and the RFC's signature value, our verify() must accept.
    let public_key = B64URL.decode(RFC_TEST_KEY_PUBLIC_B64URL).unwrap();
    let signature_bytes = B64.decode(RFC_B26_EXPECTED_SIGNATURE_B64).unwrap();
    assert_eq!(signature_bytes.len(), 64);

    let components: &[(&str, &str)] = &[
        ("date", "Tue, 20 Apr 2021 02:07:55 GMT"),
        ("@method", "POST"),
        ("@path", "/foo"),
        ("@authority", "example.com"),
        ("content-type", "application/json"),
        ("content-length", "18"),
    ];
    let params_value = concat!(
        "(\"date\" \"@method\" \"@path\" \"@authority\" \"content-type\" \"content-length\")",
        ";created=1618884473;keyid=\"test-key-ed25519\""
    );
    let base = build_signature_base(components, params_value);

    let verified = checkrd_core::identity::verify(base.as_bytes(), &signature_bytes, &public_key)
        .expect("verify must not error on well-formed inputs");
    assert!(
        verified,
        "verify() must accept the RFC 9421 §B.2.6 signature against the §B.1.4 public key"
    );
}

#[test]
fn rfc9421_b26_tamper_detection() {
    // The dual: any single-byte change in the signature base must cause
    // verification to fail. This proves the signature is binding the exact
    // bytes (not just a hash collision class).
    let public_key = B64URL.decode(RFC_TEST_KEY_PUBLIC_B64URL).unwrap();
    let signature_bytes = B64.decode(RFC_B26_EXPECTED_SIGNATURE_B64).unwrap();

    let components: &[(&str, &str)] = &[
        ("date", "Tue, 20 Apr 2021 02:07:55 GMT"),
        ("@method", "POST"),
        ("@path", "/foo"),
        ("@authority", "example.com"),
        ("content-type", "application/json"),
        ("content-length", "18"),
    ];
    let params_value = concat!(
        "(\"date\" \"@method\" \"@path\" \"@authority\" \"content-type\" \"content-length\")",
        ";created=1618884473;keyid=\"test-key-ed25519\""
    );
    let base = build_signature_base(components, params_value);

    // Mutate one byte (flip the @method from POST to PUT, padded to keep length).
    let mut mutated_components = components.to_vec();
    mutated_components[1] = ("@method", "GET ");
    let mutated = build_signature_base(&mutated_components, params_value);
    assert_ne!(base, mutated);

    let verified =
        checkrd_core::identity::verify(mutated.as_bytes(), &signature_bytes, &public_key)
            .expect("verify must not error on well-formed inputs");
    assert!(
        !verified,
        "verify() must reject a signature when the base bytes have been tampered with"
    );
}
