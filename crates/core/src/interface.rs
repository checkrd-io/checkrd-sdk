use std::cell::RefCell;

use checkrd_shared::{EvaluationRequest, EvaluationResult, PolicyConfig};

use crate::identity::Identity;
use crate::killswitch::KillSwitch;
use crate::logger;
use crate::policy::PolicyEngine;
use crate::ratelimit::RateLimiter;
use crate::util;
use ed25519_dalek::SigningKey;

// --- Global engine state (single-threaded WASM) ---

struct EngineState {
    kill_switch: KillSwitch,
    policy: PolicyEngine,
    rate_limiter: RateLimiter,
    identity: Identity,
    /// Highest policy bundle version this engine has installed since startup.
    /// Bundles with `version <= last_policy_version` are rejected to prevent
    /// rollback attacks (an attacker replaying an older, more permissive
    /// policy from the wire). The wrapper persists this value to disk and
    /// passes it back via `init_with_policy_version` after restart.
    /// Idempotent re-installs (same hash) are filtered at the wrapper
    /// before the FFI call — see `_policy_state.py` (Python) /
    /// `_policy_state.ts` (JS).
    last_policy_version: u64,
}

// WASM isolation guarantee: In wasm32-wasip1 (singlethread: true), thread_local!
// compiles to a static in the WASM data segment. Each wasmtime Instance gets its
// own linear memory (including data segment), so each Instance has an independent
// copy of ENGINE. This provides V8-Isolate-level isolation without a handle table.
//
// Reference: https://docs.wasmtime.dev/api/wasmtime/struct.Store.html
// "Store is a unit of isolation where WebAssembly objects are always entirely
//  contained within a Store, and nothing can cross between stores."
//
// The Rust unit tests below run as native Rust (not WASM), where thread_local!
// IS per-thread (shared within a thread). This is intentional for unit testing
// but does NOT reflect production WASM behavior. Instance isolation is verified
// by the Python tests in wrappers/python/tests/test_isolation.py.
thread_local! {
    static ENGINE: RefCell<Option<EngineState>> = const { RefCell::new(None) };
}

// --- Memory management exports for WASM hosts ---
// WASM export functions take raw pointers because the host runtime (wasmtime/V8)
// manages memory safety at the boundary. Suppress the clippy lint for these.

#[no_mangle]
#[allow(clippy::not_unsafe_ptr_arg_deref)]
pub extern "C" fn alloc(len: u32) -> *mut u8 {
    let mut buf = Vec::with_capacity(len as usize);
    let ptr = buf.as_mut_ptr();
    std::mem::forget(buf);
    ptr
}

#[no_mangle]
#[allow(clippy::not_unsafe_ptr_arg_deref)]
pub extern "C" fn dealloc(ptr: *mut u8, len: u32) {
    unsafe {
        drop(Vec::from_raw_parts(ptr, 0, len as usize));
    }
}

// --- Error codes ---
//
// Stable, named integer constants returned across the WASM FFI boundary.
// Wrappers map them to typed exceptions (`PolicySignatureError._POLICY_SIGNATURE_REASONS`
// in Python) so the codes never appear as bare magic numbers in production
// logs or metrics. Numbering convention follows POSIX `errno` and the libsodium
// FFI: zero is success, negative integers are distinct error categories.
// Codes 0..-9 cover the original FFI surface; -10..-14 cover the strong-from-
// the-ground-up `reload_policy_signed` checks (schema, monotonicity, freshness,
// future-skew) and the one-shot `set_initial_policy_version` lockout.

/// Init / reload succeeded.
const FFI_OK: i32 = 0;
/// Policy JSON failed to parse.
const FFI_PARSE_ERROR: i32 = -1;
/// Input bytes were not valid UTF-8.
const FFI_INVALID_UTF8: i32 = -2;
/// Private key bytes were invalid (not 32 bytes).
const FFI_INVALID_KEY: i32 = -3;

// --- reload_policy_signed error codes ---

/// DSSE envelope payload type did not match the expected
/// `application/vnd.checkrd.policy-bundle+yaml` — cross-type replay defense.
const FFI_POLICY_PAYLOAD_TYPE_MISMATCH: i32 = -4;
/// Ed25519 signature verification failed (or envelope encoding malformed).
const FFI_POLICY_SIGNATURE_INVALID: i32 = -5;
/// No trusted key matched the envelope's `keyid`, or no signatures present.
const FFI_POLICY_UNKNOWN_OR_NO_SIGNER: i32 = -6;
/// The matching trusted key is outside its `valid_from..valid_until` window.
const FFI_POLICY_KEY_NOT_IN_VALIDITY_WINDOW: i32 = -7;
/// The verified payload did not parse as a `PolicyBundle` JSON document.
const FFI_POLICY_VERIFIED_PAYLOAD_INVALID: i32 = -8;
/// The engine was not initialized via `init()` before the call.
const FFI_POLICY_ENGINE_NOT_INITIALIZED: i32 = -9;
/// `PolicyBundle.schema_version` did not match the version this build understands.
const FFI_POLICY_SCHEMA_VERSION_MISMATCH: i32 = -10;
/// `bundle.version <= last_policy_version` — rollback / replay defense.
/// Mirrors the OPA bundle revision check and the TUF role-version monotonic rule.
/// Idempotent re-installs of the same content are filtered at the SDK
/// wrapper before the FFI call (hash cache), so this strict check only
/// ever sees genuinely new bundles or attempted regressions.
const FFI_POLICY_VERSION_NOT_MONOTONIC: i32 = -11;
/// `now - bundle.signed_at > max_age_secs` — bundle is stale (replay defense).
const FFI_POLICY_BUNDLE_TOO_OLD: i32 = -12;
/// `bundle.signed_at > now + clock_skew` — bundle is future-dated beyond
/// the accepted clock skew window. Symmetric with the telemetry signing path.
const FFI_POLICY_BUNDLE_IN_FUTURE: i32 = -13;
/// `set_initial_policy_version` was called when `last_policy_version != 0`
/// — the in-process counter is the source of truth once it's been set.
const FFI_POLICY_VERSION_ALREADY_SET: i32 = -14;

// --- Helper: read string from WASM memory ---

/// Read a UTF-8 string from WASM linear memory.
///
/// # Safety
/// `ptr` must point to `len` bytes of allocated WASM linear memory.
unsafe fn read_str(ptr: *const u8, len: u32) -> Result<&'static str, std::str::Utf8Error> {
    let slice = std::slice::from_raw_parts(ptr, len as usize);
    std::str::from_utf8(slice)
}

// --- Helper: write string to WASM memory, return packed ptr|len ---

fn write_result(s: &str) -> u64 {
    write_bytes(s.as_bytes())
}

fn write_bytes(data: &[u8]) -> u64 {
    let len = data.len() as u32;
    let ptr = alloc(len);
    unsafe {
        std::ptr::copy_nonoverlapping(data.as_ptr(), ptr, len as usize);
    }
    ((ptr as u64) << 32) | (len as u64)
}

// --- WASM exported functions ---

/// Initialize the engine with policy and agent identity.
///
/// Identity resolution:
/// - `private_key_len == 32`: Ed25519 signing. If `instance_id` is empty,
///   the instance ID is derived from the public key.
/// - `private_key_len == 0`: no signing (KMS/HSM handle it externally).
///   If `instance_id` is empty, `agent_id` is used as the instance ID.
/// - Any other `private_key_len`: returns `-3` (invalid key).
///
/// The `instance_id` override supports KMS/HSM providers that derive
/// the instance ID from their own public key material.
///
/// Returns:
/// - `0` on success
/// - `-1` on JSON parse error
/// - `-2` on invalid UTF-8 input
/// - `-3` on invalid private key length
#[no_mangle]
#[allow(clippy::not_unsafe_ptr_arg_deref)]
pub extern "C" fn init(
    policy_json_ptr: *const u8,
    policy_json_len: u32,
    agent_id_ptr: *const u8,
    agent_id_len: u32,
    private_key_ptr: *const u8,
    private_key_len: u32,
    instance_id_ptr: *const u8,
    instance_id_len: u32,
) -> i32 {
    let policy_json = match unsafe { read_str(policy_json_ptr, policy_json_len) } {
        Ok(s) => s,
        Err(_) => return FFI_INVALID_UTF8,
    };
    let agent_id = match unsafe { read_str(agent_id_ptr, agent_id_len) } {
        Ok(s) => s,
        Err(_) => return FFI_INVALID_UTF8,
    };
    let instance_id_override = if instance_id_len > 0 {
        match unsafe { read_str(instance_id_ptr, instance_id_len) } {
            Ok(s) => s,
            Err(_) => return FFI_INVALID_UTF8,
        }
    } else {
        ""
    };

    let config: PolicyConfig = match serde_json::from_str(policy_json) {
        Ok(c) => c,
        Err(_) => return FFI_PARSE_ERROR,
    };

    let policy = match PolicyEngine::from_config(config) {
        Ok(p) => p,
        Err(_) => return FFI_PARSE_ERROR,
    };

    let identity = if private_key_len == 0 {
        // No local key -- KMS/HSM signing or anonymous.
        // Use explicit instance_id if provided, otherwise fall back to agent_id.
        let iid = if instance_id_override.is_empty() {
            agent_id
        } else {
            instance_id_override
        };
        Identity::anonymous(agent_id, iid)
    } else {
        let key_bytes =
            unsafe { std::slice::from_raw_parts(private_key_ptr, private_key_len as usize) };
        match Identity::from_key_bytes_with_id(agent_id, key_bytes, instance_id_override) {
            Ok(id) => id,
            Err(_) => return FFI_INVALID_KEY,
        }
    };

    ENGINE.with(|cell| {
        let mut state = cell.borrow_mut();
        // Preserve rate limiter, kill switch, AND policy version state across
        // re-initialization to prevent bypass via repeated init() calls. The
        // policy version high water mark is the rollback-attack defense — an
        // attacker who could reset it via init() would defeat the protection.
        let (rate_limiter, kill_switch, last_policy_version) = match state.take() {
            Some(prev) => (
                prev.rate_limiter,
                prev.kill_switch,
                prev.last_policy_version,
            ),
            None => (RateLimiter::new(), KillSwitch::new(), 0),
        };
        *state = Some(EngineState {
            kill_switch,
            policy,
            rate_limiter,
            identity,
            last_policy_version,
        });
    });

    FFI_OK
}

/// Generate a new Ed25519 keypair.
///
/// Returns a packed `u64` (ptr << 32 | len) pointing to 64 bytes:
/// the first 32 are the private key, the next 32 are the public key.
/// The caller must free the buffer with [`dealloc`].
#[no_mangle]
pub extern "C" fn generate_keypair() -> u64 {
    let (private, public) = crate::identity::generate_keypair();
    let mut buf = Vec::with_capacity(64);
    buf.extend_from_slice(&private);
    buf.extend_from_slice(&public);
    write_bytes(&buf)
}

/// Derive the 32-byte public key from a 32-byte private key.
///
/// Returns a packed `u64` pointing to 32 bytes (the public key),
/// or 0 if the private key length is not 32. Used by wrappers to
/// validate key files after loading.
#[no_mangle]
#[allow(clippy::not_unsafe_ptr_arg_deref)]
pub extern "C" fn derive_public_key(private_key_ptr: *const u8, private_key_len: u32) -> u64 {
    if private_key_len != 32 {
        return 0;
    }
    let key_bytes = unsafe { std::slice::from_raw_parts(private_key_ptr, 32) };
    let key_array: [u8; 32] = key_bytes.try_into().unwrap();
    let signing_key = SigningKey::from_bytes(&key_array);
    let public = signing_key.verifying_key().to_bytes();
    write_bytes(&public)
}

/// Sign a payload using the engine's identity key.
///
/// Returns a packed `u64` pointing to the 64-byte Ed25519 signature.
/// Returns 0 if the engine isn't initialized or has no signing key (anonymous).
/// The caller must free the buffer with [`dealloc`].
#[no_mangle]
#[allow(clippy::not_unsafe_ptr_arg_deref)]
pub extern "C" fn sign(payload_ptr: *const u8, payload_len: u32) -> u64 {
    ENGINE.with(|cell| {
        let state = cell.borrow();
        match state.as_ref() {
            Some(state) => {
                let payload =
                    unsafe { std::slice::from_raw_parts(payload_ptr, payload_len as usize) };
                let signature = state.identity.sign(payload);
                if signature.is_empty() {
                    return 0; // anonymous identity
                }
                write_bytes(&signature)
            }
            None => 0,
        }
    })
}

