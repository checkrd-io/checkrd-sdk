use serde::{Deserialize, Serialize};
use typeshare::typeshare;

use crate::http::HttpMethod;

// --- Policy configuration (deserialized from YAML) ---

// PolicyConfig holds `Vec<PolicyRule>`; since PolicyRule can't be typeshared
// (see note above), PolicyConfig is left untyped too. The dashboard edits
// policies as raw YAML strings and consumes `PolicyDiff` / `PolicyAnalysis`
// for UI — both of those ARE typeshared.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PolicyConfig {
    pub default: DefaultAction,
    /// Enforcement mode. When `dry_run`, decisions are logged but always allowed.
    #[serde(default)]
    pub mode: PolicyMode,
    pub rules: Vec<PolicyRule>,
}

#[typeshare]
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
#[non_exhaustive]
pub enum DefaultAction {
    Allow,
    Deny,
}

// `PolicyRule` uses `#[serde(flatten)]` on `kind` so the JSON looks like
// `{ name: "x", allow: {..} }` rather than `{ name: "x", kind: { type: "allow", ..} }`.
// typeshare doesn't support `flatten` today (it would require a tagged
// representation that would break the existing wire format). Leaving
// untyped here means the dashboard interacts with policies through the
// YAML string + PolicyDiff / PolicyAnalysis, which we DO export.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PolicyRule {
    pub name: String,
    #[serde(flatten)]
    pub kind: PolicyRuleKind,
    /// Source of this rule after merge: "org", "agent", or "template:<name>".
    /// Set by the control plane during merge — not user-authored.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<String>,
}

// Algebraic enum paired with `PolicyRule`'s `#[serde(flatten)]`: it uses
// serde's externally-tagged representation (`{ allow: {..} }`), which
// typeshare-cli rejects because the current generator only emits
// `{ type, content }` for tagged enums. Intentionally NOT typeshared —
// matches the `PolicyRule` decision above.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
#[non_exhaustive]
pub enum PolicyRuleKind {
    Allow(RequestMatcher),
    Deny(RequestMatcher),
    Limit(RateLimitConfig),
}

// RequestMatcher is reached through PolicyRule / PolicyRuleKind, so it
// doesn't need a typeshare annotation in its own right — the dashboard
// uses raw YAML for policy editing. Leaving untyped to avoid a broken
// transitive reference.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RequestMatcher {
    #[serde(default)]
    pub method: Vec<HttpMethod>,
    #[serde(default)]
    pub url: Option<String>,
    /// Body field matchers (AND semantics — all must match).
    /// Accepts either a single object or an array in YAML/JSON.
    #[serde(
        default,
        deserialize_with = "deserialize_body_matchers",
        serialize_with = "serialize_body_matchers",
        skip_serializing_if = "Vec::is_empty"
    )]
    pub body: Vec<BodyMatcher>,
    /// Header matchers (AND semantics — all must match).
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub headers: Vec<HeaderMatcher>,
    #[serde(default)]
    pub time_outside: Option<String>,
    #[serde(default)]
    pub timezone: Option<String>,
}

/// Matches a field in the JSON request body identified by a dot-path.
///
/// Operators are ANDed: if both `exact` and `max` are set, the field value
/// must satisfy both. If no operator is set, the matcher checks field
/// existence only (equivalent to OPA's `input.body.field` truthiness check).
///
/// Operator categories:
/// - Numeric: `max`, `min` — field must be a JSON number.
/// - Value: `exact`, `in` — compared via JSON value equality (type-aware).
/// - String: `prefix`, `suffix`, `contains`, `regex` — field must be a JSON string.
// Not typeshared — reached through PolicyRule's flatten-based wire format,
// which typeshare can't describe. See note above on PolicyRule.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BodyMatcher {
    pub jsonpath: String,
    // -- Numeric operators (field must be a JSON number) --
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub min: Option<i64>,
    // -- Value operators (compared via JSON value equality) --
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub exact: Option<serde_json::Value>,
    #[serde(default, rename = "in", skip_serializing_if = "Option::is_none")]
    pub in_values: Option<Vec<serde_json::Value>>,
    // -- String operators (field must be a JSON string) --
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub prefix: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub suffix: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub contains: Option<String>,
    /// RE2-compatible regex pattern (linear-time, no backtracking).
    /// Max pattern length: 256 characters (enforced at the API layer).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub regex: Option<String>,
}

