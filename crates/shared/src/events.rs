//! Agent event model — the type system for all events delivered to agents.
//!
//! # Architecture
//!
//! Every event is an [`AgentEvent`] envelope containing metadata (unique ID,
//! timestamp, schema version) and a typed [`AgentEventData`] payload. This
//! separation follows the [CloudEvents](https://cloudevents.io/) pattern used
//! by AWS EventBridge, Google Pub/Sub, and Kafka — metadata is processed by
//! infrastructure (routing, dedup, audit) without understanding the payload.
//!
//! # Phases
//!
//! - **Phase 1–2 (current):** Control signals from the control plane (kill
//!   switch, policy sync). Currently delivered via `ControlSignal` in
//!   `crates/api`. When the API migrates to `AgentEvent`, the SSE layer will
//!   serialize the new envelope format.
//! - **Phase 3 (planned):** Bilateral interaction events between agents.
//!   Delivery mechanism TBD (likely SQS-backed with at-least-once guarantees).
//!   Dedup uses `AgentEvent::id`; request/response correlation uses
//!   `interaction_id`.
//!
//! # Wire format
//!
//! ```json
//! {
//!   "id": "550e8400-e29b-41d4-a716-446655440000",
//!   "timestamp": "2026-04-01T12:00:00Z",
//!   "schema_version": 1,
//!   "data": {
//!     "type": "kill_switch",
//!     "agent_id": "...",
//!     "active": true
//!   }
//! }
//! ```
//!
//! # Design constraints
//!
//! - This crate is imported by the WASM core (no I/O, no async). All types
//!   here must be pure data.
//! - Interaction payloads use `serde_json::Value` because the schema varies
//!   by action type. Typed payloads can be layered via `serde_json::from_value`.

use serde::{Deserialize, Serialize};
use uuid::Uuid;

// ---------------------------------------------------------------------------
// Schema version
// ---------------------------------------------------------------------------

/// Current schema version for the `AgentEvent` envelope.
///
/// Bump this when making breaking changes to `AgentEvent` or `AgentEventData`.
/// Consumers that receive an event with an unrecognized version must skip it
/// (fail-open for forward compatibility during rolling deploys).
pub const AGENT_EVENT_SCHEMA_VERSION: u32 = 1;

// ---------------------------------------------------------------------------
// Event envelope
// ---------------------------------------------------------------------------

/// An event delivered to an agent.
///
/// The envelope carries metadata needed by infrastructure (routing, dedup,
/// ordering, audit) without knowing the payload type. The typed payload lives
/// in [`data`](AgentEvent::data).
///
/// Modeled after CloudEvents: `id` + `timestamp` + `type` (via the tagged
/// enum in `data`) + typed payload.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct AgentEvent {
    /// Unique event ID. Used for at-least-once dedup: consumers that see the
    /// same `id` twice must skip the duplicate.
    pub id: Uuid,

    /// ISO 8601 / RFC 3339 timestamp when the event was produced.
    pub timestamp: String,

    /// Schema version of this envelope format. Consumers must skip events
    /// with versions they don't understand. Matches the pattern used by
    /// [`TelemetryBatchMessage`](crate::TelemetryBatchMessage).
    pub schema_version: u32,

    /// The typed event payload.
    pub data: AgentEventData,
}

impl AgentEvent {
    /// The agent this event is addressed to (routing key).
    ///
    /// For control signals this is `agent_id`. For interactions this is
    /// `target_agent_id`.
    pub fn target_agent_id(&self) -> Uuid {
        self.data.target_agent_id()
    }

    /// The event type as a static string (e.g. `"kill_switch"`).
    ///
    /// Matches the `data.type` serde tag. Use for logging, metrics, and
    /// routing tables without pattern matching the full enum.
    pub fn event_type(&self) -> &'static str {
        self.data.event_type()
    }
}

// ---------------------------------------------------------------------------
// Event payload
// ---------------------------------------------------------------------------