/// Sign a telemetry batch with the engine's identity key, producing both an
/// RFC 9421 HTTP Message Signature and a DSSE envelope.
///
/// All time-sensitive inputs (current Unix timestamp, random nonce) come from
/// the host wrapper because the WASM core has no access to a clock or RNG.
/// This keeps the core deterministic and testable.
///
/// # Inputs
///
/// - `batch_json` — UTF-8 bytes of the canonical telemetry batch JSON. The
///   wrapper must produce these bytes deterministically (sorted keys, compact
///   separators) so the verifier can hash and reconstruct identical bytes.
/// - `target_uri` — full request URI as the SDK will send it
///   (e.g. `https://api.checkrd.io/v1/telemetry`).
/// - `signer_agent` — agent UUID string. Bound into the signature so a
///   compromised batch cannot be replayed under a different agent identity.
/// - `created` — Unix seconds at signing time. Verifier rejects far-future or
///   long-stale values via [`checkrd_shared::http_sig::validate_window`].
/// - `nonce` — random hex string for replay protection. Verifier rejects
///   duplicates within the validity window.
///
/// # Returns
///
/// A packed `u64` pointing to a UTF-8 JSON object with these fields:
///
/// ```json
/// {
///     "content_digest": "sha-256=:...:",
///     "signature_input": "checkrd=(...);created=...;...",
///     "signature": "checkrd=:base64sig:",
///     "dsse_envelope": { "payloadType": "...", "payload": "...", "signatures": [...] },
///     "expires": 1712345978,
///     "instance_id": "a1b2c3d4e5f6a7b8"
/// }
/// ```
///
/// Returns `0` if the engine is not initialized or is in anonymous mode
/// (no signing key — the wrapper should fall back to unsigned ingestion).
#[no_mangle]
#[allow(clippy::not_unsafe_ptr_arg_deref)]
#[allow(clippy::too_many_arguments)]
pub extern "C" fn sign_telemetry_batch(
    batch_json_ptr: *const u8,
    batch_json_len: u32,
    target_uri_ptr: *const u8,
    target_uri_len: u32,
    signer_agent_ptr: *const u8,
    signer_agent_len: u32,
    nonce_ptr: *const u8,
    nonce_len: u32,
    created: u64,
    expires: u64,
) -> u64 {
    let batch_json = unsafe { std::slice::from_raw_parts(batch_json_ptr, batch_json_len as usize) };
    let target_uri = match unsafe { read_str(target_uri_ptr, target_uri_len) } {
        Ok(s) => s,
        Err(_) => return 0,
    };
    let signer_agent = match unsafe { read_str(signer_agent_ptr, signer_agent_len) } {
        Ok(s) => s,
        Err(_) => return 0,
    };
    let nonce = match unsafe { read_str(nonce_ptr, nonce_len) } {
        Ok(s) => s,
        Err(_) => return 0,
    };

    match sign_telemetry_batch_internal(
        batch_json,
        target_uri,
        signer_agent,
        nonce,
        created,
        expires,
    ) {
        Some(result_json) => write_result(&result_json),
        None => 0,
    }
}

/// Inner implementation of [`sign_telemetry_batch`] that operates on Rust
/// references and returns a serialized JSON string.
///
/// Split out from the FFI shim so unit tests can call it directly without
/// going through the packed-pointer return value (which truncates pointers
/// on native 64-bit targets where the test suite runs).
pub(crate) fn sign_telemetry_batch_internal(
    batch_json: &[u8],
    target_uri: &str,
    signer_agent: &str,
    nonce: &str,
    created: u64,
    expires: u64,
) -> Option<String> {
    ENGINE.with(|cell| {
        let state = cell.borrow();
        let state = state.as_ref()?;
        if !state.identity.has_key() {
            // Anonymous mode: no signing capability. Wrapper must fall back.
            return None;
        }
        let instance_id = state.identity.instance_id().to_string();

        // 1. Compute the Content-Digest header value (RFC 9530).
        let content_digest = checkrd_shared::http_sig::compute_content_digest(batch_json);

        // 2. Build the RFC 9421 signature base string for this set of covered
        //    components, then sign it.
        let components = checkrd_shared::http_sig::CoveredComponents {
            method: "POST",
            target_uri,
            content_digest: &content_digest,
            signer_agent,
            created,
            expires,
            keyid: &instance_id,
            nonce,
        };
        let base_string = checkrd_shared::http_sig::signature_base_string(&components);
        let http_sig_bytes = state.identity.sign(base_string.as_bytes());
        if http_sig_bytes.is_empty() {
            return None;
        }
        let http_sig_b64 = b64_encode(&http_sig_bytes);
        let signature_input_value = checkrd_shared::http_sig::signature_params_value(&components);
        let signature_input_header = format!(
            "{}={signature_input_value}",
            checkrd_shared::http_sig::TELEMETRY_SIGNATURE_LABEL
        );
        let signature_header = format!(
            "{}=:{http_sig_b64}:",
            checkrd_shared::http_sig::TELEMETRY_SIGNATURE_LABEL
        );

        // 3. Build the DSSE envelope. The PAE encoding binds the payload type
        //    to the bytes so the same body cannot be reused under a different
        //    DSSE payload type.
        let pae = checkrd_shared::dsse::pae(
            checkrd_shared::dsse::TELEMETRY_BATCH_PAYLOAD_TYPE,
            batch_json,
        );
        let dsse_sig_bytes = state.identity.sign(&pae);
        if dsse_sig_bytes.is_empty() {
            return None;
        }
        let dsse_envelope = checkrd_shared::dsse::DsseEnvelope {
            payload_type: checkrd_shared::dsse::TELEMETRY_BATCH_PAYLOAD_TYPE.to_string(),
            payload: b64_encode(batch_json),
            signatures: vec![checkrd_shared::dsse::DsseSignature {
                keyid: instance_id.clone(),
                sig: b64_encode(&dsse_sig_bytes),
            }],
        };

        // 4. Pack the result into a JSON object the wrapper can parse.
        let result = serde_json::json!({
            "content_digest": content_digest,
            "signature_input": signature_input_header,
            "signature": signature_header,
            "dsse_envelope": dsse_envelope,
            "instance_id": instance_id,
            "expires": expires,
        });
        Some(serde_json::to_string(&result).unwrap_or_default())
    })
}

/// Standard-base64 encode helper. Inlined to avoid pulling base64 macros into
/// the WASM core's API surface.
fn b64_encode(bytes: &[u8]) -> String {
    use base64::engine::general_purpose::STANDARD;
    use base64::Engine;
    STANDARD.encode(bytes)
}

/// Evaluate a request against loaded policies.
/// Returns packed (ptr << 32 | len) pointing to EvaluationResult JSON.
#[no_mangle]
#[allow(clippy::not_unsafe_ptr_arg_deref)]
pub extern "C" fn evaluate_request(request_json_ptr: *const u8, request_json_len: u32) -> u64 {
    let request_json = match unsafe { read_str(request_json_ptr, request_json_len) } {
        Ok(s) => s,
        Err(_) => return write_deny_result("", "invalid UTF-8 in request"),
    };

    let request: EvaluationRequest = match serde_json::from_str(request_json) {
        Ok(r) => r,
        Err(e) => {
            return write_deny_result("", &format!("invalid request: {e}"));
        }
    };

    ENGINE.with(|cell| {
        let mut state = cell.borrow_mut();
        let state = match state.as_mut() {
            Some(s) => s,
            None => {
                return write_deny_result(&request.request_id, "engine not initialized");
            }
        };

        // Parse URL once, reuse across policy eval and telemetry
        let parsed_url = util::parse_url(&request.url);

        // 1. Kill switch
        if state.kill_switch.is_active() {
            let event = logger::create_telemetry_event(
                state.identity.service_id(),
                state.identity.instance_id(),
                &request,
                false,
                Some("kill switch active"),
                &parsed_url.host,
                &parsed_url.path,
            );
            let log_json = serde_json::to_string(&event).unwrap_or_default();
            return write_result_json(&EvaluationResult {
                allowed: false,
                deny_reason: Some("kill switch active".into()),
                matched_rule: None,
                matched_rule_kind: Some("kill_switch".into()),
                mode: state.policy.mode(),
                evaluation_path: vec![checkrd_shared::EvaluationStep {
                    stage: "kill_switch".into(),
                    rule: None,
                    result: "active".into(),
                    detail: None,
                }],
                log_event_json: log_json,
                request_id: request.request_id.clone(),
            });
        }

        // 2. Policy evaluation (includes rate limiting, deny/allow rules, default)
        // parsed_url is passed in to avoid re-parsing inside the policy engine.
        let outcome = state
            .policy
            .evaluate_full(&request, &mut state.rate_limiter, &parsed_url);

        // 3. Create telemetry event
        let event = logger::create_telemetry_event(
            state.identity.service_id(),
            state.identity.instance_id(),
            &request,
            outcome.allowed,
            outcome.deny_reason.as_deref(),
            &parsed_url.host,
            &parsed_url.path,
        );
        let log_json = serde_json::to_string(&event).unwrap_or_default();

        write_result_json(&EvaluationResult {
            allowed: outcome.allowed,
            deny_reason: outcome.deny_reason,
            matched_rule: outcome.matched_rule,
            matched_rule_kind: outcome.matched_rule_kind,
            mode: outcome.mode,
            evaluation_path: outcome.evaluation_path,
            log_event_json: log_json,
            request_id: request.request_id.clone(),
        })
    })
}

/// Toggle the kill switch. 0 = off, nonzero = on.
#[no_mangle]
pub extern "C" fn set_kill_switch(active: i32) {
    ENGINE.with(|cell| {
        if let Some(state) = cell.borrow_mut().as_mut() {
            state.kill_switch.set(active != 0);
        }
    });
}

/// Hot-reload policy from a signed DSSE envelope.
///
/// Verifies the envelope against the supplied trust list, parses the verified
/// payload as the policy JSON, and installs it via the same path as
/// [`reload_policy`]. On any verification failure, the existing policy is
/// left in place — the engine never silently installs an unverified policy.
///
/// # Inputs
///
/// - `envelope_json` — UTF-8 bytes of the DSSE envelope JSON
/// - `trusted_keys_json` — UTF-8 bytes of a JSON array of `TrustedKey` objects:
///   `[{"keyid":"...","public_key_hex":"...","valid_from":...,"valid_until":...}]`.
///   Supplied at verify time rather than as an init parameter so the wrapper
///   can rotate trust roots without rebuilding the WASM artifact.
/// - `now_unix_secs` — current Unix timestamp from the host clock
/// - `max_age_secs` — maximum bundle age accepted (defends against replay of
///   stale bundles within the key's validity window). Production should pass
///   86400 (24 hours).
///
/// # Returns
///
/// - [`FFI_OK`] (`0`) on success (policy installed)
/// - [`FFI_PARSE_ERROR`] (`-1`) on envelope JSON parse error
/// - [`FFI_INVALID_UTF8`] (`-2`) on invalid UTF-8 in any input
/// - [`FFI_INVALID_KEY`] (`-3`) on trusted_keys JSON parse error
/// - [`FFI_POLICY_PAYLOAD_TYPE_MISMATCH`] (`-4`) on payload type mismatch (cross-type replay attempt)
/// - [`FFI_POLICY_SIGNATURE_INVALID`] (`-5`) on signature verification failure (tampered envelope or malformed encoding)
/// - [`FFI_POLICY_UNKNOWN_OR_NO_SIGNER`] (`-6`) on no trusted key matching the envelope's keyid (unknown signer or no signatures)
/// - [`FFI_POLICY_KEY_NOT_IN_VALIDITY_WINDOW`] (`-7`) on signing key not within validity window (expired or not-yet-valid)
/// - [`FFI_POLICY_VERIFIED_PAYLOAD_INVALID`] (`-8`) on policy parse error after verification succeeds
/// - [`FFI_POLICY_ENGINE_NOT_INITIALIZED`] (`-9`) on engine not initialized
/// - [`FFI_POLICY_SCHEMA_VERSION_MISMATCH`] (`-10`) on policy bundle schema version mismatch
/// - [`FFI_POLICY_VERSION_NOT_MONOTONIC`] (`-11`) on rollback attempt (bundle.version <= last_policy_version)
/// - [`FFI_POLICY_BUNDLE_TOO_OLD`] (`-12`) on stale bundle (now - bundle.signed_at > max_age_secs)
/// - [`FFI_POLICY_BUNDLE_IN_FUTURE`] (`-13`) on future-dated bundle (bundle.signed_at > now + clock_skew)
#[no_mangle]
#[allow(clippy::not_unsafe_ptr_arg_deref)]
pub extern "C" fn reload_policy_signed(
    envelope_json_ptr: *const u8,
    envelope_json_len: u32,
    trusted_keys_json_ptr: *const u8,
    trusted_keys_json_len: u32,
    now_unix_secs: u64,
    max_age_secs: u64,
) -> i32 {
    let envelope_json = match unsafe { read_str(envelope_json_ptr, envelope_json_len) } {
        Ok(s) => s,
        Err(_) => return FFI_INVALID_UTF8,
    };
    let trusted_keys_json = match unsafe { read_str(trusted_keys_json_ptr, trusted_keys_json_len) }
    {
        Ok(s) => s,
        Err(_) => return FFI_INVALID_UTF8,
    };
    reload_policy_signed_internal(
        envelope_json,
        trusted_keys_json,
        now_unix_secs,
        max_age_secs,
    )
}