/// Matches a request header by name.
///
/// Header names are matched case-insensitively (per RFC 9110 / HTTP/2).
/// Header values are matched case-sensitively by default.
/// If multiple operators are set, all must match (AND semantics).
/// If no operator is set, `present: true` is implied.
// Not typeshared — same reason as BodyMatcher / PolicyRule.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct HeaderMatcher {
    pub name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub exact: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub prefix: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub suffix: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub contains: Option<String>,
    /// RE2-compatible regex pattern (linear-time, no backtracking).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub regex: Option<String>,
    /// If true, only checks that the header exists (any value).
    /// If false (explicit), checks that the header does NOT exist.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub present: Option<bool>,
}

// Not typeshared — part of the PolicyRule wire format. See note above.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RateLimitConfig {
    pub calls_per_minute: u32,
    pub per: RateLimitScope,
    /// Dot-path to a JSON body field used as the rate limit key.
    /// Required when `per` is `body_field`. E.g., `"$.model"`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub field: Option<String>,
}

// Not typeshared — tied to RateLimitConfig above.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
#[non_exhaustive]
pub enum RateLimitScope {
    Endpoint,
    Global,
    /// Rate limit keyed on a JSON body field value (e.g., per-model limiting).
    BodyField,
}

// --- Body matcher serde helpers ---

/// Deserialize `body` as either a single BodyMatcher object or an array.
/// This allows both YAML syntaxes:
/// ```yaml
/// body:                    # single matcher
///   jsonpath: "$.amount"
///   max: 50000
/// ```
/// and:
/// ```yaml
/// body:                    # multiple matchers (AND)
///   - jsonpath: "$.amount"
///     max: 50000
///   - jsonpath: "$.model"
///     exact: "gpt-4o"
/// ```
fn deserialize_body_matchers<'de, D>(deserializer: D) -> Result<Vec<BodyMatcher>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    #[derive(Deserialize)]
    #[serde(untagged)]
    enum SingleOrVec {
        Single(BodyMatcher),
        Vec(Vec<BodyMatcher>),
    }

    let opt: Option<SingleOrVec> = Option::deserialize(deserializer)?;
    Ok(match opt {
        None => Vec::new(),
        Some(SingleOrVec::Single(m)) => vec![m],
        Some(SingleOrVec::Vec(v)) => v,
    })
}

/// Serialize `body` as a single object if there's exactly one matcher,
/// an array if there are multiple, or omit if empty.
fn serialize_body_matchers<S>(body: &[BodyMatcher], serializer: S) -> Result<S::Ok, S::Error>
where
    S: serde::Serializer,
{
    match body.len() {
        0 => serializer.serialize_none(),
        1 => body[0].serialize(serializer),
        _ => body.serialize(serializer),
    }
}

// --- Evaluation types (cross the WASM boundary as JSON) ---

#[typeshare]
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
#[non_exhaustive]
pub enum PolicyResult {
    Allowed,
    Denied,
}

// EvaluationRequest carries `Vec<(String, String)>` headers. typeshare
// rejects tuple types. Not exposed to the dashboard anyway (it's the input
// to the WASM FFI boundary), so skipping.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EvaluationRequest {
    pub request_id: String,
    pub method: HttpMethod,
    pub url: String,
    pub headers: Vec<(String, String)>,
    #[serde(default)]
    pub body: Option<String>,
    pub timestamp: String,
    pub timestamp_ms: u64,
    /// OTEL trace context -- 32 lowercase hex chars
    pub trace_id: String,
    /// OTEL span context -- 16 lowercase hex chars
    pub span_id: String,
    /// OTEL parent span -- 16 lowercase hex chars, if propagated from caller
    #[serde(default)]
    pub parent_span_id: Option<String>,
}

/// Result returned from the WASM policy engine across the FFI boundary.
#[typeshare]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EvaluationResult {
    pub allowed: bool,
    #[serde(default)]
    pub deny_reason: Option<String>,
    /// Name of the rule that determined the decision.
    /// Present for rule matches and rate limit hits. Absent for default-action fallthrough.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub matched_rule: Option<String>,
    /// Kind of the matched rule: "allow", "deny", "rate_limit", "kill_switch", or "default".
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub matched_rule_kind: Option<String>,
    /// Policy enforcement mode. In dry_run, decisions are logged but always allowed.
    #[serde(default)]
    pub mode: PolicyMode,
    /// Ordered evaluation trace: which stages ran and what they decided.
    /// Follows OPA decision log / AWS IAM MatchedStatements pattern.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub evaluation_path: Vec<EvaluationStep>,
    pub log_event_json: String,
    pub request_id: String,
}

