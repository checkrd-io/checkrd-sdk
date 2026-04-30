use checkrd_shared::{EvaluationRequest, PolicyResult, TelemetryEvent, TelemetryRequest};

pub fn create_telemetry_event(
    agent_id: &str,
    instance_id: &str,
    request: &EvaluationRequest,
    allowed: bool,
    deny_reason: Option<&str>,
    host: &str,
    path: &str,
) -> TelemetryEvent {
    TelemetryEvent {
        event_id: request.request_id.clone(),
        agent_id: agent_id.to_string(),
        instance_id: instance_id.to_string(),
        timestamp: request.timestamp.clone(),
        request: TelemetryRequest {
            url_host: host.to_string(),
            url_path: path.to_string(),
            method: request.method,
        },
        response: None, // filled in by wrapper after the actual HTTP call
        policy_result: if allowed {
            PolicyResult::Allowed
        } else {
            PolicyResult::Denied
        },
        deny_reason: deny_reason.map(String::from),
        trace_id: request.trace_id.clone(),
        span_id: request.span_id.clone(),
        parent_span_id: request.parent_span_id.clone(),
        // OTEL span metadata -- span_name and span_kind are known at eval time.
        // span_status_code is set to UNSET here; the language wrapper upgrades it
        // to OK or ERROR after receiving the HTTP response.
        span_name: format!("{} {}", request.method, host),
        span_kind: "INTERNAL".into(),
        span_status_code: "UNSET".into(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use checkrd_shared::{EvaluationRequest, HttpMethod};

    fn test_request() -> EvaluationRequest {
        EvaluationRequest {
            request_id: "req-001".into(),
            method: HttpMethod::GET,
            url: "https://api.salesforce.com/v58/sobjects/Contact/001".into(),
            headers: vec![],
            body: None,
            timestamp: "2026-03-28T14:30:00Z".into(),
            timestamp_ms: 1774708200000,
            trace_id: "0af7651916cd43dd8448eb211c80319c".into(),
            span_id: "b7ad6b7169203331".into(),
            parent_span_id: None,
        }
    }

    #[test]
    fn allowed_event() {
        let event = create_telemetry_event(
            "sales-agent",
            "inst-abc",
            &test_request(),
            true,
            None,
            "api.salesforce.com",
            "/v58/sobjects/Contact/{id}",
        );
        assert_eq!(event.agent_id, "sales-agent");
        assert_eq!(event.instance_id, "inst-abc");
        assert_eq!(event.event_id, "req-001");
        assert_eq!(event.policy_result, PolicyResult::Allowed);
        assert!(event.deny_reason.is_none());
        assert!(event.response.is_none());
        // OTEL span fields
        assert_eq!(event.span_name, "GET api.salesforce.com");
        assert_eq!(event.span_kind, "INTERNAL");
        assert_eq!(event.span_status_code, "UNSET"); // wrapper upgrades after response
    }

    #[test]
    fn denied_event() {
        let event = create_telemetry_event(
            "sales-agent",
            "inst-abc",
            &test_request(),
            false,
            Some("denied by rule 'block-deletes'"),
            "api.salesforce.com",
            "/v58/sobjects/Contact/{id}",
        );
        assert_eq!(event.policy_result, PolicyResult::Denied);
        assert_eq!(
            event.deny_reason.as_deref(),
            Some("denied by rule 'block-deletes'")
        );
        // OTEL: denied is UNSET (policy working as designed)
        assert_eq!(event.span_status_code, "UNSET");
    }

    #[test]
    fn url_fields() {
        let event = create_telemetry_event(
            "agent",
            "inst",
            &test_request(),
            true,
            None,
            "api.salesforce.com",
            "/v58/sobjects/Contact/{id}",
        );
        assert_eq!(event.request.url_host, "api.salesforce.com");
        assert_eq!(event.request.url_path, "/v58/sobjects/Contact/{id}");
        assert_eq!(event.request.method, HttpMethod::GET);
    }

    #[test]
    fn trace_context_copied_to_event() {
        let mut req = test_request();
        req.trace_id = "a1b2c3d4e5f6a7b8a1b2c3d4e5f6a7b8".into();
        req.span_id = "1234567890abcdef".into();
        req.parent_span_id = Some("fedcba0987654321".into());

        let event = create_telemetry_event(
            "agent",
            "inst",
            &req,
            true,
            None,
            "api.salesforce.com",
            "/v58/sobjects/Contact/{id}",
        );

        assert_eq!(event.trace_id, "a1b2c3d4e5f6a7b8a1b2c3d4e5f6a7b8");
        assert_eq!(event.span_id, "1234567890abcdef");
        assert_eq!(event.parent_span_id.as_deref(), Some("fedcba0987654321"));
    }

    #[test]
    fn serializes_to_valid_json() {
        let event = create_telemetry_event(
            "agent",
            "inst",
            &test_request(),
            true,
            None,
            "api.salesforce.com",
            "/v58/sobjects/Contact/{id}",
        );
        let json = serde_json::to_string(&event).unwrap();
        let deserialized: checkrd_shared::TelemetryEvent = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized, event);
    }
}