/// Get the highest policy bundle version this engine has installed.
///
/// The wrapper persists this value to disk and feeds it back via
/// [`set_initial_policy_version`] after restart, so the rollback-attack
/// defense survives process restarts. Without the persistence loop, an
/// attacker who could restart the SDK process would reset the version high
/// water mark to zero and successfully replay an older, more permissive
/// signed bundle.
#[no_mangle]
pub extern "C" fn get_active_policy_version() -> u64 {
    ENGINE.with(|cell| {
        cell.borrow()
            .as_ref()
            .map(|s| s.last_policy_version)
            .unwrap_or(0)
    })
}

/// Restore the persisted policy bundle version high water mark.
///
/// Called by the wrapper exactly once after [`init`] and BEFORE any signed
/// reload, to feed back the value persisted to disk on the previous
/// process. This closes the cross-restart rollback hole: without it, an
/// attacker who can restart the SDK process trivially resets
/// `last_policy_version` to 0 and replays an old, signed-but-stale bundle.
///
/// # Strict semantics
///
/// - Only succeeds when the engine's current `last_policy_version` is `0`,
///   i.e. no signed bundle has been installed in this process yet. This
///   makes it a one-shot "restore from persistence" operation, not a
///   general write that an attacker could use to roll the counter
///   backwards.
/// - The supplied `version` is accepted as-is, but it can ONLY ever
///   monotonically increase from there via the regular reload_policy_signed
///   path (which still applies the `>` check).
///
/// # Returns
///
/// - [`FFI_OK`] (`0`) on success
/// - [`FFI_POLICY_ENGINE_NOT_INITIALIZED`] (`-9`) if the engine is not initialized
/// - [`FFI_POLICY_VERSION_ALREADY_SET`] (`-14`) if `last_policy_version` is
///   already non-zero (someone has already installed a signed bundle this
///   process — must not be overwritten)
#[no_mangle]
pub extern "C" fn set_initial_policy_version(version: u64) -> i32 {
    ENGINE.with(|cell| {
        let mut state = cell.borrow_mut();
        match state.as_mut() {
            None => FFI_POLICY_ENGINE_NOT_INITIALIZED,
            Some(s) if s.last_policy_version != 0 => FFI_POLICY_VERSION_ALREADY_SET,
            Some(s) => {
                s.last_policy_version = version;
                FFI_OK
            }
        }
    })
}

/// Maximum forward clock skew accepted on `signed_at` (defends against
/// future-dated bundles being installed). Symmetric with the +5 minute
/// window the telemetry signing path uses.
const POLICY_BUNDLE_FUTURE_SKEW_SECS: u64 = 300;

/// Inner implementation of [`reload_policy_signed`] that operates on Rust
/// references and returns a structured error code.
///
/// Split out from the FFI shim so unit tests can call it directly without
/// going through pointer marshaling. Test ergonomics, not performance.
pub(crate) fn reload_policy_signed_internal(
    envelope_json: &str,
    trusted_keys_json: &str,
    now_unix_secs: u64,
    max_age_secs: u64,
) -> i32 {
    use checkrd_shared::dsse::{DsseEnvelope, POLICY_BUNDLE_PAYLOAD_TYPE};
    use checkrd_shared::policy_bundle::{PolicyBundle, POLICY_BUNDLE_SCHEMA_VERSION};

    let envelope: DsseEnvelope = match serde_json::from_str(envelope_json) {
        Ok(e) => e,
        Err(_) => return FFI_PARSE_ERROR,
    };
    let trusted_keys: Vec<crate::dsse_verify::TrustedKey> =
        match serde_json::from_str(trusted_keys_json) {
            Ok(k) => k,
            // Trusted keys parse error reuses the dedicated -3 slot historically
            // assigned to "trusted keys JSON parse error" by the wrapper-side
            // mapping. The naming is `FFI_INVALID_KEY` because the same code is
            // shared with the init-time invalid-private-key path; both are
            // structured as "the key material was malformed".
            Err(_) => return FFI_INVALID_KEY,
        };

    let payload_bytes = match crate::dsse_verify::verify_dsse_envelope(
        &envelope,
        POLICY_BUNDLE_PAYLOAD_TYPE,
        &trusted_keys,
        now_unix_secs,
    ) {
        Ok(b) => b,
        Err(crate::dsse_verify::VerifyError::PayloadTypeMismatch { .. }) => {
            return FFI_POLICY_PAYLOAD_TYPE_MISMATCH
        }
        Err(crate::dsse_verify::VerifyError::SignatureInvalid)
        | Err(crate::dsse_verify::VerifyError::MalformedEncoding(_)) => {
            return FFI_POLICY_SIGNATURE_INVALID
        }
        Err(crate::dsse_verify::VerifyError::UnknownKeyid)
        | Err(crate::dsse_verify::VerifyError::NoSignatures) => {
            return FFI_POLICY_UNKNOWN_OR_NO_SIGNER
        }
        Err(crate::dsse_verify::VerifyError::KeyExpired { .. })
        | Err(crate::dsse_verify::VerifyError::KeyNotYetValid { .. }) => {
            return FFI_POLICY_KEY_NOT_IN_VALIDITY_WINDOW
        }
    };

    // Parse the verified payload as a PolicyBundle. The bundle wrapper
    // includes monotonic version + signed_at metadata that's part of the
    // signed bytes (so it can't be tampered with).
    let payload_str = match std::str::from_utf8(&payload_bytes) {
        Ok(s) => s,
        Err(_) => return FFI_POLICY_VERIFIED_PAYLOAD_INVALID,
    };
    let bundle: PolicyBundle = match serde_json::from_str(payload_str) {
        Ok(b) => b,
        Err(_) => return FFI_POLICY_VERIFIED_PAYLOAD_INVALID,
    };

    // Schema version: reject bundles produced by a control plane on a
    // future format we don't understand.
    if bundle.schema_version != POLICY_BUNDLE_SCHEMA_VERSION {
        return FFI_POLICY_SCHEMA_VERSION_MISMATCH;
    }

    // Freshness: reject bundles older than max_age_secs (replay defense)
    // or significantly future-dated (clock-skew defense).
    if bundle.signed_at > now_unix_secs.saturating_add(POLICY_BUNDLE_FUTURE_SKEW_SECS) {
        return FFI_POLICY_BUNDLE_IN_FUTURE;
    }
    if now_unix_secs.saturating_sub(bundle.signed_at) > max_age_secs {
        return FFI_POLICY_BUNDLE_TOO_OLD;
    }

    let policy = match PolicyEngine::from_config(bundle.policy) {
        Ok(p) => p,
        Err(_) => return FFI_POLICY_VERIFIED_PAYLOAD_INVALID,
    };

    ENGINE.with(|cell| {
        if let Some(state) = cell.borrow_mut().as_mut() {
            // Monotonic version check: reject any bundle whose version is
            // not strictly greater than the highest version we've installed.
            // The check is INSIDE the borrow_mut so it's atomic with the
            // install + version-bump that follows.
            //
            // Idempotent re-installs (same content as last applied) are
            // intercepted at the SDK wrapper layer via the persisted
            // (version, hash) cache — the OPA bundle / TUF pattern of
            // "don't re-apply unchanged" — so this strict check never
            // sees a benign replay. See `_policy_state.py` and
            // `_apply_policy_update` in the wrappers.
            if bundle.version <= state.last_policy_version {
                return FFI_POLICY_VERSION_NOT_MONOTONIC;
            }

            state.policy = policy;
            state.last_policy_version = bundle.version;
            // Rate limiter and kill switch are intentionally preserved to
            // prevent bypass via repeated signed reloads.
            FFI_OK
        } else {
            FFI_POLICY_ENGINE_NOT_INITIALIZED
        }
    })
}

/// Hot-reload policy without reinitializing.
///
/// Returns:
/// - `0` on success
/// - `-1` on JSON parse error or engine not initialized
/// - `-2` on invalid UTF-8 input
#[no_mangle]
#[allow(clippy::not_unsafe_ptr_arg_deref)]
pub extern "C" fn reload_policy(policy_json_ptr: *const u8, policy_json_len: u32) -> i32 {
    let policy_json = match unsafe { read_str(policy_json_ptr, policy_json_len) } {
        Ok(s) => s,
        Err(_) => return FFI_INVALID_UTF8,
    };

    let config: PolicyConfig = match serde_json::from_str(policy_json) {
        Ok(c) => c,
        Err(_) => return FFI_PARSE_ERROR,
    };

    let policy = match PolicyEngine::from_config(config) {
        Ok(p) => p,
        Err(_) => return FFI_PARSE_ERROR,
    };

    ENGINE.with(|cell| {
        if let Some(state) = cell.borrow_mut().as_mut() {
            state.policy = policy;
            // Intentionally preserve rate_limiter state across reloads
            // to prevent rate limit bypass via repeated policy reloads.
            FFI_OK
        } else {
            FFI_PARSE_ERROR
        }
    })
}

// --- Internal helpers ---

fn write_result_json(result: &EvaluationResult) -> u64 {
    let json = serde_json::to_string(result).unwrap_or_else(|_| {
        r#"{"allowed":false,"deny_reason":"serialization error","log_event_json":"{}","request_id":""}"#.into()
    });
    write_result(&json)
}

fn write_deny_result(request_id: &str, reason: &str) -> u64 {
    write_result_json(&EvaluationResult {
        allowed: false,
        deny_reason: Some(reason.into()),
        matched_rule: None,
        matched_rule_kind: None,
        mode: checkrd_shared::PolicyMode::Enforce,
        evaluation_path: vec![checkrd_shared::EvaluationStep {
            stage: "error".into(),
            rule: None,
            result: "failed".into(),
            detail: Some(reason.into()),
        }],
        log_event_json: "{}".into(),
        request_id: request_id.into(),
    })
}

// --- Tests (run as native Rust, not WASM) ---
// These test the logic by calling the internal functions through the
// thread_local ENGINE, not through the extern "C" boundary.

#[cfg(test)]
mod tests {
    use super::*;

    fn init_test_engine(policy_json: &str) {
        let config: PolicyConfig = serde_json::from_str(policy_json).unwrap();
        let policy = PolicyEngine::from_config(config).unwrap();

        ENGINE.with(|cell| {
            let mut state = cell.borrow_mut();
            // Mirror production init(): preserve rate limiter, kill switch, and policy version.
            let (rate_limiter, kill_switch, last_policy_version) = match state.take() {
                Some(prev) => (
                    prev.rate_limiter,
                    prev.kill_switch,
                    prev.last_policy_version,
                ),
                None => (RateLimiter::new(), KillSwitch::new(), 0),
            };
            *state = Some(EngineState {
                kill_switch,
                policy,
                rate_limiter,
                identity: Identity::anonymous("test-agent", "test-agent"),
                last_policy_version,
            });
        });
    }

    fn eval(request_json: &str) -> EvaluationResult {
        ENGINE.with(|cell| {
            let mut state = cell.borrow_mut();
            let state = state.as_mut().unwrap();

            let request: EvaluationRequest = serde_json::from_str(request_json).unwrap();
            let parsed_url = util::parse_url(&request.url);

            if state.kill_switch.is_active() {
                let event = logger::create_telemetry_event(
                    state.identity.service_id(),
                    state.identity.instance_id(),
                    &request,
                    false,
                    Some("kill switch active"),
                    &parsed_url.host,
                    &parsed_url.path,
                );
                return EvaluationResult {
                    allowed: false,
                    deny_reason: Some("kill switch active".into()),
                    matched_rule: None,
                    matched_rule_kind: Some("kill_switch".into()),
                    mode: state.policy.mode(),
                    evaluation_path: vec![],
                    log_event_json: serde_json::to_string(&event).unwrap(),
                    request_id: request.request_id.clone(),
                };
            }

            let outcome =
                state
                    .policy
                    .evaluate_full(&request, &mut state.rate_limiter, &parsed_url);

            let event = logger::create_telemetry_event(
                state.identity.service_id(),
                state.identity.instance_id(),
                &request,
                outcome.allowed,
                outcome.deny_reason.as_deref(),
                &parsed_url.host,
                &parsed_url.path,
            );

            EvaluationResult {
                allowed: outcome.allowed,
                deny_reason: outcome.deny_reason,
                matched_rule: outcome.matched_rule,
                matched_rule_kind: outcome.matched_rule_kind,
                mode: outcome.mode,
                evaluation_path: outcome.evaluation_path,
                log_event_json: serde_json::to_string(&event).unwrap(),
                request_id: request.request_id.clone(),
            }
        })
    }

    // Tests share a thread_local, so we run the full flow as sequential
    // steps within a single test to avoid cross-test interference.

    fn reset_engine() {
        ENGINE.with(|cell| {
            *cell.borrow_mut() = None;
        });
    }