/// One step in the evaluation trace, recording what the engine checked.
#[typeshare]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EvaluationStep {
    pub stage: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rule: Option<String>,
    pub result: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

/// Policy enforcement mode.
#[typeshare]
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
#[non_exhaustive]
pub enum PolicyMode {
    /// Normal enforcement — decisions are applied.
    #[default]
    Enforce,
    /// Shadow/dry-run — decisions are logged but not enforced (always allows).
    DryRun,
}

// --- Policy test framework types ---

/// A single test case for policy testing.
/// Follows Envoy Route Table Check Tool / OPA test conventions.
// Not typeshared — PolicyTestInput carries `Vec<(String, String)>` headers.
// See note on PolicyTestInput below.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PolicyTestCase {
    pub name: String,
    pub input: PolicyTestInput,
    pub expect: PolicyTestExpectation,
}

/// Input for a test case — the request to evaluate.
// Not typeshared — `Vec<(String, String)>` headers aren't expressible in
// typeshare (rejects tuples). Dashboard policy tests go through raw JSON
// against the /test endpoint, not through this typed shape.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PolicyTestInput {
    pub method: HttpMethod,
    pub url: String,
    #[serde(default)]
    pub headers: Vec<(String, String)>,
    #[serde(default)]
    pub body: Option<String>,
    /// Timestamp in epoch millis. Defaults to a fixed value if omitted.
    #[serde(default = "default_test_timestamp_ms")]
    pub timestamp_ms: u64,
}

fn default_test_timestamp_ms() -> u64 {
    // 2026-04-01T12:00:00Z — midday UTC, within business hours
    1774958400000
}

/// Expected result from evaluating a test case.
#[typeshare]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PolicyTestExpectation {
    pub allowed: bool,
    /// If set, assert the matched rule name matches.
    #[serde(default)]
    pub matched_rule: Option<String>,
}

/// Result of running a single test case.
#[typeshare]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PolicyTestResult {
    pub name: String,
    pub passed: bool,
    pub expected_allowed: bool,
    pub actual_allowed: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub expected_rule: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub actual_rule: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// Summary of running a test suite.
#[typeshare]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PolicyTestSummary {
    #[typeshare(serialized_as = "number")]
    pub total: usize,
    #[typeshare(serialized_as = "number")]
    pub passed: usize,
    #[typeshare(serialized_as = "number")]
    pub failed: usize,
    pub results: Vec<PolicyTestResult>,
}

// --- Policy diff types ---

/// Semantic diff between two policy versions (Terraform plan-style).
#[typeshare]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PolicyDiff {
    pub summary: PolicyDiffSummary,
    pub default_action: PolicyDiffAction,
    pub mode: PolicyDiffAction,
    pub rules: Vec<RuleDiffEntry>,
}

#[typeshare]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PolicyDiffSummary {
    #[typeshare(serialized_as = "number")]
    pub added: usize,
    #[typeshare(serialized_as = "number")]
    pub modified: usize,
    #[typeshare(serialized_as = "number")]
    pub removed: usize,
    #[typeshare(serialized_as = "number")]
    pub unchanged: usize,
}

#[typeshare]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PolicyDiffAction {
    pub action: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub before: Option<serde_json::Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub after: Option<serde_json::Value>,
}

#[typeshare]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RuleDiffEntry {
    pub name: String,
    pub action: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub before: Option<serde_json::Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub after: Option<serde_json::Value>,
}

// --- Policy merge (org + agent composition) ---