/// Typed event payload, discriminated by the `"type"` tag.
///
/// Control signal variants use the same field names as the existing
/// `ControlSignal` in `crates/api/src/services/pubsub.rs`, so the API
/// migration is a matter of wrapping each signal in an `AgentEvent` envelope.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "type")]
pub enum AgentEventData {
    // --- Control signals (Phase 1–2) ------------------------------------
    /// Kill switch activation/deactivation.
    ///
    /// When `active` is true, the agent must stop making API calls immediately.
    #[serde(rename = "kill_switch")]
    KillSwitch { agent_id: Uuid, active: bool },

    /// Policy hot-reload. Contains the full YAML so the agent can apply it
    /// without a round-trip to the control plane.
    #[serde(rename = "policy_updated")]
    PolicyUpdated {
        agent_id: Uuid,
        version: i32,
        hash: String,
        yaml_content: String,
    },

    // --- Agent-to-agent interactions (Phase 3, reserved) ----------------
    /// A request from one agent to another.
    ///
    /// The receiving agent's policy engine decides whether to accept. If
    /// accepted, the agent performs the action and sends back an
    /// `InteractionResponse` with the same `interaction_id`.
    #[serde(rename = "interaction_request")]
    InteractionRequest {
        /// Correlation ID linking request and response. Distinct from
        /// `AgentEvent::id` (which is the delivery-level dedup key).
        interaction_id: Uuid,
        /// The agent that initiated this interaction.
        source_agent_id: Uuid,
        /// The agent that should receive and process this request.
        target_agent_id: Uuid,
        /// Identifies the kind of action requested.
        /// Namespace convention: `"org.capability"`, e.g. `"stripe.create_charge"`.
        action: String,
        /// Action-specific payload. Schema depends on `action`.
        #[serde(default)]
        payload: serde_json::Value,
        /// ISO 8601 expiry. The receiving agent should reject after this time.
        #[serde(default)]
        expires_at: Option<String>,
    },

    /// Response to a prior `InteractionRequest`.
    #[serde(rename = "interaction_response")]
    InteractionResponse {
        /// Must match the request's `interaction_id`.
        interaction_id: Uuid,
        /// The agent sending this response (was `target_agent_id` in the request).
        source_agent_id: Uuid,
        /// The agent receiving this response (was `source_agent_id` in the request).
        target_agent_id: Uuid,
        /// Outcome of the interaction.
        status: InteractionStatus,
        /// Response payload. Present on `Accepted`; may carry error details on
        /// `Rejected` or `Error`.
        #[serde(default)]
        payload: serde_json::Value,
    },
}

impl AgentEventData {
    /// The agent this payload is addressed to.
    pub fn target_agent_id(&self) -> Uuid {
        match self {
            Self::KillSwitch { agent_id, .. } | Self::PolicyUpdated { agent_id, .. } => *agent_id,
            Self::InteractionRequest {
                target_agent_id, ..
            }
            | Self::InteractionResponse {
                target_agent_id, ..
            } => *target_agent_id,
        }
    }

    /// The event type as a static string, matching the serde `"type"` tag.
    pub fn event_type(&self) -> &'static str {
        match self {
            Self::KillSwitch { .. } => "kill_switch",
            Self::PolicyUpdated { .. } => "policy_updated",
            Self::InteractionRequest { .. } => "interaction_request",
            Self::InteractionResponse { .. } => "interaction_response",
        }
    }
}

// ---------------------------------------------------------------------------
// Supporting types
// ---------------------------------------------------------------------------

