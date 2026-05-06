//! Shared types imported by WASM core and control-plane services.

pub mod api_key_scope;
pub mod billing;
pub mod dsse;
pub mod error;
pub mod events;
pub mod http;
pub mod http_sig;
pub mod policy;
pub mod policy_bundle;
pub mod telemetry;
pub mod url;

pub use api_key_scope::{AccessLevel, ApiKeyScope, Resource};
pub use billing::{PlanLimits, PlanTier};
pub use dsse::{
    pae, DsseEnvelope, DsseSignature, POLICY_BUNDLE_PAYLOAD_TYPE, TELEMETRY_BATCH_PAYLOAD_TYPE,
};
pub use error::PolicyError;
pub use events::{AgentEvent, AgentEventData, InteractionStatus, AGENT_EVENT_SCHEMA_VERSION};
pub use http::HttpMethod;
pub use http_sig::{
    build_signature_base, compute_content_digest, parse_signature_header, parse_signature_input,
    signature_base_string, CoveredComponents, SigError, TELEMETRY_SIGNATURE_LABEL,
};
pub use policy::{
    analyze_policy, diff_policies, merge_policies, BodyMatcher, DefaultAction, EvaluationRequest,
    EvaluationResult, EvaluationStep, HeaderMatcher, PolicyAnalysis, PolicyAnalysisSummary,
    PolicyConfig, PolicyDiff, PolicyMode, PolicyResult, PolicyRule, PolicyRuleKind, PolicyTestCase,
    PolicyTestExpectation, PolicyTestInput, PolicyTestResult, PolicyTestSummary, PolicyWarning,
    RateLimitConfig, RateLimitScope, RequestMatcher,
};
pub use policy_bundle::{PolicyBundle, POLICY_BUNDLE_SCHEMA_VERSION};
pub use telemetry::{
    parameterize_path, BatchSignature, TelemetryBatchMessage, TelemetryEvent, TelemetryEventInput,
    TelemetryRequest, TelemetryResponse, TelemetrySource, TelemetryValidationError,
    TELEMETRY_BATCH_SCHEMA_VERSION,
};
pub use url::{parse_url, ParsedUrl};