/// Merge an org-level default policy with an agent-level policy.
///
/// Follows AWS IAM SCP model: org deny rules are unremovable guardrails.
///
/// Merge semantics:
/// - `default_action`: agent overrides org (agent is more specific scope)
/// - `mode`: agent overrides org
/// - `deny` rules: UNION (org denies + agent denies). Org denies are guardrails.
/// - `allow` rules: agent replaces org allows (if agent has any allows)
/// - `rate limits`: most-restrictive-wins (min calls_per_minute per scope key)
///
/// Each rule in the merged result gets a `source` annotation ("org" or "agent").
pub fn merge_policies(org: &PolicyConfig, agent: &PolicyConfig) -> PolicyConfig {
    // default_action: agent wins
    let default = agent.default;

    // mode: agent wins
    let mode = agent.mode;

    // Collect deny rules from both (union, org first)
    let mut rules: Vec<PolicyRule> = Vec::new();
    for rule in &org.rules {
        if matches!(rule.kind, PolicyRuleKind::Deny(_)) {
            let mut r = rule.clone();
            r.source = Some("org".into());
            rules.push(r);
        }
    }
    for rule in &agent.rules {
        if matches!(rule.kind, PolicyRuleKind::Deny(_)) {
            let mut r = rule.clone();
            r.source = Some("agent".into());
            rules.push(r);
        }
    }

    // Allow rules: agent replaces org (if agent has any allows)
    let agent_has_allows = agent
        .rules
        .iter()
        .any(|r| matches!(r.kind, PolicyRuleKind::Allow(_)));
    if agent_has_allows {
        for rule in &agent.rules {
            if matches!(rule.kind, PolicyRuleKind::Allow(_)) {
                let mut r = rule.clone();
                r.source = Some("agent".into());
                rules.push(r);
            }
        }
    } else {
        for rule in &org.rules {
            if matches!(rule.kind, PolicyRuleKind::Allow(_)) {
                let mut r = rule.clone();
                r.source = Some("org".into());
                rules.push(r);
            }
        }
    }

    // Rate limits: most-restrictive-wins per scope key
    let mut rate_limit_map: std::collections::BTreeMap<String, (PolicyRule, u32)> =
        std::collections::BTreeMap::new();
    for rule in org.rules.iter().chain(agent.rules.iter()) {
        if let PolicyRuleKind::Limit(ref config) = rule.kind {
            let scope_key = match config.per {
                RateLimitScope::Global => "__global__".to_string(),
                RateLimitScope::Endpoint => "__endpoint__".to_string(),
                RateLimitScope::BodyField => {
                    format!("bf:{}", config.field.as_deref().unwrap_or(""))
                }
            };
            let source = if org.rules.contains(rule) {
                "org"
            } else {
                "agent"
            };
            match rate_limit_map.get(&scope_key) {
                Some((_, existing_cpm)) if config.calls_per_minute >= *existing_cpm => {
                    // Existing is more restrictive, keep it
                }
                _ => {
                    let mut r = rule.clone();
                    r.source = Some(source.into());
                    rate_limit_map.insert(scope_key, (r, config.calls_per_minute));
                }
            }
        }
    }
    for (_, (rule, _)) in rate_limit_map {
        rules.push(rule);
    }

    PolicyConfig {
        default,
        mode,
        rules,
    }
}

// --- Policy conflict detection (static analysis) ---

/// Warning from static policy analysis.
#[typeshare]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PolicyWarning {
    pub kind: String,
    pub severity: String,
    pub rule: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub related_rule: Option<String>,
    pub message: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub suggestion: Option<String>,
}

/// Result of static policy analysis.
#[typeshare]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PolicyAnalysis {
    pub warnings: Vec<PolicyWarning>,
    pub summary: PolicyAnalysisSummary,
}

#[typeshare]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PolicyAnalysisSummary {
    #[typeshare(serialized_as = "number")]
    pub errors: usize,
    #[typeshare(serialized_as = "number")]
    pub warnings: usize,
    #[typeshare(serialized_as = "number")]
    pub info: usize,
}