/// Outcome of an agent-to-agent interaction.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum InteractionStatus {
    /// The target agent accepted and processed the request.
    Accepted,
    /// The target agent's policy denied the interaction.
    Rejected,
    /// The request expired before the target agent processed it.
    Expired,
    /// The target agent encountered an error while processing.
    Error,
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // Stable UUIDs for golden tests (deterministic output).
    const UUID_A: &str = "550e8400-e29b-41d4-a716-446655440000";
    const UUID_B: &str = "550e8400-e29b-41d4-a716-446655440001";
    const UUID_C: &str = "550e8400-e29b-41d4-a716-446655440002";

    fn uuid_a() -> Uuid {
        UUID_A.parse().unwrap()
    }
    fn uuid_b() -> Uuid {
        UUID_B.parse().unwrap()
    }
    fn uuid_c() -> Uuid {
        UUID_C.parse().unwrap()
    }

    /// Wrap data in a valid envelope with stable values for golden tests.
    fn envelope(data: AgentEventData) -> AgentEvent {
        AgentEvent {
            id: uuid_a(),
            timestamp: "2026-04-01T12:00:00Z".into(),
            schema_version: AGENT_EVENT_SCHEMA_VERSION,
            data,
        }
    }

    // ===================================================================
    // 1. Golden wire format (exact JSON comparison)
    //
    // These are the wire format contract. If any test here fails, it means
    // a serialization change that would break consumers. Update these only
    // when intentionally changing the wire format AND bumping schema_version.
    // ===================================================================

    #[test]
    fn golden_kill_switch() {
        let event = envelope(AgentEventData::KillSwitch {
            agent_id: uuid_b(),
            active: true,
        });
        let actual: serde_json::Value = serde_json::to_value(&event).unwrap();
        let expected = serde_json::json!({
            "id": UUID_A,
            "timestamp": "2026-04-01T12:00:00Z",
            "schema_version": 1,
            "data": {
                "type": "kill_switch",
                "agent_id": UUID_B,
                "active": true,
            }
        });
        assert_eq!(actual, expected);
    }

    #[test]
    fn golden_policy_updated() {
        let event = envelope(AgentEventData::PolicyUpdated {
            agent_id: uuid_b(),
            version: 3,
            hash: "sha256:abc123".into(),
            yaml_content: "agent: test\ndefault: deny\nrules: []".into(),
        });
        let actual: serde_json::Value = serde_json::to_value(&event).unwrap();
        let expected = serde_json::json!({
            "id": UUID_A,
            "timestamp": "2026-04-01T12:00:00Z",
            "schema_version": 1,
            "data": {
                "type": "policy_updated",
                "agent_id": UUID_B,
                "version": 3,
                "hash": "sha256:abc123",
                "yaml_content": "agent: test\ndefault: deny\nrules: []",
            }
        });
        assert_eq!(actual, expected);
    }

    #[test]
    fn golden_interaction_request() {
        let event = envelope(AgentEventData::InteractionRequest {
            interaction_id: uuid_b(),
            source_agent_id: uuid_b(),
            target_agent_id: uuid_c(),
            action: "stripe.create_charge".into(),
            payload: serde_json::json!({"amount": 5000}),
            expires_at: Some("2026-04-01T13:00:00Z".into()),
        });
        let actual: serde_json::Value = serde_json::to_value(&event).unwrap();
        let expected = serde_json::json!({
            "id": UUID_A,
            "timestamp": "2026-04-01T12:00:00Z",
            "schema_version": 1,
            "data": {
                "type": "interaction_request",
                "interaction_id": UUID_B,
                "source_agent_id": UUID_B,
                "target_agent_id": UUID_C,
                "action": "stripe.create_charge",
                "payload": {"amount": 5000},
                "expires_at": "2026-04-01T13:00:00Z",
            }
        });
        assert_eq!(actual, expected);
    }

    #[test]
    fn golden_interaction_response() {
        let event = envelope(AgentEventData::InteractionResponse {
            interaction_id: uuid_b(),
            source_agent_id: uuid_c(),
            target_agent_id: uuid_b(),
            status: InteractionStatus::Accepted,
            payload: serde_json::json!({"charge_id": "ch_xxx"}),
        });
        let actual: serde_json::Value = serde_json::to_value(&event).unwrap();
        let expected = serde_json::json!({
            "id": UUID_A,
            "timestamp": "2026-04-01T12:00:00Z",
            "schema_version": 1,
            "data": {
                "type": "interaction_response",
                "interaction_id": UUID_B,
                "source_agent_id": UUID_C,
                "target_agent_id": UUID_B,
                "status": "accepted",
                "payload": {"charge_id": "ch_xxx"},
            }
        });
        assert_eq!(actual, expected);
    }

    // ===================================================================
    // 2. Round-trip serialization (every variant)
    // ===================================================================

    #[test]
    fn round_trip_kill_switch() {
        let event = envelope(AgentEventData::KillSwitch {
            agent_id: uuid_a(),
            active: false,
        });
        let json = serde_json::to_string(&event).unwrap();
        assert_eq!(event, serde_json::from_str::<AgentEvent>(&json).unwrap());
    }

    #[test]
    fn round_trip_policy_updated() {
        let event = envelope(AgentEventData::PolicyUpdated {
            agent_id: uuid_a(),
            version: 42,
            hash: "sha256:deadbeef".into(),
            yaml_content: "agent: x\ndefault: allow\nrules: []".into(),
        });
        let json = serde_json::to_string(&event).unwrap();
        assert_eq!(event, serde_json::from_str::<AgentEvent>(&json).unwrap());
    }

    #[test]
    fn round_trip_interaction_request() {
        let event = envelope(AgentEventData::InteractionRequest {
            interaction_id: uuid_a(),
            source_agent_id: uuid_b(),
            target_agent_id: uuid_c(),
            action: "test.action".into(),
            payload: serde_json::json!({"key": "value"}),
            expires_at: Some("2026-12-31T23:59:59Z".into()),
        });
        let json = serde_json::to_string(&event).unwrap();
        assert_eq!(event, serde_json::from_str::<AgentEvent>(&json).unwrap());
    }

    #[test]
    fn round_trip_interaction_response() {
        let event = envelope(AgentEventData::InteractionResponse {
            interaction_id: uuid_a(),
            source_agent_id: uuid_b(),
            target_agent_id: uuid_c(),
            status: InteractionStatus::Error,
            payload: serde_json::json!({"error": "timeout"}),
        });
        let json = serde_json::to_string(&event).unwrap();
        assert_eq!(event, serde_json::from_str::<AgentEvent>(&json).unwrap());
    }

    // ===================================================================
    // 3. Forward compatibility (unknown fields silently ignored)
    //
    // If someone adds #[serde(deny_unknown_fields)] in the future, these
    // tests catch it. New fields from a newer producer must not break
    // older consumers.
    // ===================================================================

    #[test]
    fn envelope_ignores_unknown_top_level_fields() {
        let json = serde_json::json!({
            "id": UUID_A,
            "timestamp": "2026-04-01T12:00:00Z",
            "schema_version": 1,
            "source": "checkrd://control-plane",
            "trace_id": "abc123",
            "data": {"type": "kill_switch", "agent_id": UUID_A, "active": true}
        });
        let event: AgentEvent = serde_json::from_value(json).unwrap();
        assert_eq!(event.event_type(), "kill_switch");
    }

    #[test]
    fn data_ignores_unknown_fields_in_variant() {
        let json = serde_json::json!({
            "id": UUID_A,
            "timestamp": "2026-04-01T12:00:00Z",
            "schema_version": 1,
            "data": {
                "type": "kill_switch",
                "agent_id": UUID_A,
                "active": true,
                "reason": "emergency shutdown",
                "triggered_by": "admin@example.com"
            }
        });
        let event: AgentEvent = serde_json::from_value(json).unwrap();
        assert!(matches!(
            event.data,
            AgentEventData::KillSwitch { active: true, .. }
        ));
    }

    #[test]
    fn unknown_schema_version_still_deserializes() {
        let json = serde_json::json!({
            "id": UUID_A,
            "timestamp": "2026-04-01T12:00:00Z",
            "schema_version": 999,
            "data": {"type": "kill_switch", "agent_id": UUID_A, "active": true}
        });
        let event: AgentEvent = serde_json::from_value(json).unwrap();
        assert_eq!(event.schema_version, 999);
    }

    // ===================================================================
    // 4. Optional field handling (null vs absent)
    // ===================================================================

    #[test]
    fn interaction_request_payload_absent() {
        let json = serde_json::json!({
            "id": UUID_A, "timestamp": "2026-04-01T12:00:00Z", "schema_version": 1,
            "data": {
                "type": "interaction_request",
                "interaction_id": UUID_A, "source_agent_id": UUID_B,
                "target_agent_id": UUID_C, "action": "ping"
            }
        });
        let event: AgentEvent = serde_json::from_value(json).unwrap();
        match event.data {
            AgentEventData::InteractionRequest {
                payload,
                expires_at,
                ..
            } => {
                assert!(payload.is_null(), "absent payload defaults to null");
                assert!(expires_at.is_none(), "absent expires_at is None");
            }
            _ => panic!("expected InteractionRequest"),
        }
    }

    #[test]
    fn interaction_request_payload_explicit_null() {
        let json = serde_json::json!({
            "id": UUID_A, "timestamp": "2026-04-01T12:00:00Z", "schema_version": 1,
            "data": {
                "type": "interaction_request",
                "interaction_id": UUID_A, "source_agent_id": UUID_B,
                "target_agent_id": UUID_C, "action": "ping",
                "payload": null, "expires_at": null
            }
        });
        let event: AgentEvent = serde_json::from_value(json).unwrap();
        match event.data {
            AgentEventData::InteractionRequest {
                payload,
                expires_at,
                ..
            } => {
                assert!(payload.is_null());
                assert!(expires_at.is_none());
            }
            _ => panic!("expected InteractionRequest"),
        }
    }

    #[test]
    fn interaction_response_payload_absent() {
        let json = serde_json::json!({
            "id": UUID_A, "timestamp": "2026-04-01T12:00:00Z", "schema_version": 1,
            "data": {
                "type": "interaction_response",
                "interaction_id": UUID_A, "source_agent_id": UUID_B,
                "target_agent_id": UUID_C, "status": "rejected"
            }
        });
        let event: AgentEvent = serde_json::from_value(json).unwrap();
        match event.data {
            AgentEventData::InteractionResponse {
                status, payload, ..
            } => {
                assert_eq!(status, InteractionStatus::Rejected);
                assert!(payload.is_null());
            }
            _ => panic!("expected InteractionResponse"),
        }
    }

    // ===================================================================
    // 5. Type tag stability
    //
    // The string values of the "type" tag are wire-format contracts.
    // Renaming any of these is a breaking change.
    // ===================================================================

    #[test]
    fn type_tag_kill_switch() {
        let v = serde_json::to_value(AgentEventData::KillSwitch {
            agent_id: uuid_a(),
            active: true,
        })
        .unwrap();
        assert_eq!(v["type"], "kill_switch");
    }

    #[test]
    fn type_tag_policy_updated() {
        let v = serde_json::to_value(AgentEventData::PolicyUpdated {
            agent_id: uuid_a(),
            version: 1,
            hash: "h".into(),
            yaml_content: "y".into(),
        })
        .unwrap();
        assert_eq!(v["type"], "policy_updated");
    }

    #[test]
    fn type_tag_interaction_request() {
        let v = serde_json::to_value(AgentEventData::InteractionRequest {
            interaction_id: uuid_a(),
            source_agent_id: uuid_a(),
            target_agent_id: uuid_a(),
            action: "x".into(),
            payload: serde_json::Value::Null,
            expires_at: None,
        })
        .unwrap();
        assert_eq!(v["type"], "interaction_request");
    }

    #[test]
    fn type_tag_interaction_response() {
        let v = serde_json::to_value(AgentEventData::InteractionResponse {
            interaction_id: uuid_a(),
            source_agent_id: uuid_a(),
            target_agent_id: uuid_a(),
            status: InteractionStatus::Accepted,
            payload: serde_json::Value::Null,
        })
        .unwrap();
        assert_eq!(v["type"], "interaction_response");
    }

    #[test]
    fn unknown_type_tag_is_deser_error() {
        let json = serde_json::json!({
            "id": UUID_A, "timestamp": "2026-04-01T12:00:00Z", "schema_version": 1,
            "data": {"type": "future_variant", "foo": "bar"}
        });
        assert!(serde_json::from_value::<AgentEvent>(json).is_err());
    }

    // ===================================================================
    // 6. InteractionStatus (all variants through full envelope path)
    // ===================================================================

    #[test]
    fn interaction_status_wire_names() {
        for (variant, wire_name) in [
            (InteractionStatus::Accepted, "accepted"),
            (InteractionStatus::Rejected, "rejected"),
            (InteractionStatus::Expired, "expired"),
            (InteractionStatus::Error, "error"),
        ] {
            let json = serde_json::to_string(&variant).unwrap();
            assert_eq!(json, format!(r#""{wire_name}""#));
            let back: InteractionStatus = serde_json::from_str(&json).unwrap();
            assert_eq!(variant, back);
        }
    }

    #[test]
    fn interaction_response_round_trips_with_each_status() {
        for status in [
            InteractionStatus::Accepted,
            InteractionStatus::Rejected,
            InteractionStatus::Expired,
            InteractionStatus::Error,
        ] {
            let event = envelope(AgentEventData::InteractionResponse {
                interaction_id: uuid_a(),
                source_agent_id: uuid_b(),
                target_agent_id: uuid_c(),
                status,
                payload: serde_json::Value::Null,
            });
            let json = serde_json::to_string(&event).unwrap();
            let back: AgentEvent = serde_json::from_str(&json).unwrap();
            assert_eq!(event, back, "round-trip failed for status {status:?}");
        }
    }

    // ===================================================================
    // 7. event_type() accessor (matches serde tag for every variant)
    // ===================================================================

    #[test]
    fn event_type_matches_serde_tag() {
        let cases: Vec<(AgentEventData, &str)> = vec![
            (
                AgentEventData::KillSwitch {
                    agent_id: uuid_a(),
                    active: true,
                },
                "kill_switch",
            ),
            (
                AgentEventData::PolicyUpdated {
                    agent_id: uuid_a(),
                    version: 1,
                    hash: "h".into(),
                    yaml_content: "y".into(),
                },
                "policy_updated",
            ),
            (
                AgentEventData::InteractionRequest {
                    interaction_id: uuid_a(),
                    source_agent_id: uuid_a(),
                    target_agent_id: uuid_a(),
                    action: "x".into(),
                    payload: serde_json::Value::Null,
                    expires_at: None,
                },
                "interaction_request",
            ),
            (
                AgentEventData::InteractionResponse {
                    interaction_id: uuid_a(),
                    source_agent_id: uuid_a(),
                    target_agent_id: uuid_a(),
                    status: InteractionStatus::Accepted,
                    payload: serde_json::Value::Null,
                },
                "interaction_response",
            ),
        ];
        for (data, expected_type) in cases {
            // Accessor matches the serde tag in serialized JSON
            let v: serde_json::Value = serde_json::to_value(&data).unwrap();
            assert_eq!(data.event_type(), expected_type);
            assert_eq!(v["type"], expected_type);

            // Envelope delegates correctly
            assert_eq!(envelope(data).event_type(), expected_type);
        }
    }

    // ===================================================================
    // 8. target_agent_id() accessor (every variant)
    // ===================================================================

    #[test]
    fn target_agent_id_kill_switch() {
        let event = envelope(AgentEventData::KillSwitch {
            agent_id: uuid_b(),
            active: true,
        });
        assert_eq!(event.target_agent_id(), uuid_b());
    }

    #[test]
    fn target_agent_id_policy_updated() {
        let event = envelope(AgentEventData::PolicyUpdated {
            agent_id: uuid_b(),
            version: 1,
            hash: "h".into(),
            yaml_content: "y".into(),
        });
        assert_eq!(event.target_agent_id(), uuid_b());
    }

    #[test]
    fn target_agent_id_interaction_request() {
        let event = envelope(AgentEventData::InteractionRequest {
            interaction_id: uuid_a(),
            source_agent_id: uuid_b(),
            target_agent_id: uuid_c(),
            action: "x".into(),
            payload: serde_json::Value::Null,
            expires_at: None,
        });
        assert_eq!(event.target_agent_id(), uuid_c());
    }

    #[test]
    fn target_agent_id_interaction_response() {
        let event = envelope(AgentEventData::InteractionResponse {
            interaction_id: uuid_a(),
            source_agent_id: uuid_c(),
            target_agent_id: uuid_b(),
            status: InteractionStatus::Accepted,
            payload: serde_json::Value::Null,
        });
        assert_eq!(event.target_agent_id(), uuid_b());
    }

    // ===================================================================
    // 9. Dedup semantics (event.id vs interaction_id)
    // ===================================================================

    #[test]
    fn interaction_id_correlates_request_and_response() {
        let iid = uuid_b();
        let req = envelope(AgentEventData::InteractionRequest {
            interaction_id: iid,
            source_agent_id: uuid_a(),
            target_agent_id: uuid_c(),
            action: "ping".into(),
            payload: serde_json::Value::Null,
            expires_at: None,
        });
        let resp = AgentEvent {
            id: uuid_c(), // different event id
            timestamp: "2026-04-01T12:00:01Z".into(),
            schema_version: 1,
            data: AgentEventData::InteractionResponse {
                interaction_id: iid, // same interaction id
                source_agent_id: uuid_c(),
                target_agent_id: uuid_a(),
                status: InteractionStatus::Accepted,
                payload: serde_json::Value::Null,
            },
        };
        // Different events (different id), same interaction (same interaction_id)
        assert_ne!(req.id, resp.id);
        match (&req.data, &resp.data) {
            (
                AgentEventData::InteractionRequest {
                    interaction_id: a, ..
                },
                AgentEventData::InteractionResponse {
                    interaction_id: b, ..
                },
            ) => assert_eq!(a, b),
            _ => panic!("wrong variants"),
        }
    }

    // ===================================================================
    // 10. Edge cases
    // ===================================================================

    #[test]
    fn payload_nested_objects() {
        let event = envelope(AgentEventData::InteractionRequest {
            interaction_id: uuid_a(),
            source_agent_id: uuid_b(),
            target_agent_id: uuid_c(),
            action: "complex".into(),
            payload: serde_json::json!({
                "nested": {"deep": {"value": 42}},
                "array": [1, 2, 3],
                "null_field": null,
            }),
            expires_at: None,
        });
        let json = serde_json::to_string(&event).unwrap();
        let back: AgentEvent = serde_json::from_str(&json).unwrap();
        assert_eq!(event, back);
    }

    #[test]
    fn payload_empty_object() {
        let event = envelope(AgentEventData::InteractionRequest {
            interaction_id: uuid_a(),
            source_agent_id: uuid_b(),
            target_agent_id: uuid_c(),
            action: "no-op".into(),
            payload: serde_json::json!({}),
            expires_at: None,
        });
        let json = serde_json::to_string(&event).unwrap();
        let back: AgentEvent = serde_json::from_str(&json).unwrap();
        assert_eq!(event, back);
    }

    #[test]
    fn unicode_in_string_fields() {
        let event = envelope(AgentEventData::PolicyUpdated {
            agent_id: uuid_a(),
            version: 1,
            hash: "sha256:café".into(),
            yaml_content: "agent: テスト\ndefault: deny\nrules: []".into(),
        });
        let json = serde_json::to_string(&event).unwrap();
        let back: AgentEvent = serde_json::from_str(&json).unwrap();
        assert_eq!(event, back);
    }

    #[test]
    fn empty_action_string_deserializes() {
        // Empty action is syntactically valid (semantic validation happens elsewhere)
        let json = serde_json::json!({
            "id": UUID_A, "timestamp": "2026-04-01T12:00:00Z", "schema_version": 1,
            "data": {
                "type": "interaction_request",
                "interaction_id": UUID_A, "source_agent_id": UUID_B,
                "target_agent_id": UUID_C, "action": ""
            }
        });
        let event: AgentEvent = serde_json::from_value(json).unwrap();
        match event.data {
            AgentEventData::InteractionRequest { action, .. } => assert_eq!(action, ""),
            _ => panic!("expected InteractionRequest"),
        }
    }
}