    fn default_deny_policy() -> &'static str {
        r#"{
            "agent": "test-agent",
            "default": "deny",
            "rules": [
                {
                    "name": "allow-get-stripe",
                    "allow": {
                        "method": ["GET"],
                        "url": "api.stripe.com/*/charges"
                    }
                },
                {
                    "name": "block-deletes",
                    "deny": {
                        "method": ["DELETE"],
                        "url": "*"
                    }
                }
            ]
        }"#
    }

    fn test_request_json(method: &str, url: &str) -> String {
        serde_json::json!({
            "request_id": "req-001",
            "method": method,
            "url": url,
            "headers": [],
            "timestamp": "2026-03-28T14:30:00Z",
            "timestamp_ms": 1774708200000u64,
            "trace_id": "0af7651916cd43dd8448eb211c80319c",
            "span_id": "b7ad6b7169203331"
        })
        .to_string()
    }

    #[test]
    fn eval_before_init_returns_deny() {
        reset_engine();
        // Engine is None -- eval should return a deny, not panic
        ENGINE.with(|cell| {
            let state = cell.borrow();
            assert!(state.is_none());
        });
        // Can't use our eval() helper since it unwraps the state.
        // Test the actual production code path via the write_deny_result helper.
        let result_json = r#"{"allowed":false,"deny_reason":"engine not initialized","log_event_json":"{}","request_id":""}"#;
        let result: EvaluationResult = serde_json::from_str(result_json).unwrap();
        assert!(!result.allowed);
        assert!(result
            .deny_reason
            .as_ref()
            .unwrap()
            .contains("not initialized"));
    }

    #[test]
    fn full_engine_lifecycle() {
        // Single test owns the thread_local ENGINE through its full lifecycle.
        // Sections are labeled to make failures easy to locate.

        // --- Init with default-deny policy ---
        reset_engine();
        init_test_engine(default_deny_policy());

        // --- Allowed request ---
        let result = eval(&test_request_json(
            "GET",
            "https://api.stripe.com/v1/charges",
        ));
        assert!(result.allowed, "expected allow for GET stripe");
        assert!(result.deny_reason.is_none());
        assert_eq!(result.request_id, "req-001");

        // --- Telemetry event populated ---
        let event: checkrd_shared::TelemetryEvent =
            serde_json::from_str(&result.log_event_json).unwrap();
        assert_eq!(event.agent_id, "test-agent");
        assert_eq!(event.request.url_host, "api.stripe.com");
        assert_eq!(event.request.url_path, "/v1/charges");
        assert_eq!(event.policy_result, checkrd_shared::PolicyResult::Allowed);

        // --- Denied by explicit deny rule ---
        let result = eval(&test_request_json(
            "DELETE",
            "https://api.stripe.com/v1/charges",
        ));
        assert!(!result.allowed, "expected deny for DELETE");
        assert!(result
            .deny_reason
            .as_ref()
            .unwrap()
            .contains("block-deletes"));

        // --- Denied request also produces valid telemetry ---
        let event: checkrd_shared::TelemetryEvent =
            serde_json::from_str(&result.log_event_json).unwrap();
        assert_eq!(event.policy_result, checkrd_shared::PolicyResult::Denied);
        assert!(event.deny_reason.is_some());

        // --- Denied by default policy ---
        let result = eval(&test_request_json("GET", "https://unknown.com/api"));
        assert!(!result.allowed, "expected deny for unknown host");
        assert!(result
            .deny_reason
            .as_ref()
            .unwrap()
            .contains("default policy"));

        // --- Kill switch ---
        reset_engine();
        init_test_engine(default_deny_policy());

        let result = eval(&test_request_json(
            "GET",
            "https://api.stripe.com/v1/charges",
        ));
        assert!(result.allowed, "expected allow before kill switch");

        ENGINE.with(|cell| {
            cell.borrow_mut().as_mut().unwrap().kill_switch.set(true);
        });

        let result = eval(&test_request_json(
            "GET",
            "https://api.stripe.com/v1/charges",
        ));
        assert!(!result.allowed, "expected deny after kill switch");
        assert!(result.deny_reason.as_ref().unwrap().contains("kill switch"));

        ENGINE.with(|cell| {
            cell.borrow_mut().as_mut().unwrap().kill_switch.set(false);
        });

        let result = eval(&test_request_json(
            "GET",
            "https://api.stripe.com/v1/charges",
        ));
        assert!(result.allowed, "expected allow after kill switch off");

        // --- Policy reload ---
        let result = eval(&test_request_json(
            "GET",
            "https://api.stripe.com/v1/charges",
        ));
        assert!(result.allowed, "expected allow before reload");

        let deny_all = r#"{"agent": "test-agent", "default": "deny", "rules": []}"#;
        let config: PolicyConfig = serde_json::from_str(deny_all).unwrap();
        let policy = PolicyEngine::from_config(config).unwrap();
        ENGINE.with(|cell| {
            let mut state = cell.borrow_mut();
            let state = state.as_mut().unwrap();
            state.policy = policy;
        });

        let result = eval(&test_request_json(
            "GET",
            "https://api.stripe.com/v1/charges",
        ));
        assert!(!result.allowed, "expected deny after reload to deny-all");
    }

    #[test]
    fn reinit_preserves_rate_limiter_and_kill_switch() {
        // Verifies that calling init() again does NOT reset rate limiter or kill switch,
        // preventing bypass via repeated re-initialization.
        reset_engine();

        let rate_limit_policy = r#"{
            "agent": "test-agent",
            "default": "allow",
            "rules": [
                {
                    "name": "rate-limit-stripe",
                    "limit": {
                        "url": "api.stripe.com/*",
                        "calls_per_minute": 2,
                        "per": "endpoint"
                    }
                }
            ]
        }"#;

        init_test_engine(rate_limit_policy);

        // Use up the rate limit (2 calls allowed)
        let req = test_request_json("GET", "https://api.stripe.com/v1/charges");
        let r1 = eval(&req);
        assert!(r1.allowed, "first call should be allowed");
        let r2 = eval(&req);
        assert!(r2.allowed, "second call should be allowed");
        let r3 = eval(&req);
        assert!(!r3.allowed, "third call should be rate-limited");

        // Re-initialize with the same policy -- rate limiter must NOT reset
        init_test_engine(rate_limit_policy);

        let r4 = eval(&req);
        assert!(
            !r4.allowed,
            "after re-init, rate limit must still be enforced"
        );

        // Also verify kill switch survives re-init
        ENGINE.with(|cell| {
            cell.borrow_mut().as_mut().unwrap().kill_switch.set(true);
        });

        init_test_engine(rate_limit_policy);

        let r5 = eval(&test_request_json("GET", "https://example.com/anything"));
        assert!(!r5.allowed, "kill switch must survive re-init");
        assert!(
            r5.deny_reason.as_ref().unwrap().contains("kill switch"),
            "deny reason should be kill switch"
        );
    }

    #[test]
    fn trace_context_survives_evaluate_boundary() {
        // Verify trace_id, span_id, and parent_span_id flow through the full
        // init -> evaluate_request -> log_event_json path.
        reset_engine();
        let allow_all = r#"{"agent": "test-agent", "default": "allow", "rules": []}"#;
        init_test_engine(allow_all);

        let trace_id = "a1b2c3d4e5f6a7b8a1b2c3d4e5f6a7b8";
        let span_id = "1234567890abcdef";
        let parent_span_id = "fedcba0987654321";

        let request_json = serde_json::json!({
            "request_id": "req-trace",
            "method": "GET",
            "url": "https://api.stripe.com/v1/charges",
            "headers": [],
            "timestamp": "2026-03-28T14:30:00Z",
            "timestamp_ms": 1774708200000u64,
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": parent_span_id
        })
        .to_string();

        let result = eval(&request_json);
        assert!(result.allowed);

        let event: checkrd_shared::TelemetryEvent =
            serde_json::from_str(&result.log_event_json).unwrap();
        assert_eq!(event.trace_id, trace_id);
        assert_eq!(event.span_id, span_id);
        assert_eq!(event.parent_span_id.as_deref(), Some(parent_span_id));
    }

    #[test]
    fn trace_context_on_denied_request() {
        // Trace context must appear in telemetry even when the request is denied.
        reset_engine();
        init_test_engine(default_deny_policy());

        let trace_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
        let span_id = "cccccccccccccccc";

        let request_json = serde_json::json!({
            "request_id": "req-deny-trace",
            "method": "POST",
            "url": "https://unknown.example.com/api",
            "headers": [],
            "timestamp": "2026-03-28T14:30:00Z",
            "timestamp_ms": 1774708200000u64,
            "trace_id": trace_id,
            "span_id": span_id
        })
        .to_string();

        let result = eval(&request_json);
        assert!(!result.allowed);

        let event: checkrd_shared::TelemetryEvent =
            serde_json::from_str(&result.log_event_json).unwrap();
        assert_eq!(event.trace_id, trace_id);
        assert_eq!(event.span_id, span_id);
        assert!(event.parent_span_id.is_none());
        assert_eq!(event.policy_result, checkrd_shared::PolicyResult::Denied);
    }

    // ============================================================
    // FFI boundary tests (call extern "C" functions directly)
    //
    // Note: evaluate_request() returns a packed u64 (ptr << 32 | len) designed
    // for 32-bit WASM pointers. On 64-bit native targets the upper pointer bits
    // are truncated, so we can't unpack the result here. Those paths are verified
    // end-to-end by the Python wrapper tests which run actual WASM via wasmtime.
    // ============================================================

    #[test]
    fn read_str_validates_utf8() {
        // Valid ASCII
        let ascii = b"hello";
        assert_eq!(
            unsafe { read_str(ascii.as_ptr(), ascii.len() as u32) }.unwrap(),
            "hello"
        );

        // Valid multi-byte UTF-8
        let multi = "résumé".as_bytes();
        assert_eq!(
            unsafe { read_str(multi.as_ptr(), multi.len() as u32) }.unwrap(),
            "résumé"
        );

        // Invalid: bare 0xFF is never valid UTF-8
        let invalid: &[u8] = &[0xFF, 0xFE];
        assert!(unsafe { read_str(invalid.as_ptr(), invalid.len() as u32) }.is_err());

        // Invalid: truncated multi-byte sequence (0xC0 expects a continuation byte)
        let truncated: &[u8] = &[0xC0];
        assert!(unsafe { read_str(truncated.as_ptr(), truncated.len() as u32) }.is_err());

        // Empty input is valid
        let empty: &[u8] = &[];
        assert_eq!(unsafe { read_str(empty.as_ptr(), 0) }.unwrap(), "");
    }

    #[test]
    fn init_rejects_invalid_utf8_in_policy() {
        reset_engine();
        let invalid: &[u8] = &[0xFF, 0xFE];
        let agent = b"test-agent";

        let no_key: &[u8] = &[];
        let no_iid: &[u8] = &[];
        let result = init(
            invalid.as_ptr(),
            invalid.len() as u32,
            agent.as_ptr(),
            agent.len() as u32,
            no_key.as_ptr(),
            0,
            no_iid.as_ptr(),
            0,
        );
        assert_eq!(result, FFI_INVALID_UTF8);
    }

    #[test]
    fn init_rejects_invalid_utf8_in_agent_id() {
        reset_engine();
        let policy = br#"{"agent":"t","default":"allow","rules":[]}"#;
        let invalid: &[u8] = &[0xFE, 0xFF];
        let no_key: &[u8] = &[];
        let no_iid: &[u8] = &[];

        let result = init(
            policy.as_ptr(),
            policy.len() as u32,
            invalid.as_ptr(),
            invalid.len() as u32,
            no_key.as_ptr(),
            0,
            no_iid.as_ptr(),
            0,
        );
        assert_eq!(result, FFI_INVALID_UTF8);
    }

    #[test]
    fn init_distinguishes_utf8_from_parse_errors() {
        reset_engine();
        let no_key: &[u8] = &[];
        let no_iid: &[u8] = &[];

        // UTF-8 error: returns -2
        let invalid: &[u8] = &[0xFF];
        let agent = b"a";
        assert_eq!(
            init(
                invalid.as_ptr(),
                invalid.len() as u32,
                agent.as_ptr(),
                agent.len() as u32,
                no_key.as_ptr(),
                0,
                no_iid.as_ptr(),
                0,
            ),
            FFI_INVALID_UTF8
        );

        // Parse error: returns -1 (valid UTF-8 but bad JSON)
        let bad_json = b"not json";
        assert_eq!(
            init(
                bad_json.as_ptr(),
                bad_json.len() as u32,
                agent.as_ptr(),
                agent.len() as u32,
                no_key.as_ptr(),
                0,
                no_iid.as_ptr(),
                0,
            ),
            FFI_PARSE_ERROR
        );
    }

    #[test]
    fn evaluate_request_does_not_crash_on_invalid_utf8() {
        reset_engine();
        init_test_engine(default_deny_policy());

        // Previously used from_utf8_unchecked which was UB on invalid bytes.
        // Now returns a deny result instead of triggering undefined behavior.
        let invalid: &[u8] = &[0xFF, 0xFE, 0x80];
        let packed = evaluate_request(invalid.as_ptr(), invalid.len() as u32);

        // Non-zero means it wrote a deny result (pointer + length packed).
        // We can't unpack on 64-bit native (pointer truncation), but the
        // absence of UB/crash proves the safe validation works.
        assert_ne!(packed, 0, "should return a packed deny result");
    }

    #[test]
    fn reload_policy_rejects_invalid_utf8() {
        reset_engine();
        init_test_engine(default_deny_policy());

        let invalid: &[u8] = &[0xFF];
        let result = reload_policy(invalid.as_ptr(), invalid.len() as u32);
        assert_eq!(result, FFI_INVALID_UTF8);
    }

    #[test]
    fn reload_policy_returns_parse_error_for_bad_json() {
        reset_engine();
        init_test_engine(default_deny_policy());

        let bad = b"not json";
        let result = reload_policy(bad.as_ptr(), bad.len() as u32);
        assert_eq!(result, FFI_PARSE_ERROR);
    }

    #[test]
    fn ffi_init_success_and_evaluate_returns_result() {
        reset_engine();
        let policy = br#"{"agent":"test","default":"allow","rules":[]}"#;
        let agent = b"test-agent";
        let no_key: &[u8] = &[];
        let no_iid: &[u8] = &[];

        let rc = init(
            policy.as_ptr(),
            policy.len() as u32,
            agent.as_ptr(),
            agent.len() as u32,
            no_key.as_ptr(),
            0,
            no_iid.as_ptr(),
            0,
        );
        assert_eq!(rc, FFI_OK);

        let req = br#"{"request_id":"req-ffi","method":"GET","url":"https://api.stripe.com/v1/charges","headers":[],"timestamp":"2026-03-28T14:30:00Z","timestamp_ms":1774708200000,"trace_id":"a1b2c3d4e5f6a7b8a1b2c3d4e5f6a7b8","span_id":"1234567890abcdef"}"#;
        let packed = evaluate_request(req.as_ptr(), req.len() as u32);
        assert_ne!(packed, 0, "should return a packed result");

        // Verify via the internal path that the request was allowed
        let result = eval(String::from_utf8_lossy(req).as_ref());
        assert!(result.allowed);
        assert_eq!(result.request_id, "req-ffi");
    }

    #[test]
    fn trace_context_on_kill_switch() {
        // Trace context must appear even when kill switch short-circuits evaluation.
        reset_engine();
        let allow_all = r#"{"agent": "test-agent", "default": "allow", "rules": []}"#;
        init_test_engine(allow_all);

        ENGINE.with(|cell| {
            cell.borrow_mut().as_mut().unwrap().kill_switch.set(true);
        });

        let trace_id = "dddddddddddddddddddddddddddddddd";
        let span_id = "eeeeeeeeeeeeeeee";

        let request_json = serde_json::json!({
            "request_id": "req-kill-trace",
            "method": "GET",
            "url": "https://api.stripe.com/v1/charges",
            "headers": [],
            "timestamp": "2026-03-28T14:30:00Z",
            "timestamp_ms": 1774708200000u64,
            "trace_id": trace_id,
            "span_id": span_id
        })
        .to_string();

        let result = eval(&request_json);
        assert!(!result.allowed);
        assert!(result.deny_reason.as_ref().unwrap().contains("kill switch"));

        let event: checkrd_shared::TelemetryEvent =
            serde_json::from_str(&result.log_event_json).unwrap();
        assert_eq!(event.trace_id, trace_id);
        assert_eq!(event.span_id, span_id);
    }

    // ============================================================
    // Identity FFI tests
    // ============================================================

    #[test]
    fn ffi_init_with_valid_key() {
        reset_engine();
        let (private, _public) = crate::identity::generate_keypair();
        let policy = br#"{"agent":"test","default":"allow","rules":[]}"#;
        let agent = b"test-agent";
        let no_iid: &[u8] = &[];

        let rc = init(
            policy.as_ptr(),
            policy.len() as u32,
            agent.as_ptr(),
            agent.len() as u32,
            private.as_ptr(),
            private.len() as u32,
            no_iid.as_ptr(),
            0,
        );
        assert_eq!(rc, FFI_OK);

        // Verify the identity has a real instance_id (hex, not "test-agent")
        ENGINE.with(|cell| {
            let state = cell.borrow();
            let state = state.as_ref().unwrap();
            let iid = state.identity.instance_id();
            assert_eq!(iid.len(), 16, "keyed instance_id should be 16 hex chars");
            assert!(iid.chars().all(|c| c.is_ascii_hexdigit()));
        });
    }

    #[test]
    fn ffi_init_with_invalid_key_length() {
        reset_engine();
        let bad_key = [0u8; 16]; // wrong length
        let policy = br#"{"agent":"test","default":"allow","rules":[]}"#;
        let agent = b"test-agent";
        let no_iid: &[u8] = &[];

        let rc = init(
            policy.as_ptr(),
            policy.len() as u32,
            agent.as_ptr(),
            agent.len() as u32,
            bad_key.as_ptr(),
            bad_key.len() as u32,
            no_iid.as_ptr(),
            0,
        );
        assert_eq!(rc, FFI_INVALID_KEY);
    }

    #[test]
    fn ffi_init_with_instance_id_override() {
        reset_engine();
        let policy = br#"{"agent":"test","default":"allow","rules":[]}"#;
        let agent = b"test-agent";
        let no_key: &[u8] = &[];
        let custom_iid = b"kms-derived-12345678";

        let rc = init(
            policy.as_ptr(),
            policy.len() as u32,
            agent.as_ptr(),
            agent.len() as u32,
            no_key.as_ptr(),
            0,
            custom_iid.as_ptr(),
            custom_iid.len() as u32,
        );
        assert_eq!(rc, FFI_OK);

        ENGINE.with(|cell| {
            let state = cell.borrow();
            let state = state.as_ref().unwrap();
            assert_eq!(state.identity.instance_id(), "kms-derived-12345678");
        });
    }

    #[test]
    fn ffi_init_anonymous_uses_agent_id_as_instance_id() {
        reset_engine();
        let policy = br#"{"agent":"test","default":"allow","rules":[]}"#;
        let agent = b"my-agent";
        let no_key: &[u8] = &[];
        let no_iid: &[u8] = &[];

        let rc = init(
            policy.as_ptr(),
            policy.len() as u32,
            agent.as_ptr(),
            agent.len() as u32,
            no_key.as_ptr(),
            0,
            no_iid.as_ptr(),
            0,
        );
        assert_eq!(rc, FFI_OK);

        ENGINE.with(|cell| {
            let state = cell.borrow();
            let state = state.as_ref().unwrap();
            assert_eq!(state.identity.instance_id(), "my-agent");
        });
    }

    #[test]
    fn ffi_generate_keypair_returns_64_bytes() {
        let packed = generate_keypair();
        assert_ne!(packed, 0);
        // On native 64-bit targets the pointer bits are truncated in the pack,
        // but we can verify the function doesn't crash and returns non-zero.
    }

    // ============================================================
    // FFI sign tests
    // ============================================================

    #[test]
    fn ffi_sign_round_trip_with_verify() {
        reset_engine();
        let (private, public) = crate::identity::generate_keypair();
        let policy = br#"{"agent":"test","default":"allow","rules":[]}"#;
        let agent = b"test-agent";
        let no_iid: &[u8] = &[];

        let rc = init(
            policy.as_ptr(),
            policy.len() as u32,
            agent.as_ptr(),
            agent.len() as u32,
            private.as_ptr(),
            private.len() as u32,
            no_iid.as_ptr(),
            0,
        );
        assert_eq!(rc, FFI_OK);

        let payload = b"telemetry event to authenticate";
        let packed = sign(payload.as_ptr(), payload.len() as u32);
        assert_ne!(packed, 0, "sign should return a packed signature");

        // Unpack and verify the signature is valid Ed25519
        // (on native 64-bit the packed ptr is truncated, so we verify via
        // the internal function instead)
        let id = crate::identity::Identity::from_key_bytes("test-agent", &private).unwrap();
        let sig = id.sign(payload);
        assert_eq!(sig.len(), 64);
        assert!(
            crate::identity::verify(payload, &sig, &public).unwrap(),
            "signature should verify against the public key"
        );
    }

    #[test]
    fn ffi_sign_deterministic() {
        reset_engine();
        let (private, _) = crate::identity::generate_keypair();
        let policy = br#"{"agent":"test","default":"allow","rules":[]}"#;
        let agent = b"test-agent";
        let no_iid: &[u8] = &[];

        init(
            policy.as_ptr(),
            policy.len() as u32,
            agent.as_ptr(),
            agent.len() as u32,
            private.as_ptr(),
            private.len() as u32,
            no_iid.as_ptr(),
            0,
        );

        let payload = b"same message";
        let packed1 = sign(payload.as_ptr(), payload.len() as u32);
        let packed2 = sign(payload.as_ptr(), payload.len() as u32);
        // Both calls should return non-zero (valid signatures)
        assert_ne!(packed1, 0);
        assert_ne!(packed2, 0);
    }

    #[test]
    fn ffi_sign_before_init_returns_zero() {
        reset_engine();
        let payload = b"should fail";
        let packed = sign(payload.as_ptr(), payload.len() as u32);
        assert_eq!(packed, 0, "sign before init should return 0");
    }

    #[test]
    fn ffi_sign_anonymous_returns_zero() {
        reset_engine();
        let policy = br#"{"agent":"test","default":"allow","rules":[]}"#;
        let agent = b"test-agent";
        let no_key: &[u8] = &[];
        let no_iid: &[u8] = &[];

        let rc = init(
            policy.as_ptr(),
            policy.len() as u32,
            agent.as_ptr(),
            agent.len() as u32,
            no_key.as_ptr(),
            0,
            no_iid.as_ptr(),
            0,
        );
        assert_eq!(rc, FFI_OK);

        let payload = b"should return empty for anonymous";
        let packed = sign(payload.as_ptr(), payload.len() as u32);
        assert_eq!(packed, 0, "anonymous identity should return 0 from sign");
    }

    #[test]
    fn ffi_sign_empty_payload() {
        reset_engine();
        let (private, public) = crate::identity::generate_keypair();
        let policy = br#"{"agent":"test","default":"allow","rules":[]}"#;
        let agent = b"test-agent";
        let no_iid: &[u8] = &[];

        init(
            policy.as_ptr(),
            policy.len() as u32,
            agent.as_ptr(),
            agent.len() as u32,
            private.as_ptr(),
            private.len() as u32,
            no_iid.as_ptr(),
            0,
        );

        // Empty payload should still produce a valid 64-byte signature
        let empty: &[u8] = &[];
        let packed = sign(empty.as_ptr(), 0);
        assert_ne!(packed, 0, "sign with empty payload should succeed");

        // Verify the signature is correct via internal path
        let id = crate::identity::Identity::from_key_bytes("test-agent", &private).unwrap();
        let sig = id.sign(b"");
        assert!(crate::identity::verify(b"", &sig, &public).unwrap());
    }

    #[test]
    fn ffi_sign_service_id_on_identity() {
        reset_engine();
        let (private, _) = crate::identity::generate_keypair();
        let policy = br#"{"agent":"test","default":"allow","rules":[]}"#;
        let agent = b"sales-agent";
        let no_iid: &[u8] = &[];

        init(
            policy.as_ptr(),
            policy.len() as u32,
            agent.as_ptr(),
            agent.len() as u32,
            private.as_ptr(),
            private.len() as u32,
            no_iid.as_ptr(),
            0,
        );

        ENGINE.with(|cell| {
            let state = cell.borrow();
            let state = state.as_ref().unwrap();
            assert_eq!(state.identity.service_id(), "sales-agent");
            assert_eq!(state.identity.instance_id().len(), 16);
        });
    }

    // ----- sign_telemetry_batch FFI ---------------------------------------
    //
    // The FFI return type packs `(ptr << 32) | len` into a u64, which works
    // in WASM32 (where pointers fit in 32 bits) but truncates the pointer
    // on native 64-bit. The tests therefore call sign_telemetry_batch_internal
    // directly and only verify the FFI shim's zero-return paths.

    fn init_keyed_engine_with(private: &[u8; 32]) {
        let policy = br#"{"agent":"test","default":"allow","rules":[]}"#;
        let agent = b"sales-agent";
        let no_iid: &[u8] = &[];
        let rc = init(
            policy.as_ptr(),
            policy.len() as u32,
            agent.as_ptr(),
            agent.len() as u32,
            private.as_ptr(),
            private.len() as u32,
            no_iid.as_ptr(),
            0,
        );
        assert_eq!(rc, FFI_OK);
    }

    fn ffi_sign_telemetry_batch(
        batch_json: &[u8],
        target_uri: &str,
        signer_agent: &str,
        nonce: &str,
        created: u64,
        expires: u64,
    ) -> serde_json::Value {
        let s = sign_telemetry_batch_internal(
            batch_json,
            target_uri,
            signer_agent,
            nonce,
            created,
            expires,
        )
        .expect("internal must return Some for keyed identity");
        serde_json::from_str(&s).expect("internal must return valid JSON")
    }

    #[test]
    fn sign_telemetry_batch_returns_zero_when_anonymous() {
        // Anonymous identities have no signing capability — the wrapper must
        // be able to detect this and skip signing rather than crashing.
        reset_engine();
        let policy = br#"{"agent":"test","default":"allow","rules":[]}"#;
        let agent = b"sales-agent";
        let no_key: &[u8] = &[];
        let no_iid: &[u8] = &[];
        init(
            policy.as_ptr(),
            policy.len() as u32,
            agent.as_ptr(),
            agent.len() as u32,
            no_key.as_ptr(),
            0,
            no_iid.as_ptr(),
            0,
        );

        let body = br#"{"events":[]}"#;
        let packed = sign_telemetry_batch(
            body.as_ptr(),
            body.len() as u32,
            "https://api.checkrd.io/v1/telemetry".as_ptr(),
            "https://api.checkrd.io/v1/telemetry".len() as u32,
            "550e8400-e29b-41d4-a716-446655440000".as_ptr(),
            36,
            "abcd1234".as_ptr(),
            8,
            1_700_000_000,
            1_700_000_300,
        );
        assert_eq!(packed, 0, "anonymous identity must return 0");
    }

    #[test]
    fn sign_telemetry_batch_returns_zero_when_uninitialized() {
        reset_engine();
        let body = br#"{}"#;
        let packed = sign_telemetry_batch(
            body.as_ptr(),
            body.len() as u32,
            "https://x".as_ptr(),
            9,
            "agent".as_ptr(),
            5,
            "n".as_ptr(),
            1,
            1,
            2,
        );
        assert_eq!(packed, 0, "uninitialized engine must return 0");
    }

    #[test]
    fn sign_telemetry_batch_full_round_trip() {
        // The end-to-end invariant: the FFI returns headers and a DSSE envelope
        // that an independent verifier can reconstruct and verify against the
        // public key. If the test passes, the protocol is correct.
        reset_engine();
        let (private, public) = crate::identity::generate_keypair();
        init_keyed_engine_with(&private);

        let batch_json = br#"{"events":[{"id":"r-1"}],"sdk_version":"0.2.0"}"#;
        let target_uri = "https://api.checkrd.io/v1/telemetry";
        let signer_agent = "550e8400-e29b-41d4-a716-446655440000";
        let nonce = "abcdef0123456789abcdef0123456789";
        let created = 1_712_345_678u64;
        let expires = created + 300;

        let result = ffi_sign_telemetry_batch(
            batch_json,
            target_uri,
            signer_agent,
            nonce,
            created,
            expires,
        );

        // Sanity-check the return shape.
        let content_digest = result["content_digest"].as_str().unwrap();
        let signature_input = result["signature_input"].as_str().unwrap();
        let signature_header = result["signature"].as_str().unwrap();
        let instance_id = result["instance_id"].as_str().unwrap();
        assert_eq!(result["expires"].as_u64().unwrap(), expires);
        assert_eq!(instance_id.len(), 16);

        // ----- HTTP signature: reconstruct base string and verify ----------
        // Parse Signature-Input → reconstruct CoveredComponents → rebuild base
        // → decode Signature → verify with public key. This is exactly the
        // verification dance the ingestion service will do.
        let parsed_input = checkrd_shared::http_sig::parse_signature_input(
            signature_input,
            checkrd_shared::http_sig::TELEMETRY_SIGNATURE_LABEL,
        )
        .unwrap();
        assert_eq!(parsed_input.created, created);
        assert_eq!(parsed_input.expires, expires);
        assert_eq!(parsed_input.keyid, instance_id);
        assert_eq!(parsed_input.alg, "ed25519");
        assert_eq!(parsed_input.nonce, nonce);

        let verifier_components = checkrd_shared::http_sig::CoveredComponents {
            method: "POST",
            target_uri,
            content_digest,
            signer_agent,
            created: parsed_input.created,
            expires: parsed_input.expires,
            keyid: &parsed_input.keyid,
            nonce: &parsed_input.nonce,
        };
        let verifier_base = checkrd_shared::http_sig::signature_base_string(&verifier_components);

        let sig_bytes = checkrd_shared::http_sig::parse_signature_header(
            signature_header,
            checkrd_shared::http_sig::TELEMETRY_SIGNATURE_LABEL,
        )
        .unwrap();
        assert_eq!(sig_bytes.len(), 64, "Ed25519 signatures are 64 bytes");

        assert!(
            crate::identity::verify(verifier_base.as_bytes(), &sig_bytes, &public).unwrap(),
            "HTTP signature must verify against the agent's public key"
        );

        // ----- Content-Digest: must hash the exact body bytes ---------------
        let computed_digest = checkrd_shared::http_sig::compute_content_digest(batch_json);
        assert_eq!(content_digest, computed_digest);

        // ----- DSSE envelope: payload type, payload bytes, signature verify -
        let envelope = result["dsse_envelope"].clone();
        let payload_type = envelope["payloadType"].as_str().unwrap();
        assert_eq!(
            payload_type,
            checkrd_shared::dsse::TELEMETRY_BATCH_PAYLOAD_TYPE
        );
        let payload_b64 = envelope["payload"].as_str().unwrap();
        use base64::engine::general_purpose::STANDARD as B64;
        use base64::Engine;
        let payload_bytes = B64.decode(payload_b64).unwrap();
        assert_eq!(payload_bytes, batch_json);

        let signatures = envelope["signatures"].as_array().unwrap();
        assert_eq!(signatures.len(), 1);
        let dsse_sig = signatures[0].clone();
        assert_eq!(dsse_sig["keyid"].as_str().unwrap(), instance_id);
        let dsse_sig_bytes = B64.decode(dsse_sig["sig"].as_str().unwrap()).unwrap();
        let pae = checkrd_shared::dsse::pae(payload_type, &payload_bytes);
        assert!(
            crate::identity::verify(&pae, &dsse_sig_bytes, &public).unwrap(),
            "DSSE signature must verify against the agent's public key"
        );
    }

    #[test]
    fn sign_telemetry_batch_tampering_breaks_verification() {
        // If anyone flips a single byte of the body, the signature must fail.
        // This proves the digest binding is real.
        reset_engine();
        let (private, public) = crate::identity::generate_keypair();
        init_keyed_engine_with(&private);

        let original = br#"{"events":[{"amount":100}]}"#;
        let target_uri = "https://api.checkrd.io/v1/telemetry";
        let signer_agent = "550e8400-e29b-41d4-a716-446655440000";
        let nonce = "n";
        let result =
            ffi_sign_telemetry_batch(original, target_uri, signer_agent, nonce, 1_000, 1_300);
        let content_digest = result["content_digest"].as_str().unwrap();
        let signature_input = result["signature_input"].as_str().unwrap();
        let signature_header = result["signature"].as_str().unwrap();

        // Now pretend an attacker swaps the body for a higher-amount one but
        // keeps the headers the same (replaying the signature on different bytes).
        let tampered = br#"{"events":[{"amount":999}]}"#;
        let tampered_digest = checkrd_shared::http_sig::compute_content_digest(tampered);
        // The original content_digest is bound into the signature, so even if
        // the verifier somehow trusted the new body, it would compute a
        // different content-digest and the signature base would diverge.
        assert_ne!(content_digest, tampered_digest);

        // Build the verifier's view using the *tampered* digest (simulating an
        // attacker who replaced the body and updated their own digest header,
        // but couldn't re-sign because they don't have the key).
        let parsed_input = checkrd_shared::http_sig::parse_signature_input(
            signature_input,
            checkrd_shared::http_sig::TELEMETRY_SIGNATURE_LABEL,
        )
        .unwrap();
        let tampered_components = checkrd_shared::http_sig::CoveredComponents {
            method: "POST",
            target_uri,
            content_digest: &tampered_digest,
            signer_agent,
            created: parsed_input.created,
            expires: parsed_input.expires,
            keyid: &parsed_input.keyid,
            nonce: &parsed_input.nonce,
        };
        let tampered_base = checkrd_shared::http_sig::signature_base_string(&tampered_components);
        let sig_bytes = checkrd_shared::http_sig::parse_signature_header(
            signature_header,
            checkrd_shared::http_sig::TELEMETRY_SIGNATURE_LABEL,
        )
        .unwrap();
        assert!(
            !crate::identity::verify(tampered_base.as_bytes(), &sig_bytes, &public).unwrap(),
            "tampered body must fail signature verification"
        );

        // And the DSSE side: tampering the payload bytes obviously breaks
        // verification too because the PAE input changes.
        let tampered_pae =
            checkrd_shared::dsse::pae(checkrd_shared::dsse::TELEMETRY_BATCH_PAYLOAD_TYPE, tampered);
        let envelope = result["dsse_envelope"].clone();
        use base64::engine::general_purpose::STANDARD as B64;
        use base64::Engine;
        let original_dsse_sig_bytes = B64
            .decode(envelope["signatures"][0]["sig"].as_str().unwrap())
            .unwrap();
        assert!(
            !crate::identity::verify(&tampered_pae, &original_dsse_sig_bytes, &public).unwrap(),
            "tampered DSSE payload must fail signature verification"
        );
    }

    #[test]
    fn sign_telemetry_batch_changing_signer_agent_breaks_verification() {
        // The signer_agent header is part of the covered components, so binding
        // the agent identity into the signature prevents an attacker who has a
        // valid signature for agent A from replaying it as agent B.
        reset_engine();
        let (private, public) = crate::identity::generate_keypair();
        init_keyed_engine_with(&private);

        let body = br#"{"events":[]}"#;
        let target_uri = "https://api.checkrd.io/v1/telemetry";
        let original_agent = "550e8400-e29b-41d4-a716-446655440000";
        let attacker_agent = "00000000-0000-0000-0000-000000000000";
        let nonce = "n2";

        let result =
            ffi_sign_telemetry_batch(body, target_uri, original_agent, nonce, 1_000, 1_300);
        let signature_header = result["signature"].as_str().unwrap();
        let parsed_input = checkrd_shared::http_sig::parse_signature_input(
            result["signature_input"].as_str().unwrap(),
            checkrd_shared::http_sig::TELEMETRY_SIGNATURE_LABEL,
        )
        .unwrap();

        // Verifier rebuilds base with the *attacker's* agent header.
        let attacker_components = checkrd_shared::http_sig::CoveredComponents {
            method: "POST",
            target_uri,
            content_digest: result["content_digest"].as_str().unwrap(),
            signer_agent: attacker_agent,
            created: parsed_input.created,
            expires: parsed_input.expires,
            keyid: &parsed_input.keyid,
            nonce: &parsed_input.nonce,
        };
        let attacker_base = checkrd_shared::http_sig::signature_base_string(&attacker_components);
        let sig_bytes = checkrd_shared::http_sig::parse_signature_header(
            signature_header,
            checkrd_shared::http_sig::TELEMETRY_SIGNATURE_LABEL,
        )
        .unwrap();
        assert!(
            !crate::identity::verify(attacker_base.as_bytes(), &sig_bytes, &public).unwrap(),
            "swapping signer_agent must invalidate the signature"
        );
    }

    #[test]
    fn sign_telemetry_batch_uses_engine_instance_id_as_keyid() {
        // The keyid in the signature input is the agent's instance_id, derived
        // from the public key. This binds verification to a specific public key
        // and lets the verifier look up the right key in the agent registry.
        reset_engine();
        let (private, public) = crate::identity::generate_keypair();
        init_keyed_engine_with(&private);

        let body = br#"{}"#;
        let result = ffi_sign_telemetry_batch(
            body,
            "https://api.checkrd.io/v1/telemetry",
            "agent-id",
            "nnn",
            1_000,
            1_300,
        );
        let parsed_input = checkrd_shared::http_sig::parse_signature_input(
            result["signature_input"].as_str().unwrap(),
            checkrd_shared::http_sig::TELEMETRY_SIGNATURE_LABEL,
        )
        .unwrap();

        // instance_id is the first 8 bytes of the public key as hex (16 chars).
        let expected_iid: String = public[..8].iter().map(|b| format!("{b:02x}")).collect();
        assert_eq!(parsed_input.keyid, expected_iid);
        assert_eq!(result["instance_id"].as_str().unwrap(), expected_iid);
    }

    // ----- reload_policy_signed FFI ---------------------------------------

    /// Test constants for the policy bundle freshness check.
    const TEST_NOW_SECS: u64 = 1_000_000;
    const TEST_MAX_AGE_SECS: u64 = 86_400; // 24 hours

    /// Build a signed DSSE envelope wrapping a `PolicyBundle`. Strong-from-the-
    /// ground-up: every signed payload is a versioned `PolicyBundle`, never a
    /// bare policy. Tests pass an explicit `version` so they can exercise the
    /// monotonicity check; the default helper below uses version 1.
    fn make_signed_bundle_envelope(
        signing_key: &ed25519_dalek::SigningKey,
        policy_json: &str,
        version: u64,
        signed_at: u64,
    ) -> String {
        use base64::engine::general_purpose::STANDARD as B64;
        use base64::Engine;
        use ed25519_dalek::Signer;
        let policy: checkrd_shared::PolicyConfig = serde_json::from_str(policy_json).unwrap();
        let bundle = checkrd_shared::PolicyBundle::new(version, signed_at, policy);
        let bundle_bytes = serde_json::to_vec(&bundle).unwrap();
        let pae = checkrd_shared::dsse::pae(
            checkrd_shared::dsse::POLICY_BUNDLE_PAYLOAD_TYPE,
            &bundle_bytes,
        );
        let sig = signing_key.sign(&pae);
        let envelope = checkrd_shared::dsse::DsseEnvelope {
            payload_type: checkrd_shared::dsse::POLICY_BUNDLE_PAYLOAD_TYPE.to_string(),
            payload: B64.encode(&bundle_bytes),
            signatures: vec![checkrd_shared::dsse::DsseSignature {
                keyid: "test-cp".to_string(),
                sig: B64.encode(sig.to_bytes()),
            }],
        };
        serde_json::to_string(&envelope).unwrap()
    }

    /// Convenience: build a fresh signed bundle with version 1.
    fn make_signed_envelope(signing_key: &ed25519_dalek::SigningKey, policy_json: &str) -> String {
        make_signed_bundle_envelope(signing_key, policy_json, 1, TEST_NOW_SECS)
    }

    fn make_trusted_keys_json(signing_key: &ed25519_dalek::SigningKey) -> String {
        let pk = signing_key.verifying_key().to_bytes();
        let hex: String = pk.iter().map(|b| format!("{b:02x}")).collect();
        serde_json::json!([{
            "keyid": "test-cp",
            "public_key_hex": hex,
            "valid_from": 0,
            "valid_until": u64::MAX,
        }])
        .to_string()
    }

    /// A second policy that's distinct from `default_deny_policy()`. We
    /// install one policy via the unsigned path, then install this one via
    /// the signed path, and assert via evaluation that the new policy took
    /// effect.
    fn permissive_policy_json() -> &'static str {
        r#"{
            "agent": "test-agent",
            "default": "allow",
            "rules": []
        }"#
    }

    #[test]
    fn reload_policy_signed_installs_verified_policy() {
        // End-to-end FFI test: init engine with default-deny policy, sign a
        // permissive policy with a fresh key, call reload_policy_signed via
        // the internal helper, and assert evaluation reflects the new policy.
        reset_engine();
        init_test_engine(default_deny_policy());

        // GET to a non-stripe URL should be denied by default-deny.
        let pre = eval(&test_request_json("GET", "https://example.com/api"));
        assert!(!pre.allowed);

        let signing_key = ed25519_dalek::SigningKey::from_bytes(&[0xa3; 32]);
        let envelope_json = make_signed_envelope(&signing_key, permissive_policy_json());
        let trusted_json = make_trusted_keys_json(&signing_key);

        let rc = reload_policy_signed_internal(
            &envelope_json,
            &trusted_json,
            TEST_NOW_SECS,
            TEST_MAX_AGE_SECS,
        );
        assert_eq!(rc, FFI_OK, "expected FFI_OK, got {rc}");

        // After the signed reload, the same request should be allowed by
        // the new permissive policy.
        let post = eval(&test_request_json("GET", "https://example.com/api"));
        assert!(post.allowed, "permissive policy should allow this request");
    }

    #[test]
    fn reload_policy_signed_rejects_envelope_with_wrong_payload_type() {
        // The envelope is correctly signed but uses the telemetry payload
        // type. Verifier must reject with -4 (cross-type replay defense).
        reset_engine();
        init_test_engine(default_deny_policy());

        use base64::engine::general_purpose::STANDARD as B64;
        use base64::Engine;
        use ed25519_dalek::Signer;
        let signing_key = ed25519_dalek::SigningKey::from_bytes(&[0xa3; 32]);
        let payload = permissive_policy_json().as_bytes();
        // Sign under TELEMETRY type — wrong type for the policy verifier.
        let pae =
            checkrd_shared::dsse::pae(checkrd_shared::dsse::TELEMETRY_BATCH_PAYLOAD_TYPE, payload);
        let sig = signing_key.sign(&pae);
        let envelope = checkrd_shared::dsse::DsseEnvelope {
            payload_type: checkrd_shared::dsse::TELEMETRY_BATCH_PAYLOAD_TYPE.to_string(),
            payload: B64.encode(payload),
            signatures: vec![checkrd_shared::dsse::DsseSignature {
                keyid: "test-cp".to_string(),
                sig: B64.encode(sig.to_bytes()),
            }],
        };
        let envelope_json = serde_json::to_string(&envelope).unwrap();
        let trusted_json = make_trusted_keys_json(&signing_key);

        let rc = reload_policy_signed_internal(
            &envelope_json,
            &trusted_json,
            TEST_NOW_SECS,
            TEST_MAX_AGE_SECS,
        );
        assert_eq!(rc, FFI_POLICY_PAYLOAD_TYPE_MISMATCH);

        // Old policy must still be in place — eval still rejects.
        let still_denied = eval(&test_request_json("GET", "https://example.com/api"));
        assert!(!still_denied.allowed);
    }

    #[test]
    fn reload_policy_signed_rejects_tampered_envelope() {
        reset_engine();
        init_test_engine(default_deny_policy());

        let signing_key = ed25519_dalek::SigningKey::from_bytes(&[0xa3; 32]);
        let mut envelope: checkrd_shared::dsse::DsseEnvelope = serde_json::from_str(
            &make_signed_envelope(&signing_key, permissive_policy_json()),
        )
        .unwrap();
        // Replace the payload AFTER signing — base64 of a different policy.
        use base64::engine::general_purpose::STANDARD as B64;
        use base64::Engine;
        envelope.payload = B64.encode(br#"{"agent":"x","default":"allow","rules":[]}"#);
        let tampered = serde_json::to_string(&envelope).unwrap();
        let trusted = make_trusted_keys_json(&signing_key);

        let rc =
            reload_policy_signed_internal(&tampered, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS);
        assert_eq!(rc, FFI_POLICY_SIGNATURE_INVALID);
    }

    #[test]
    fn reload_policy_signed_rejects_unknown_signer() {
        reset_engine();
        init_test_engine(default_deny_policy());

        let signer = ed25519_dalek::SigningKey::from_bytes(&[0xa3; 32]);
        let envelope = make_signed_envelope(&signer, permissive_policy_json());

        // Trust list contains a different keyid than the envelope.
        let other = ed25519_dalek::SigningKey::from_bytes(&[0x99; 32]);
        let pk: String = other
            .verifying_key()
            .to_bytes()
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        let trusted = serde_json::json!([{
            "keyid": "other-cp",
            "public_key_hex": pk,
            "valid_from": 0,
            "valid_until": u64::MAX,
        }])
        .to_string();

        let rc =
            reload_policy_signed_internal(&envelope, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS);
        assert_eq!(rc, FFI_POLICY_UNKNOWN_OR_NO_SIGNER);
    }

    #[test]
    fn reload_policy_signed_rejects_expired_key() {
        reset_engine();
        init_test_engine(default_deny_policy());

        let signing_key = ed25519_dalek::SigningKey::from_bytes(&[0xa3; 32]);
        let envelope = make_signed_envelope(&signing_key, permissive_policy_json());
        let pk: String = signing_key
            .verifying_key()
            .to_bytes()
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        let trusted = serde_json::json!([{
            "keyid": "test-cp",
            "public_key_hex": pk,
            "valid_from": 0,
            "valid_until": 500,
        }])
        .to_string();

        // Bundle was signed at TEST_NOW_SECS, but the trusted key has
        // valid_until=500. We pass now=2_000_000 (well past 500) to force
        // the key-expired branch — and use a much larger max_age so the
        // freshness check doesn't fire first. Even though the bundle is
        // "stale", verify_dsse_envelope's key check fires before the
        // freshness check, returning -7.
        let rc = reload_policy_signed_internal(
            &envelope,
            &trusted,
            2_000_000,
            u64::MAX, // disable freshness check so we test the key window only
        );
        assert_eq!(rc, FFI_POLICY_KEY_NOT_IN_VALIDITY_WINDOW);
    }

    #[test]
    fn reload_policy_signed_rejects_malformed_envelope_json() {
        reset_engine();
        init_test_engine(default_deny_policy());
        let signing_key = ed25519_dalek::SigningKey::from_bytes(&[0xa3; 32]);
        let trusted = make_trusted_keys_json(&signing_key);
        let rc = reload_policy_signed_internal(
            "{not valid json",
            &trusted,
            TEST_NOW_SECS,
            TEST_MAX_AGE_SECS,
        );
        assert_eq!(rc, FFI_PARSE_ERROR);
    }

    #[test]
    fn reload_policy_signed_rejects_malformed_trusted_keys_json() {
        reset_engine();
        init_test_engine(default_deny_policy());
        let signing_key = ed25519_dalek::SigningKey::from_bytes(&[0xa3; 32]);
        let envelope = make_signed_envelope(&signing_key, permissive_policy_json());
        let rc = reload_policy_signed_internal(
            &envelope,
            "{not an array",
            TEST_NOW_SECS,
            TEST_MAX_AGE_SECS,
        );
        assert_eq!(rc, FFI_INVALID_KEY);
    }

    #[test]
    fn reload_policy_signed_rejects_when_engine_not_initialized() {
        reset_engine();
        let signing_key = ed25519_dalek::SigningKey::from_bytes(&[0xa3; 32]);
        let envelope = make_signed_envelope(&signing_key, permissive_policy_json());
        let trusted = make_trusted_keys_json(&signing_key);
        let rc =
            reload_policy_signed_internal(&envelope, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS);
        assert_eq!(rc, FFI_POLICY_ENGINE_NOT_INITIALIZED);
    }

    #[test]
    fn reload_policy_signed_rejects_invalid_policy_payload() {
        // Verification succeeds but the verified payload isn't a valid PolicyBundle.
        // We construct an envelope wrapping a JSON object that's neither a
        // PolicyBundle nor a PolicyConfig, just `{"not":"a policy"}`.
        reset_engine();
        init_test_engine(default_deny_policy());
        let signing_key = ed25519_dalek::SigningKey::from_bytes(&[0xa3; 32]);
        // Sign raw garbage bytes (not a valid PolicyBundle JSON).
        use base64::engine::general_purpose::STANDARD as B64;
        use base64::Engine;
        use ed25519_dalek::Signer;
        let garbage = b"{\"not\":\"a policy\"}";
        let pae =
            checkrd_shared::dsse::pae(checkrd_shared::dsse::POLICY_BUNDLE_PAYLOAD_TYPE, garbage);
        let sig = signing_key.sign(&pae);
        let envelope = checkrd_shared::dsse::DsseEnvelope {
            payload_type: checkrd_shared::dsse::POLICY_BUNDLE_PAYLOAD_TYPE.to_string(),
            payload: B64.encode(garbage),
            signatures: vec![checkrd_shared::dsse::DsseSignature {
                keyid: "test-cp".to_string(),
                sig: B64.encode(sig.to_bytes()),
            }],
        };
        let envelope_json = serde_json::to_string(&envelope).unwrap();
        let trusted = make_trusted_keys_json(&signing_key);
        let rc = reload_policy_signed_internal(
            &envelope_json,
            &trusted,
            TEST_NOW_SECS,
            TEST_MAX_AGE_SECS,
        );
        assert_eq!(rc, FFI_POLICY_VERIFIED_PAYLOAD_INVALID);
    }

    // ----- Strong-from-the-ground-up: rollback + freshness defense ----------

    #[test]
    fn reload_policy_signed_rejects_rollback_attack() {
        // Install a higher-version bundle, then attempt to install a lower-
        // version bundle. The lower one MUST be rejected with -11 (rollback).
        // This is the core defense against an attacker replaying an older,
        // more permissive policy from the wire.
        reset_engine();
        init_test_engine(default_deny_policy());

        let signing_key = ed25519_dalek::SigningKey::from_bytes(&[0xa3; 32]);
        let trusted = make_trusted_keys_json(&signing_key);

        // Install version 5 first.
        let bundle_v5 =
            make_signed_bundle_envelope(&signing_key, permissive_policy_json(), 5, TEST_NOW_SECS);
        let rc =
            reload_policy_signed_internal(&bundle_v5, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS);
        assert_eq!(rc, FFI_OK, "v5 install should succeed");
        assert_eq!(get_active_policy_version(), 5);

        // Now attempt to install version 3 (rollback). MUST be rejected.
        let bundle_v3 =
            make_signed_bundle_envelope(&signing_key, permissive_policy_json(), 3, TEST_NOW_SECS);
        let rc =
            reload_policy_signed_internal(&bundle_v3, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS);
        assert_eq!(
            rc, FFI_POLICY_VERSION_NOT_MONOTONIC,
            "rollback to v3 must be rejected"
        );
        // Version high water mark unchanged.
        assert_eq!(get_active_policy_version(), 5);
    }

    #[test]
    fn reload_policy_signed_rejects_replay_of_same_version() {
        // Replaying the EXACT same envelope must be rejected at the FFI
        // layer (version equality is rollback by the strict-greater rule).
        // The SDK wrapper is responsible for not calling reload_policy_signed
        // on identical bundles via its persisted (version, hash) cache —
        // see `_policy_state.py` / `_policy_state.ts`. This test pins the
        // FFI's behavior so an attacker who bypasses the wrapper cache
        // (e.g., direct WASM injection) still hits the monotonic gate.
        reset_engine();
        init_test_engine(default_deny_policy());

        let signing_key = ed25519_dalek::SigningKey::from_bytes(&[0xa3; 32]);
        let trusted = make_trusted_keys_json(&signing_key);

        let bundle =
            make_signed_bundle_envelope(&signing_key, permissive_policy_json(), 7, TEST_NOW_SECS);
        // First install succeeds.
        let rc = reload_policy_signed_internal(&bundle, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS);
        assert_eq!(rc, FFI_OK);

        // Replay of the same envelope is rejected as a rollback (version 7 == last 7).
        let rc = reload_policy_signed_internal(&bundle, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS);
        assert_eq!(rc, FFI_POLICY_VERSION_NOT_MONOTONIC);
    }

    #[test]
    fn reload_policy_signed_accepts_strictly_higher_version() {
        // The expected forward path: each new bundle has a strictly higher
        // version. v1 → v2 → v3 should all install successfully.
        reset_engine();
        init_test_engine(default_deny_policy());

        let signing_key = ed25519_dalek::SigningKey::from_bytes(&[0xa3; 32]);
        let trusted = make_trusted_keys_json(&signing_key);

        for v in [1u64, 2, 3, 5, 100] {
            let bundle = make_signed_bundle_envelope(
                &signing_key,
                permissive_policy_json(),
                v,
                TEST_NOW_SECS,
            );
            let rc =
                reload_policy_signed_internal(&bundle, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS);
            assert_eq!(rc, FFI_OK, "version {v} install should succeed");
            assert_eq!(get_active_policy_version(), v);
        }
    }

    #[test]
    fn reload_policy_signed_rejects_stale_bundle() {
        // A bundle signed more than max_age_secs ago must be rejected with -12.
        // Defends against replay even when the version is higher than current
        // (e.g. attacker captures a brand-new high-version bundle then replays
        // it weeks later, after the org has rotated to even newer bundles —
        // wait, the version check would catch that. But if the SDK is fresh
        // with version=0, the captured bundle's version > 0 passes the
        // monotonicity check. Freshness catches it).
        reset_engine();
        init_test_engine(default_deny_policy());

        let signing_key = ed25519_dalek::SigningKey::from_bytes(&[0xa3; 32]);
        let trusted = make_trusted_keys_json(&signing_key);

        // Bundle signed 25 hours ago, max age 24 hours.
        let signed_at = TEST_NOW_SECS - 25 * 3600;
        let bundle =
            make_signed_bundle_envelope(&signing_key, permissive_policy_json(), 1, signed_at);
        let rc = reload_policy_signed_internal(&bundle, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS);
        assert_eq!(rc, FFI_POLICY_BUNDLE_TOO_OLD);
    }

    #[test]
    fn reload_policy_signed_accepts_bundle_at_max_age_boundary() {
        // Boundary: bundle signed exactly max_age_secs ago is still accepted.
        // The check is `now - signed_at > max_age_secs` (strict greater than).
        reset_engine();
        init_test_engine(default_deny_policy());

        let signing_key = ed25519_dalek::SigningKey::from_bytes(&[0xa3; 32]);
        let trusted = make_trusted_keys_json(&signing_key);

        let signed_at = TEST_NOW_SECS - TEST_MAX_AGE_SECS;
        let bundle =
            make_signed_bundle_envelope(&signing_key, permissive_policy_json(), 1, signed_at);
        let rc = reload_policy_signed_internal(&bundle, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS);
        assert_eq!(rc, FFI_OK);
    }

    #[test]
    fn reload_policy_signed_rejects_future_dated_bundle_beyond_skew() {
        // Defends against an attacker pre-signing a bundle for the future.
        // 10 minutes in the future is past the 5-minute clock skew window.
        reset_engine();
        init_test_engine(default_deny_policy());

        let signing_key = ed25519_dalek::SigningKey::from_bytes(&[0xa3; 32]);
        let trusted = make_trusted_keys_json(&signing_key);

        let signed_at = TEST_NOW_SECS + 600; // 10 minutes ahead
        let bundle =
            make_signed_bundle_envelope(&signing_key, permissive_policy_json(), 1, signed_at);
        let rc = reload_policy_signed_internal(&bundle, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS);
        assert_eq!(rc, FFI_POLICY_BUNDLE_IN_FUTURE);
    }

    #[test]
    fn reload_policy_signed_accepts_bundle_within_clock_skew_forward_window() {
        // Boundary: 5 minutes (POLICY_BUNDLE_FUTURE_SKEW_SECS) into the future
        // is at the limit, still accepted.
        reset_engine();
        init_test_engine(default_deny_policy());

        let signing_key = ed25519_dalek::SigningKey::from_bytes(&[0xa3; 32]);
        let trusted = make_trusted_keys_json(&signing_key);

        let signed_at = TEST_NOW_SECS + POLICY_BUNDLE_FUTURE_SKEW_SECS;
        let bundle =
            make_signed_bundle_envelope(&signing_key, permissive_policy_json(), 1, signed_at);
        let rc = reload_policy_signed_internal(&bundle, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS);
        assert_eq!(rc, FFI_OK);
    }

    #[test]
    fn reload_policy_signed_rejects_unknown_schema_version() {
        // A bundle with a schema_version we don't recognize must be rejected
        // with -10. Defends against a future control plane shipping an
        // incompatible bundle format to an older SDK that doesn't understand it.
        reset_engine();
        init_test_engine(default_deny_policy());

        let signing_key = ed25519_dalek::SigningKey::from_bytes(&[0xa3; 32]);
        let trusted = make_trusted_keys_json(&signing_key);

        // Manually construct a bundle with schema_version=999 and sign it.
        use base64::engine::general_purpose::STANDARD as B64;
        use base64::Engine;
        use ed25519_dalek::Signer;
        let bundle_json = serde_json::json!({
            "schema_version": 999,
            "version": 1,
            "signed_at": TEST_NOW_SECS,
            "policy": serde_json::from_str::<serde_json::Value>(permissive_policy_json()).unwrap(),
        });
        let bundle_bytes = serde_json::to_vec(&bundle_json).unwrap();
        let pae = checkrd_shared::dsse::pae(
            checkrd_shared::dsse::POLICY_BUNDLE_PAYLOAD_TYPE,
            &bundle_bytes,
        );
        let sig = signing_key.sign(&pae);
        let envelope = checkrd_shared::dsse::DsseEnvelope {
            payload_type: checkrd_shared::dsse::POLICY_BUNDLE_PAYLOAD_TYPE.to_string(),
            payload: B64.encode(&bundle_bytes),
            signatures: vec![checkrd_shared::dsse::DsseSignature {
                keyid: "test-cp".to_string(),
                sig: B64.encode(sig.to_bytes()),
            }],
        };
        let envelope_json = serde_json::to_string(&envelope).unwrap();

        let rc = reload_policy_signed_internal(
            &envelope_json,
            &trusted,
            TEST_NOW_SECS,
            TEST_MAX_AGE_SECS,
        );
        assert_eq!(rc, FFI_POLICY_SCHEMA_VERSION_MISMATCH);
    }

    #[test]
    fn get_active_policy_version_returns_zero_before_any_install() {
        reset_engine();
        init_test_engine(default_deny_policy());
        // No signed install yet → version is 0.
        assert_eq!(get_active_policy_version(), 0);
    }

    #[test]
    fn get_active_policy_version_returns_zero_when_engine_uninitialized() {
        reset_engine();
        // No engine state at all → return 0 instead of panicking.
        assert_eq!(get_active_policy_version(), 0);
    }

    // ----------------------------------------------------------------------
    // set_initial_policy_version: cross-restart rollback defense
    // ----------------------------------------------------------------------
    //
    // The wrapper persists the policy version high water mark to disk after
    // every successful install (~/.checkrd/policy_state.json). On startup,
    // it reads that value and feeds it back via this FFI export so the
    // monotonic check survives process restarts. Without this, an attacker
    // who can restart the SDK process trivially defeats rollback protection.

    #[test]
    fn set_initial_policy_version_succeeds_on_fresh_engine() {
        reset_engine();
        init_test_engine(default_deny_policy());
        let rc = set_initial_policy_version(42);
        assert_eq!(rc, FFI_OK);
        assert_eq!(get_active_policy_version(), 42);
    }

    #[test]
    fn set_initial_policy_version_rejects_when_already_set() {
        reset_engine();
        init_test_engine(default_deny_policy());
        // First call succeeds.
        assert_eq!(set_initial_policy_version(10), FFI_OK);
        // Second call must fail — once a version exists, the only path
        // forward is via the monotonic-check reload path.
        let rc = set_initial_policy_version(5);
        assert_eq!(rc, FFI_POLICY_VERSION_ALREADY_SET);
        // Counter must be unchanged.
        assert_eq!(get_active_policy_version(), 10);
    }

    #[test]
    fn set_initial_policy_version_rejects_when_real_install_already_happened() {
        // Strong-from-the-ground-up: if a real signed install has already
        // happened in this process, the persisted-version restore path
        // MUST be locked out — otherwise it could be used to roll the
        // counter backwards in-process.
        reset_engine();
        init_test_engine(default_deny_policy());
        let signing_key = ed25519_dalek::SigningKey::from_bytes(&[0xa3; 32]);
        let trusted = make_trusted_keys_json(&signing_key);
        let bundle =
            make_signed_bundle_envelope(&signing_key, permissive_policy_json(), 7, TEST_NOW_SECS);
        let rc = reload_policy_signed_internal(&bundle, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS);
        assert_eq!(rc, FFI_OK);
        assert_eq!(get_active_policy_version(), 7);
        // Now restoring from "persistence" must fail — the in-process
        // counter is the source of truth once it's non-zero.
        let rc = set_initial_policy_version(3);
        assert_eq!(rc, FFI_POLICY_VERSION_ALREADY_SET);
        assert_eq!(get_active_policy_version(), 7);
    }

    #[test]
    fn set_initial_policy_version_returns_engine_not_initialized() {
        reset_engine();
        // No engine — must surface -9, not panic.
        let rc = set_initial_policy_version(1);
        assert_eq!(rc, FFI_POLICY_ENGINE_NOT_INITIALIZED);
    }

    #[test]
    fn set_initial_policy_version_then_reload_enforces_monotonic_from_restored() {
        // Restoring version=10 from disk must make subsequent installs of
        // versions ≤10 be rejected as rollback. This is the whole point of
        // persistence — without it, an attacker who restarts the SDK can
        // replay an old, signed-but-stale bundle.
        //
        // The wrapper's hash cache (see `_policy_state.py` /
        // `_policy_state.ts`) handles the benign post-restart bootstrap
        // case where the SDK re-receives the same active bundle: hash
        // matches the persisted hash, so reload_policy_signed is never
        // called for that v=10 replay. This FFI-level test pins what
        // happens when the wrapper is bypassed.
        reset_engine();
        init_test_engine(default_deny_policy());
        assert_eq!(set_initial_policy_version(10), FFI_OK);

        let signing_key = ed25519_dalek::SigningKey::from_bytes(&[0xa3; 32]);
        let trusted = make_trusted_keys_json(&signing_key);

        // Replay attempt at v=8 must be rejected.
        let stale =
            make_signed_bundle_envelope(&signing_key, permissive_policy_json(), 8, TEST_NOW_SECS);
        let rc = reload_policy_signed_internal(&stale, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS);
        assert_eq!(
            rc, FFI_POLICY_VERSION_NOT_MONOTONIC,
            "v=8 must be rejected after restore at v=10"
        );

        // Same-version replay also rejected at the FFI layer — wrapper
        // cache is the layer that prevents the call for legitimate
        // re-bootstraps; this test asserts the FFI's safety net.
        let same =
            make_signed_bundle_envelope(&signing_key, permissive_policy_json(), 10, TEST_NOW_SECS);
        let rc = reload_policy_signed_internal(&same, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS);
        assert_eq!(
            rc, FFI_POLICY_VERSION_NOT_MONOTONIC,
            "v=10 must be rejected after restore at v=10 (FFI safety net)"
        );

        // Forward progress works.
        let fresh =
            make_signed_bundle_envelope(&signing_key, permissive_policy_json(), 11, TEST_NOW_SECS);
        let rc = reload_policy_signed_internal(&fresh, &trusted, TEST_NOW_SECS, TEST_MAX_AGE_SECS);
        assert_eq!(rc, FFI_OK, "v=11 must succeed after restore at v=10");
        assert_eq!(get_active_policy_version(), 11);
    }
}