/// Analyze a policy for conflicts, shadows, and redundancies.
///
/// Runs in O(n^2) time — safe for synchronous execution at policy creation
/// (max 200 rules = 40K comparisons, <1ms).
pub fn analyze_policy(config: &PolicyConfig) -> PolicyAnalysis {
    let mut warnings = Vec::new();

    // Collect typed rule lists for pairwise analysis
    let deny_rules: Vec<&PolicyRule> = config
        .rules
        .iter()
        .filter(|r| matches!(r.kind, PolicyRuleKind::Deny(_)))
        .collect();
    let allow_rules: Vec<&PolicyRule> = config
        .rules
        .iter()
        .filter(|r| matches!(r.kind, PolicyRuleKind::Allow(_)))
        .collect();
    let rate_limits: Vec<&PolicyRule> = config
        .rules
        .iter()
        .filter(|r| matches!(r.kind, PolicyRuleKind::Limit(_)))
        .collect();

    // Check 1: Contradictory rules (allow + deny with same matchers)
    for deny in &deny_rules {
        let deny_matcher = match &deny.kind {
            PolicyRuleKind::Deny(m) => m,
            _ => continue,
        };
        for allow in &allow_rules {
            let allow_matcher = match &allow.kind {
                PolicyRuleKind::Allow(m) => m,
                _ => continue,
            };
            if matchers_overlap(deny_matcher, allow_matcher) {
                warnings.push(PolicyWarning {
                    kind: "contradictory_rules".into(),
                    severity: "warning".into(),
                    rule: allow.name.clone(),
                    related_rule: Some(deny.name.clone()),
                    message: format!(
                        "Allow rule '{}' overlaps with deny rule '{}'. \
                         Deny rules are evaluated first, so matching requests \
                         will always be denied.",
                        allow.name, deny.name
                    ),
                    suggestion: Some(
                        "Narrow the deny rule's scope or remove the \
                         overlapping allow rule."
                            .into(),
                    ),
                });
            }
        }
    }

    // Check 2: Redundant rules (same type, same matchers)
    for (i, rule_a) in deny_rules.iter().enumerate() {
        for rule_b in deny_rules.iter().skip(i + 1) {
            let m_a = match &rule_a.kind {
                PolicyRuleKind::Deny(m) => m,
                _ => continue,
            };
            let m_b = match &rule_b.kind {
                PolicyRuleKind::Deny(m) => m,
                _ => continue,
            };
            if matchers_equivalent(m_a, m_b) {
                warnings.push(PolicyWarning {
                    kind: "redundant_rule".into(),
                    severity: "info".into(),
                    rule: rule_b.name.clone(),
                    related_rule: Some(rule_a.name.clone()),
                    message: format!(
                        "Rule '{}' is redundant with '{}' — both match the same requests.",
                        rule_b.name, rule_a.name
                    ),
                    suggestion: Some("Remove the duplicate rule.".into()),
                });
            }
        }
    }
    for (i, rule_a) in allow_rules.iter().enumerate() {
        for rule_b in allow_rules.iter().skip(i + 1) {
            let m_a = match &rule_a.kind {
                PolicyRuleKind::Allow(m) => m,
                _ => continue,
            };
            let m_b = match &rule_b.kind {
                PolicyRuleKind::Allow(m) => m,
                _ => continue,
            };
            if matchers_equivalent(m_a, m_b) {
                warnings.push(PolicyWarning {
                    kind: "redundant_rule".into(),
                    severity: "info".into(),
                    rule: rule_b.name.clone(),
                    related_rule: Some(rule_a.name.clone()),
                    message: format!(
                        "Rule '{}' is redundant with '{}' — both match the same requests.",
                        rule_b.name, rule_a.name
                    ),
                    suggestion: Some("Remove the duplicate rule.".into()),
                });
            }
        }
    }

    // Check 3: Unreachable allow with default deny and no allows
    if config.default == DefaultAction::Deny && allow_rules.is_empty() && !deny_rules.is_empty() {
        warnings.push(PolicyWarning {
            kind: "unreachable_config".into(),
            severity: "warning".into(),
            rule: "(default)".into(),
            related_rule: None,
            message:
                "Policy has default:deny with deny rules but no allow rules — all requests denied."
                    .into(),
            suggestion: Some("Add allow rules for permitted traffic patterns.".into()),
        });
    }

    // Check 4: Overly broad allow
    for rule in &allow_rules {
        let matcher = match &rule.kind {
            PolicyRuleKind::Allow(m) => m,
            _ => continue,
        };
        if matcher.method.is_empty() && matcher.url.as_deref() == Some("*") {
            warnings.push(PolicyWarning {
                kind: "overly_broad_allow".into(),
                severity: "warning".into(),
                rule: rule.name.clone(),
                related_rule: None,
                message: format!(
                    "Rule '{}' allows all methods on all URLs — \
                     this effectively disables default:deny.",
                    rule.name
                ),
                suggestion: Some("Restrict to specific methods or URL patterns.".into()),
            });
        }
    }

    // Check 5: Redundant rate limits (same scope)
    for (i, rule_a) in rate_limits.iter().enumerate() {
        let config_a = match &rule_a.kind {
            PolicyRuleKind::Limit(c) => c,
            _ => continue,
        };
        for rule_b in rate_limits.iter().skip(i + 1) {
            let config_b = match &rule_b.kind {
                PolicyRuleKind::Limit(c) => c,
                _ => continue,
            };
            if config_a.per == config_b.per && config_a.field == config_b.field {
                let less_restrictive = if config_a.calls_per_minute >= config_b.calls_per_minute {
                    &rule_a.name
                } else {
                    &rule_b.name
                };
                warnings.push(PolicyWarning {
                    kind: "redundant_rate_limit".into(),
                    severity: "warning".into(),
                    rule: less_restrictive.clone(),
                    related_rule: Some(if less_restrictive == &rule_a.name {
                        rule_b.name.clone()
                    } else {
                        rule_a.name.clone()
                    }),
                    message: format!(
                        "Rate limit '{}' is less restrictive than '{}' for the same scope — \
                         the more restrictive limit will always apply first.",
                        less_restrictive,
                        if less_restrictive == &rule_a.name {
                            &rule_b.name
                        } else {
                            &rule_a.name
                        }
                    ),
                    suggestion: Some("Remove the less restrictive rate limit.".into()),
                });
            }
        }
    }

    let errors = warnings.iter().filter(|w| w.severity == "error").count();
    let warning_count = warnings.iter().filter(|w| w.severity == "warning").count();
    let info = warnings.iter().filter(|w| w.severity == "info").count();

    PolicyAnalysis {
        warnings,
        summary: PolicyAnalysisSummary {
            errors,
            warnings: warning_count,
            info,
        },
    }
}

