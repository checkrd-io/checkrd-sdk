"""B1: batcher must not drop matched_rule, matched_rule_kind, mode, evaluation_path."""
from checkrd.batcher import TelemetryBatcher


def test_flatten_event_preserves_evaluation_metadata():
    wasm_event = {
        "event_id": "req-42",
        "agent_id": "agent-1",
        "timestamp": "2026-04-20T00:00:00Z",
        "policy_result": "denied",
        "matched_rule": "block-external-api",
        "matched_rule_kind": "deny",
        "mode": "enforce",
        "evaluation_path": [
            {"stage": "kill_switch", "result": "pass"},
            {"stage": "deny_rules", "rule": "block-external-api", "result": "matched"},
        ],
        "request": {"url_host": "api.example.com", "url_path": "/", "method": "POST"},
        "response": {"status_code": 403, "latency_ms": 3},
    }
    flat = TelemetryBatcher._flatten_event(wasm_event)
    assert flat["matched_rule"] == "block-external-api"
    assert flat["matched_rule_kind"] == "deny"
    assert flat["policy_mode"] == "enforce"  # renamed from "mode"
    assert flat["evaluation_path"] == [
        {"stage": "kill_switch", "result": "pass"},
        {"stage": "deny_rules", "rule": "block-external-api", "result": "matched"},
    ]
