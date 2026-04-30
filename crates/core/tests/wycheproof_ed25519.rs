//! Wycheproof Ed25519 test vector compliance.
//!
//! Project Wycheproof is Google's collection of cryptographic test vectors
//! specifically designed to catch real-world implementation bugs:
//! malleable signatures, invalid point encodings, small-order public keys,
//! twist attacks, zero signatures, and other edge cases that have hit
//! production crypto libraries in the past.
//!
//! Reference: <https://github.com/C2SP/wycheproof>
//!
//! Vectors used: `testvectors_v1/ed25519_test.json` (150 tests across
//! 77 test groups). Each test is one of:
//!   - `valid`   — verification must succeed
//!   - `invalid` — verification must fail
//!   - `acceptable` — implementations may go either way; we don't enforce
//!
//! This is the gold-standard correctness gate for an Ed25519 implementation.
//! Passing it proves Checkrd's verify path is interoperable with any
//! compliant Ed25519 library, and that we resist the known classes of
//! signature-validation bugs Wycheproof was designed to catch.
//!
//! **Gated behind the `security_audit` feature** so it doesn't run on every
//! local `cargo test`. ed25519-dalek already ships its own Wycheproof
//! coverage upstream; this file proves our integration surface, not the
//! curve arithmetic. Re-enable for compliance audits or any change to the
//! verify path:
//!
//!     cargo test --package checkrd-core --features security_audit
#![cfg(feature = "security_audit")]

use checkrd_core::identity::verify;
use serde::Deserialize;

const VECTORS: &str = include_str!("data/wycheproof_ed25519.json");

#[derive(Debug, Deserialize)]
struct VectorFile {
    #[serde(rename = "testGroups")]
    test_groups: Vec<TestGroup>,
}

#[derive(Debug, Deserialize)]
struct TestGroup {
    #[serde(rename = "publicKey")]
    public_key: PublicKey,
    tests: Vec<TestCase>,
}

#[derive(Debug, Deserialize)]
struct PublicKey {
    pk: String, // 64 hex chars
}

#[derive(Debug, Deserialize)]
struct TestCase {
    #[serde(rename = "tcId")]
    tc_id: u32,
    #[allow(dead_code)]
    comment: String,
    msg: String,
    sig: String,
    result: String, // "valid" | "invalid" | "acceptable"
    #[serde(default)]
    #[allow(dead_code)]
    flags: Vec<String>,
}

fn hex_decode(s: &str) -> Option<Vec<u8>> {
    if !s.len().is_multiple_of(2) {
        return None;
    }
    let mut out = Vec::with_capacity(s.len() / 2);
    let bytes = s.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        let hi = hex_digit(bytes[i])?;
        let lo = hex_digit(bytes[i + 1])?;
        out.push((hi << 4) | lo);
        i += 2;
    }
    Some(out)
}

fn hex_digit(b: u8) -> Option<u8> {
    match b {
        b'0'..=b'9' => Some(b - b'0'),
        b'a'..=b'f' => Some(10 + b - b'a'),
        b'A'..=b'F' => Some(10 + b - b'A'),
        _ => None,
    }
}

#[test]
fn wycheproof_ed25519_compliance() {
    let vectors: VectorFile =
        serde_json::from_str(VECTORS).expect("vector file must be valid JSON");

    let mut total = 0usize;
    let mut valid_passed = 0usize;
    let mut invalid_rejected = 0usize;
    let mut acceptable_seen = 0usize;
    let mut failures: Vec<String> = Vec::new();

    for group in &vectors.test_groups {
        let pk_bytes = hex_decode(&group.public_key.pk).expect("public key must be valid hex");

        for tc in &group.tests {
            total += 1;
            let msg = hex_decode(&tc.msg).expect("msg must be valid hex");
            let sig = match hex_decode(&tc.sig) {
                Some(s) => s,
                None => {
                    // Malformed hex sig — only acceptable if the test is
                    // marked invalid. If valid, that's a vector bug.
                    if tc.result != "invalid" {
                        failures.push(format!(
                            "tcId={}: malformed sig hex on non-invalid case",
                            tc.tc_id
                        ));
                    }
                    invalid_rejected += 1;
                    continue;
                }
            };

            // verify() returns Err for length mismatches (which is the
            // correct behavior for malformed signatures); both Err and
            // Ok(false) are "rejection" outcomes.
            let result = verify(&msg, &sig, &pk_bytes);
            let our_verdict_passed = matches!(result, Ok(true));

            match tc.result.as_str() {
                "valid" => {
                    if our_verdict_passed {
                        valid_passed += 1;
                    } else {
                        failures.push(format!(
                            "tcId={}: VALID test rejected — comment={:?}",
                            tc.tc_id, tc.comment
                        ));
                    }
                }
                "invalid" => {
                    if !our_verdict_passed {
                        invalid_rejected += 1;
                    } else {
                        failures.push(format!(
                            "tcId={}: INVALID test accepted — comment={:?}",
                            tc.tc_id, tc.comment
                        ));
                    }
                }
                "acceptable" => {
                    // Don't enforce — tally only.
                    acceptable_seen += 1;
                }
                other => {
                    failures.push(format!("tcId={}: unknown result type {other:?}", tc.tc_id));
                }
            }
        }
    }

    eprintln!(
        "Wycheproof Ed25519: total={total} valid_passed={valid_passed} \
         invalid_rejected={invalid_rejected} acceptable_seen={acceptable_seen} \
         failures={}",
        failures.len()
    );

    if !failures.is_empty() {
        for f in failures.iter().take(20) {
            eprintln!("  - {f}");
        }
        if failures.len() > 20 {
            eprintln!("  ... ({} more)", failures.len() - 20);
        }
        panic!("Wycheproof Ed25519 compliance failures: {}", failures.len());
    }

    // Sanity: we ran the expected number of tests. The vector file says 150
    // total; we don't pin the exact count to avoid breaking on upstream
    // updates, but we want to be in the right ballpark.
    assert!(
        total >= 100,
        "expected at least 100 test cases, got {total}"
    );
    assert!(
        valid_passed > 0,
        "no valid signatures verified — implementation broken"
    );
    assert!(
        invalid_rejected > 0,
        "no invalid signatures rejected — implementation broken"
    );
}