/// Check if two matchers overlap (could match the same request).
/// Conservative: returns true if overlap is possible, false only if provably disjoint.
fn matchers_overlap(a: &RequestMatcher, b: &RequestMatcher) -> bool {
    // Methods: disjoint if both specify methods with no intersection
    if !a.method.is_empty()
        && !b.method.is_empty()
        && !a.method.iter().any(|m| b.method.contains(m))
    {
        return false;
    }

    // URL: disjoint if both specify URL patterns that don't overlap
    match (&a.url, &b.url) {
        (Some(url_a), Some(url_b)) => {
            // Conservative: if either is "*" or "**", they overlap
            if url_a == "*" || url_a == "**" || url_b == "*" || url_b == "**" {
                return true;
            }
            // If patterns are identical, they overlap
            if url_a == url_b {
                return true;
            }
            // Conservative: assume overlap for complex patterns
            // (full subset analysis is expensive and has diminishing returns)
            true
        }
        _ => true, // Missing URL = matches all
    }
}

/// Check if two matchers are equivalent (match exactly the same requests).
fn matchers_equivalent(a: &RequestMatcher, b: &RequestMatcher) -> bool {
    a.method == b.method && a.url == b.url && a.body == b.body && a.headers == b.headers
}

/// Compute a semantic diff between two policy configs.
pub fn diff_policies(before: &PolicyConfig, after: &PolicyConfig) -> PolicyDiff {
    use std::collections::BTreeMap;

    // Diff default action
    let default_action = if before.default == after.default {
        PolicyDiffAction {
            action: "no_op".into(),
            before: None,
            after: None,
        }
    } else {
        PolicyDiffAction {
            action: "update".into(),
            before: Some(serde_json::to_value(before.default).unwrap_or_default()),
            after: Some(serde_json::to_value(after.default).unwrap_or_default()),
        }
    };

    // Diff mode
    let mode = if before.mode == after.mode {
        PolicyDiffAction {
            action: "no_op".into(),
            before: None,
            after: None,
        }
    } else {
        PolicyDiffAction {
            action: "update".into(),
            before: Some(serde_json::to_value(before.mode).unwrap_or_default()),
            after: Some(serde_json::to_value(after.mode).unwrap_or_default()),
        }
    };

    // Index rules by name for semantic comparison
    let before_rules: BTreeMap<&str, &PolicyRule> =
        before.rules.iter().map(|r| (r.name.as_str(), r)).collect();
    let after_rules: BTreeMap<&str, &PolicyRule> =
        after.rules.iter().map(|r| (r.name.as_str(), r)).collect();

    let mut rules = Vec::new();
    let mut added = 0;
    let mut modified = 0;
    let mut removed = 0;
    let mut unchanged = 0;

    // Check for modified/unchanged/removed rules
    for (name, before_rule) in &before_rules {
        match after_rules.get(name) {
            Some(after_rule) => {
                if before_rule == after_rule {
                    unchanged += 1;
                } else {
                    modified += 1;
                    rules.push(RuleDiffEntry {
                        name: name.to_string(),
                        action: "update".into(),
                        before: Some(serde_json::to_value(before_rule).unwrap_or_default()),
                        after: Some(serde_json::to_value(after_rule).unwrap_or_default()),
                    });
                }
            }
            None => {
                removed += 1;
                rules.push(RuleDiffEntry {
                    name: name.to_string(),
                    action: "delete".into(),
                    before: Some(serde_json::to_value(before_rule).unwrap_or_default()),
                    after: None,
                });
            }
        }
    }

    // Check for added rules
    for (name, after_rule) in &after_rules {
        if !before_rules.contains_key(name) {
            added += 1;
            rules.push(RuleDiffEntry {
                name: name.to_string(),
                action: "create".into(),
                before: None,
                after: Some(serde_json::to_value(after_rule).unwrap_or_default()),
            });
        }
    }

    PolicyDiff {
        summary: PolicyDiffSummary {
            added,
            modified,
            removed,
            unchanged,
        },
        default_action,
        mode,
        rules,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn policy_config_round_trip() {
        let config = PolicyConfig {
            default: DefaultAction::Deny,
            mode: PolicyMode::default(),
            rules: vec![
                PolicyRule {
                    name: "read-contacts".into(),
                    kind: PolicyRuleKind::Allow(RequestMatcher {
                        method: vec![HttpMethod::GET],
                        url: Some("api.salesforce.com/*/sobjects/Contact/*".into()),
                        body: vec![],
                        headers: vec![],
                        time_outside: None,
                        timezone: None,
                    }),
                    source: None,
                },
                PolicyRule {
                    name: "block-all-deletes".into(),
                    kind: PolicyRuleKind::Deny(RequestMatcher {
                        method: vec![HttpMethod::DELETE],
                        url: Some("*".into()),
                        body: vec![],
                        headers: vec![],
                        time_outside: None,
                        timezone: None,
                    }),
                    source: None,
                },
                PolicyRule {
                    name: "rate-limit".into(),
                    kind: PolicyRuleKind::Limit(RateLimitConfig {
                        calls_per_minute: 60,
                        per: RateLimitScope::Endpoint,
                        field: None,
                    }),
                    source: None,
                },
            ],
        };

        let json = serde_json::to_string(&config).unwrap();
        let deserialized: PolicyConfig = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized, config);
    }

    #[test]
    fn default_action_serialization() {
        assert_eq!(
            serde_json::to_string(&DefaultAction::Allow).unwrap(),
            "\"allow\""
        );
        assert_eq!(
            serde_json::to_string(&DefaultAction::Deny).unwrap(),
            "\"deny\""
        );
    }

    #[test]
    fn policy_result_serialization() {
        assert_eq!(
            serde_json::to_string(&PolicyResult::Allowed).unwrap(),
            "\"allowed\""
        );
        assert_eq!(
            serde_json::to_string(&PolicyResult::Denied).unwrap(),
            "\"denied\""
        );
    }

    #[test]
    fn body_matcher_with_max() {
        let matcher = BodyMatcher {
            jsonpath: "$.amount".into(),
            max: Some(50000),
            min: None,
            exact: None,
            in_values: None,
            prefix: None,
            suffix: None,
            contains: None,
            regex: None,
        };
        let json = serde_json::to_string(&matcher).unwrap();
        let deserialized: BodyMatcher = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized, matcher);
    }

    #[test]
    fn evaluation_result_round_trip() {
        let result = EvaluationResult {
            allowed: true,
            deny_reason: None,
            matched_rule: None,
            matched_rule_kind: None,
            mode: PolicyMode::Enforce,
            evaluation_path: vec![],
            log_event_json: "{}".into(),
            request_id: "req-123".into(),
        };
        let json = serde_json::to_string(&result).unwrap();
        let deserialized: EvaluationResult = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized, result);
    }

    #[test]
    fn deserialize_allow_rule_from_json() {
        let json = r#"{
            "name": "read-contacts",
            "allow": {
                "method": ["GET"],
                "url": "api.salesforce.com/*/sobjects/Contact/*"
            }
        }"#;

        let rule: PolicyRule = serde_json::from_str(json).unwrap();
        assert_eq!(rule.name, "read-contacts");
        assert!(matches!(rule.kind, PolicyRuleKind::Allow(_)));
    }

    #[test]
    fn deserialize_deny_rule_from_json() {
        let json = r#"{
            "name": "business-hours-only",
            "deny": {
                "time_outside": "09:00-17:00",
                "timezone": "America/New_York"
            }
        }"#;

        let rule: PolicyRule = serde_json::from_str(json).unwrap();
        assert_eq!(rule.name, "business-hours-only");
        if let PolicyRuleKind::Deny(matcher) = &rule.kind {
            assert_eq!(matcher.time_outside.as_deref(), Some("09:00-17:00"));
            assert!(matcher.method.is_empty());
            assert!(matcher.url.is_none());
        } else {
            panic!("expected Deny rule");
        }
    }

    #[test]
    fn deserialize_limit_rule_from_json() {
        let json = r#"{
            "name": "rate-limit",
            "limit": {
                "calls_per_minute": 60,
                "per": "endpoint"
            }
        }"#;

        let rule: PolicyRule = serde_json::from_str(json).unwrap();
        if let PolicyRuleKind::Limit(config) = &rule.kind {
            assert_eq!(config.calls_per_minute, 60);
            assert_eq!(config.per, RateLimitScope::Endpoint);
        } else {
            panic!("expected Limit rule");
        }
    }

    #[test]
    fn request_matcher_missing_optional_fields() {
        let json = r#"{"method": ["POST"]}"#;
        let matcher: RequestMatcher = serde_json::from_str(json).unwrap();
        assert_eq!(matcher.method, vec![HttpMethod::POST]);
        assert!(matcher.url.is_none());
        assert!(matcher.body.is_empty());
        assert!(matcher.time_outside.is_none());
        assert!(matcher.timezone.is_none());
    }

    #[test]
    fn body_matcher_single_object_deserializes() {
        let json = r#"{"method": ["POST"], "body": {"jsonpath": "$.model", "exact": "gpt-4o"}}"#;
        let matcher: RequestMatcher = serde_json::from_str(json).unwrap();
        assert_eq!(matcher.body.len(), 1);
        assert_eq!(matcher.body[0].jsonpath, "$.model");
        assert_eq!(
            matcher.body[0].exact,
            Some(serde_json::Value::String("gpt-4o".into()))
        );
    }

    #[test]
    fn body_matcher_array_deserializes() {
        let json = r#"{"method": ["POST"], "body": [
            {"jsonpath": "$.amount", "max": 50000},
            {"jsonpath": "$.model", "exact": "gpt-4o"}
        ]}"#;
        let matcher: RequestMatcher = serde_json::from_str(json).unwrap();
        assert_eq!(matcher.body.len(), 2);
        assert_eq!(matcher.body[0].max, Some(50000));
        assert_eq!(
            matcher.body[1].exact,
            Some(serde_json::Value::String("gpt-4o".into()))
        );
    }

    #[test]
    fn body_matcher_in_values_deserializes() {
        let json = r#"{"jsonpath": "$.model", "in": ["gpt-4o", "claude-3-5-sonnet"]}"#;
        let matcher: BodyMatcher = serde_json::from_str(json).unwrap();
        assert_eq!(matcher.in_values.as_ref().unwrap().len(), 2);
    }

    #[test]
    fn header_matcher_deserializes() {
        let json = r#"{"method": ["POST"], "headers": [
            {"name": "user-agent", "contains": "bot"},
            {"name": "x-debug", "present": true}
        ]}"#;
        let matcher: RequestMatcher = serde_json::from_str(json).unwrap();
        assert_eq!(matcher.headers.len(), 2);
        assert_eq!(matcher.headers[0].name, "user-agent");
        assert_eq!(matcher.headers[0].contains, Some("bot".into()));
        assert_eq!(matcher.headers[1].present, Some(true));
    }

    #[test]
    fn rate_limit_body_field_deserializes() {
        let json = r#"{
            "name": "per-model-limit",
            "limit": {
                "calls_per_minute": 100,
                "per": "body_field",
                "field": "$.model"
            }
        }"#;
        let rule: PolicyRule = serde_json::from_str(json).unwrap();
        if let PolicyRuleKind::Limit(config) = &rule.kind {
            assert_eq!(config.per, RateLimitScope::BodyField);
            assert_eq!(config.field.as_deref(), Some("$.model"));
        } else {
            panic!("expected Limit rule");
        }
    }

    #[test]
    fn evaluation_request_round_trip() {
        let req = EvaluationRequest {
            request_id: "req-456".into(),
            method: HttpMethod::POST,
            url: "https://api.stripe.com/v1/charges".into(),
            headers: vec![("Content-Type".into(), "application/json".into())],
            body: Some("{\"amount\": 1000}".into()),
            timestamp: "2026-03-28T14:30:00Z".into(),
            timestamp_ms: 1774708200000,
            trace_id: "0af7651916cd43dd8448eb211c80319c".into(),
            span_id: "b7ad6b7169203331".into(),
            parent_span_id: Some("00f067aa0ba902b7".into()),
        };
        let json = serde_json::to_string(&req).unwrap();
        let deserialized: EvaluationRequest = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized, req);
    }
}
