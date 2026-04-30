use checkrd_shared::{
    BodyMatcher, DefaultAction, EvaluationRequest, EvaluationStep, HeaderMatcher, HttpMethod,
    PolicyConfig, PolicyError, PolicyMode, PolicyRuleKind, RateLimitConfig, RequestMatcher,
};

use crate::ratelimit::RateLimiter;
use crate::util;

// --- URL pattern matching ---

#[derive(Debug, Clone, PartialEq)]
enum PatternSegment {
    Literal(String),
    Wildcard,
    /// Matches zero or more segments (gitignore-style `**`).
    DoubleWildcard,
}

fn parse_pattern(pattern: &str) -> Vec<PatternSegment> {
    if pattern == "*" {
        return vec![PatternSegment::Wildcard];
    }
    if pattern == "**" {
        return vec![PatternSegment::DoubleWildcard];
    }

    pattern
        .split('/')
        .map(|seg| match seg {
            "**" => PatternSegment::DoubleWildcard,
            "*" => PatternSegment::Wildcard,
            _ => PatternSegment::Literal(seg.to_string()),
        })
        .collect()
}

fn url_matches(pattern_segments: &[PatternSegment], normalized_url: &str) -> bool {
    if pattern_segments.len() == 1 && pattern_segments[0] == PatternSegment::Wildcard {
        return true;
    }
    if pattern_segments.len() == 1 && pattern_segments[0] == PatternSegment::DoubleWildcard {
        return true;
    }

    let normalized = normalized_url.strip_suffix('/').unwrap_or(normalized_url);
    let url_segments: Vec<&str> = normalized.split('/').collect();

    // Use recursive matching when `**` is present.
    if pattern_segments
        .iter()
        .any(|s| matches!(s, PatternSegment::DoubleWildcard))
    {
        return url_matches_glob(pattern_segments, &url_segments);
    }

    // Fast path: no `**`, require exact segment count.
    if url_segments.len() != pattern_segments.len() {
        return false;
    }

    url_segments
        .iter()
        .zip(pattern_segments)
        .all(|(url_seg, pat_seg)| match pat_seg {
            PatternSegment::Wildcard | PatternSegment::DoubleWildcard => true,
            PatternSegment::Literal(lit) => url_seg == lit,
        })
}

/// Recursive glob matching with `**` (matches zero or more segments).
/// Uses the standard gitignore/Ant algorithm.
fn url_matches_glob(pattern: &[PatternSegment], url: &[&str]) -> bool {
    match (pattern.first(), url.first()) {
        (None, None) => true,
        (None, Some(_)) => false,
        (Some(PatternSegment::DoubleWildcard), _) => {
            // `**` can match zero segments (skip it) or consume one segment.
            url_matches_glob(&pattern[1..], url)
                || (!url.is_empty() && url_matches_glob(pattern, &url[1..]))
        }
        (Some(_), None) => {
            // Remaining pattern segments must all be `**` to match empty URL.
            pattern
                .iter()
                .all(|s| matches!(s, PatternSegment::DoubleWildcard))
        }
        (Some(PatternSegment::Wildcard), Some(_)) => url_matches_glob(&pattern[1..], &url[1..]),
        (Some(PatternSegment::Literal(lit)), Some(seg)) => {
            lit == seg && url_matches_glob(&pattern[1..], &url[1..])
        }
    }
}

fn specificity(segments: &[PatternSegment]) -> u32 {
    segments
        .iter()
        .filter(|s| matches!(s, PatternSegment::Literal(_)))
        .count() as u32
}

// --- Method filtering ---

fn method_matches(matcher: &RequestMatcher, method: &HttpMethod) -> bool {
    matcher.method.is_empty() || matcher.method.contains(method)
}

// --- Body field inspection ---

fn resolve_jsonpath<'a>(value: &'a serde_json::Value, path: &str) -> Option<&'a serde_json::Value> {
    let path = path.strip_prefix("$.").unwrap_or(path);
    let mut current = value;
    for segment in path.split('.') {
        // Support array index notation: "messages.0.role"
        if let Ok(idx) = segment.parse::<usize>() {
            current = current.get(idx)?;
        } else {
            current = current.get(segment)?;
        }
    }
    Some(current)
}

/// Evaluate a single BodyMatcher against a JSON body.
///
/// All specified operators are ANDed: each operator present must match.
/// If no operators are set, checks field existence only.
///
/// `fail_closed`: when true, unparseable/missing body causes the matcher
/// to return true (fires deny rules). When false, returns false (skips
/// allow rules). This asymmetry is a critical safety property.
fn body_matches(
    matcher: &BodyMatcher,
    body: &Option<String>,
    fail_closed: bool,
    compiled_regex: Option<&regex::Regex>,
) -> bool {
    let body_str = match body {
        Some(b) if !b.is_empty() => b,
        Some(_) => return fail_closed, // Body present but unparseable (empty string sentinel)
        None => return false,          // No body at all (GET/HEAD) -- matcher doesn't apply
    };

    let value: serde_json::Value = match serde_json::from_str(body_str) {
        Ok(v) => v,
        Err(_) => return fail_closed, // Body present but not valid JSON
    };

    let field = match resolve_jsonpath(&value, &matcher.jsonpath) {
        Some(f) => f,
        None => return false, // Field absent = condition doesn't match (OPA semantics)
    };

    // All operators are ANDed: each specified operator must pass.
    let has_any_operator = matcher.max.is_some()
        || matcher.min.is_some()
        || matcher.exact.is_some()
        || matcher.in_values.is_some()
        || matcher.prefix.is_some()
        || matcher.suffix.is_some()
        || matcher.contains.is_some()
        || matcher.regex.is_some();

    if !has_any_operator {
        return true; // No operators: field existence check only.
    }

    // Numeric operators
    if let Some(max) = matcher.max {
        if field.as_i64().is_none_or(|v| v > max) {
            return false;
        }
    }
    if let Some(min) = matcher.min {
        if field.as_i64().is_none_or(|v| v < min) {
            return false;
        }
    }

    // Value operators (JSON value equality — type-aware)
    if let Some(ref expected) = matcher.exact {
        if field != expected {
            return false;
        }
    }
    if let Some(ref allowed) = matcher.in_values {
        if !allowed.contains(field) {
            return false;
        }
    }

    // String operators (field must be a JSON string)
    if let Some(ref pfx) = matcher.prefix {
        if !field.as_str().is_some_and(|s| s.starts_with(pfx.as_str())) {
            return false;
        }
    }
    if let Some(ref sfx) = matcher.suffix {
        if !field.as_str().is_some_and(|s| s.ends_with(sfx.as_str())) {
            return false;
        }
    }
    if let Some(ref sub) = matcher.contains {
        if !field.as_str().is_some_and(|s| s.contains(sub.as_str())) {
            return false;
        }
    }
    if compiled_regex.is_some() || matcher.regex.is_some() {
        // Pre-compiled regex from from_config(). If compilation failed at config
        // time (invalid pattern), compiled_regex is None and we fail-closed.
        match compiled_regex {
            Some(re) => {
                if !field.as_str().is_some_and(|s| re.is_match(s)) {
                    return false;
                }
            }
            None => return false,
        }
    }

    true
}

/// Evaluate all body matchers (AND semantics: all must pass).
fn body_matchers_match(
    matchers: &[BodyMatcher],
    body: &Option<String>,
    fail_closed: bool,
    compiled_regexes: &[Option<regex::Regex>],
) -> bool {
    if matchers.is_empty() {
        return true; // No body matchers = no body constraint.
    }
    matchers.iter().enumerate().all(|(i, matcher)| {
        body_matches(
            matcher,
            body,
            fail_closed,
            compiled_regexes.get(i).and_then(|r| r.as_ref()),
        )
    })
}

// --- Header matching ---

/// Evaluate all header matchers against request headers.
/// Header names are matched case-insensitively (RFC 9110 / HTTP/2).
/// Multiple matchers use AND semantics.
fn headers_match(
    matchers: &[HeaderMatcher],
    request_headers: &[(String, String)],
    compiled_regexes: &[Option<regex::Regex>],
) -> bool {
    matchers.iter().enumerate().all(|(i, matcher)| {
        let name_lower = matcher.name.to_ascii_lowercase();
        // Find all header values with matching name (case-insensitive).
        // Concatenate with ", " per RFC 9110 field combination.
        let values: Vec<&str> = request_headers
            .iter()
            .filter(|(k, _)| k.to_ascii_lowercase() == name_lower)
            .map(|(_, v)| v.as_str())
            .collect();
        let header_present = !values.is_empty();
        let combined = values.join(", ");

        // Handle `present` operator first.
        if let Some(expected_present) = matcher.present {
            if header_present != expected_present {
                return false;
            }
            // If present was the only check and it passed, continue to other ops.
            // If present: false and header is absent, skip value checks (no value to check).
            if !expected_present && !header_present {
                return true;
            }
        } else if !header_present {
            // No `present` flag, but header is absent = matcher doesn't match.
            return false;
        }

        let has_value_operator = matcher.exact.is_some()
            || matcher.prefix.is_some()
            || matcher.suffix.is_some()
            || matcher.contains.is_some()
            || matcher.regex.is_some();

        if !has_value_operator {
            return true; // presence check only (implicit or explicit)
        }

        // Value operators (against the combined header value string)
        if let Some(ref expected) = matcher.exact {
            if combined != *expected {
                return false;
            }
        }
        if let Some(pfx) = &matcher.prefix {
            if !combined.starts_with(pfx.as_str()) {
                return false;
            }
        }
        if let Some(sfx) = &matcher.suffix {
            if !combined.ends_with(sfx.as_str()) {
                return false;
            }
        }
        if let Some(sub) = &matcher.contains {
            if !combined.contains(sub.as_str()) {
                return false;
            }
        }
        if let Some(re) = compiled_regexes
            .get(i)
            .and_then(|r: &Option<regex::Regex>| r.as_ref())
        {
            if !re.is_match(&combined) {
                return false;
            }
        }

        true
    })
}

/// Extract a body field value as a string for use as a rate limit key.
fn extract_body_field_key(body: &Option<String>, field_path: &str) -> Option<String> {
    let body_str = body.as_ref()?;
    let value: serde_json::Value = serde_json::from_str(body_str).ok()?;
    let field = resolve_jsonpath(&value, field_path)?;
    match field {
        serde_json::Value::String(s) => Some(s.clone()),
        serde_json::Value::Number(n) => Some(n.to_string()),
        serde_json::Value::Bool(b) => Some(b.to_string()),
        _ => None,
    }
}

// --- Time-of-day restriction ---

struct TimeOfDay {
    hour: u32,
    minute: u32,
}

fn parse_time(s: &str) -> Option<TimeOfDay> {
    let (h, m) = s.split_once(':')?;
    Some(TimeOfDay {
        hour: h.parse().ok()?,
        minute: m.parse().ok()?,
    })
}

fn parse_timezone_offset_minutes(tz: &str) -> Option<i32> {
    match tz {
        "UTC" | "utc" => Some(0),
        s if s.starts_with('+') || s.starts_with('-') => {
            let (h, m) = s[1..].split_once(':')?;
            let hours: i32 = h.parse().ok()?;
            let minutes: i32 = m.parse().ok()?;
            let total = hours * 60 + minutes;
            if s.starts_with('-') {
                Some(-total)
            } else {
                Some(total)
            }
        }
        _ => None, // IANA timezones not supported in Phase 1
    }
}

fn time_outside_matches(time_range: &str, timezone: Option<&str>, timestamp_ms: u64) -> bool {
    let offset_minutes = match timezone {
        Some(tz) => match parse_timezone_offset_minutes(tz) {
            Some(offset) => offset,
            None => return true, // unsupported timezone, fail-closed (rule fires)
        },
        None => 0, // default UTC
    };

    let (start_str, end_str) = match time_range.split_once('-') {
        Some(pair) => pair,
        None => return false,
    };

    let start = match parse_time(start_str) {
        Some(t) => t,
        None => return false,
    };
    let end = match parse_time(end_str) {
        Some(t) => t,
        None => return false,
    };

    let total_secs = (timestamp_ms / 1000) as i64;
    let adjusted_secs = total_secs + (offset_minutes as i64 * 60);
    let day_secs = adjusted_secs.rem_euclid(86400) as u32;
    let current_hour = day_secs / 3600;
    let current_minute = (day_secs % 3600) / 60;
    let current_minutes = current_hour * 60 + current_minute;

    let start_minutes = start.hour * 60 + start.minute;
    let end_minutes = end.hour * 60 + end.minute;

    let within_range = if start_minutes <= end_minutes {
        // Normal range: 09:00-17:00
        current_minutes >= start_minutes && current_minutes < end_minutes
    } else {
        // Midnight crossing: 22:00-06:00
        current_minutes >= start_minutes || current_minutes < end_minutes
    };

    !within_range // rule fires when OUTSIDE the range
}

// --- Request matching (combines all matchers) ---

fn request_matches(
    rule: &CompiledRule,
    request: &EvaluationRequest,
    normalized_url: &str,
    fail_closed: bool,
) -> bool {
    let matcher = &rule.matcher;

    if !method_matches(matcher, &request.method) {
        return false;
    }

    if !rule.url_segments.is_empty() && !url_matches(&rule.url_segments, normalized_url) {
        return false;
    }

    if !body_matchers_match(
        &matcher.body,
        &request.body,
        fail_closed,
        &rule.body_regexes,
    ) {
        return false;
    }

    if !matcher.headers.is_empty()
        && !headers_match(&matcher.headers, &request.headers, &rule.header_regexes)
    {
        return false;
    }

    if let Some(time_range) = &matcher.time_outside {
        if !time_outside_matches(
            time_range,
            matcher.timezone.as_deref(),
            request.timestamp_ms,
        ) {
            return false;
        }
    }

    true
}

// --- Compiled rule (pre-parsed for fast evaluation) ---

struct CompiledRule {
    name: String,
    matcher: RequestMatcher,
    url_segments: Vec<PatternSegment>,
    specificity: u32,
    /// Pre-compiled regexes for body matchers (one per body matcher, None if no regex).
    body_regexes: Vec<Option<regex::Regex>>,
    /// Pre-compiled regexes for header matchers.
    header_regexes: Vec<Option<regex::Regex>>,
}

impl CompiledRule {
    fn from_matcher(name: String, matcher: RequestMatcher) -> Self {
        let segments = matcher
            .url
            .as_deref()
            .map(parse_pattern)
            .unwrap_or_default();
        let spec = specificity(&segments);

        // Pre-compile regex patterns (Envoy's safe_regex_match equivalent).
        // regex crate uses DFA/NFA — linear-time, no ReDoS.
        let body_regexes: Vec<Option<regex::Regex>> = matcher
            .body
            .iter()
            .map(|bm| {
                bm.regex
                    .as_ref()
                    .and_then(|pat| regex::Regex::new(pat).ok())
            })
            .collect();

        let header_regexes: Vec<Option<regex::Regex>> = matcher
            .headers
            .iter()
            .map(|hm| {
                hm.regex
                    .as_ref()
                    .and_then(|pat| regex::Regex::new(pat).ok())
            })
            .collect();

        Self {
            name,
            url_segments: segments,
            specificity: spec,
            body_regexes,
            header_regexes,
            matcher,
        }
    }
}

// --- Evaluation outcome ---

/// Structured result from policy evaluation.
/// Contains the decision, the rule that matched, and the full evaluation trace.
pub struct EvaluationOutcome {
    pub allowed: bool,
    pub deny_reason: Option<String>,
    pub matched_rule: Option<String>,
    pub matched_rule_kind: Option<String>,
    pub mode: PolicyMode,
    pub evaluation_path: Vec<EvaluationStep>,
}

// --- Policy engine ---

pub struct PolicyEngine {
    default: DefaultAction,
    mode: PolicyMode,
    deny_rules: Vec<CompiledRule>,
    allow_rules: Vec<CompiledRule>,
    rate_limits: Vec<(String, RateLimitConfig)>,
}

impl PolicyEngine {
    pub fn from_config(config: PolicyConfig) -> Result<Self, PolicyError> {
        let mut deny_rules = Vec::new();
        let mut allow_rules = Vec::new();
        let mut rate_limits = Vec::new();

        for rule in config.rules {
            match rule.kind {
                PolicyRuleKind::Allow(matcher) => {
                    allow_rules.push(CompiledRule::from_matcher(rule.name, matcher));
                }
                PolicyRuleKind::Deny(matcher) => {
                    deny_rules.push(CompiledRule::from_matcher(rule.name, matcher));
                }
                PolicyRuleKind::Limit(config) => {
                    rate_limits.push((rule.name, config));
                }
                _ => {} // ignore unknown rule kinds from future versions
            }
        }

        // Most specific rules first
        deny_rules.sort_by_key(|r| std::cmp::Reverse(r.specificity));
        allow_rules.sort_by_key(|r| std::cmp::Reverse(r.specificity));

        Ok(Self {
            default: config.default,
            mode: config.mode,
            deny_rules,
            allow_rules,
            rate_limits,
        })
    }

    pub fn mode(&self) -> PolicyMode {
        self.mode
    }

    pub fn rate_limits(&self) -> &[(String, RateLimitConfig)] {
        &self.rate_limits
    }

    /// Evaluate a request and return the legacy `(allowed, deny_reason)` tuple.
    /// Delegates to `evaluate_full()` for backwards compatibility.
    pub fn evaluate(
        &self,
        request: &EvaluationRequest,
        rate_limiter: &mut RateLimiter,
        parsed_url: &util::ParsedUrl,
    ) -> (bool, Option<String>) {
        let outcome = self.evaluate_full(request, rate_limiter, parsed_url);
        (outcome.allowed, outcome.deny_reason)
    }

    /// Evaluate a request and return a structured `EvaluationOutcome` with the
    /// matched rule, evaluation path, and dry-run mode. This is the primary
    /// evaluation method — `evaluate()` delegates to it.
    ///
    /// The evaluation path follows OPA decision log / AWS IAM MatchedStatements
    /// patterns: an ordered list of stages checked with the result of each.
    pub fn evaluate_full(
        &self,
        request: &EvaluationRequest,
        rate_limiter: &mut RateLimiter,
        parsed_url: &util::ParsedUrl,
    ) -> EvaluationOutcome {
        let normalized = format!("{}{}", parsed_url.host, parsed_url.path);
        let mut path = Vec::new();
        let is_dry_run = self.mode == PolicyMode::DryRun;

        // 1. Rate limits — skipped in dry-run (don't corrupt counters)
        for (name, config) in &self.rate_limits {
            let scope_label = match config.per {
                checkrd_shared::RateLimitScope::Endpoint => "endpoint",
                checkrd_shared::RateLimitScope::BodyField => "body_field",
                _ => "global",
            };

            if is_dry_run {
                path.push(EvaluationStep {
                    stage: "rate_limit".into(),
                    rule: Some(name.clone()),
                    result: "skipped".into(),
                    detail: Some(format!("dry_run mode (scope: {scope_label})")),
                });
                continue;
            }
            let key: String = match config.per {
                checkrd_shared::RateLimitScope::Endpoint => normalized.clone(),
                checkrd_shared::RateLimitScope::BodyField => {
                    let field_val = config
                        .field
                        .as_deref()
                        .and_then(|f| extract_body_field_key(&request.body, f));
                    match field_val {
                        Some(val) => {
                            let field = config.field.as_deref().unwrap_or("");
                            format!("bf:{}:{}:{}", field.len(), field, val)
                        }
                        None => "bf:__unknown__".into(),
                    }
                }
                _ => "__global__".into(),
            };
            if rate_limiter
                .check(&key, config.calls_per_minute, request.timestamp_ms)
                .is_exceeded()
            {
                let reason = format!("rate limit '{}' exceeded", name);
                path.push(EvaluationStep {
                    stage: "rate_limit".into(),
                    rule: Some(name.clone()),
                    result: "exceeded".into(),
                    detail: Some(format!("scope: {scope_label}")),
                });
                return EvaluationOutcome {
                    allowed: false,
                    deny_reason: Some(reason),
                    matched_rule: Some(name.clone()),
                    matched_rule_kind: Some("rate_limit".into()),
                    mode: self.mode,
                    evaluation_path: path,
                };
            }
            path.push(EvaluationStep {
                stage: "rate_limit".into(),
                rule: Some(name.clone()),
                result: "pass".into(),
                detail: Some(format!("scope: {scope_label}")),
            });
        }

        // 2. Deny rules (most specific first)
        for rule in &self.deny_rules {
            if request_matches(rule, request, &normalized, true) {
                let reason = format!("denied by rule '{}'", rule.name);
                path.push(EvaluationStep {
                    stage: "deny_rules".into(),
                    rule: Some(rule.name.clone()),
                    result: "matched".into(),
                    detail: None,
                });
                let allowed = is_dry_run; // dry-run: log deny but allow
                return EvaluationOutcome {
                    allowed,
                    deny_reason: Some(reason),
                    matched_rule: Some(rule.name.clone()),
                    matched_rule_kind: Some("deny".into()),
                    mode: self.mode,
                    evaluation_path: path,
                };
            }
            path.push(EvaluationStep {
                stage: "deny_rules".into(),
                rule: Some(rule.name.clone()),
                result: "no_match".into(),
                detail: None,
            });
        }

        // 3. Allow rules (most specific first)
        for rule in &self.allow_rules {
            if request_matches(rule, request, &normalized, false) {
                path.push(EvaluationStep {
                    stage: "allow_rules".into(),
                    rule: Some(rule.name.clone()),
                    result: "matched".into(),
                    detail: None,
                });
                return EvaluationOutcome {
                    allowed: true,
                    deny_reason: None,
                    matched_rule: Some(rule.name.clone()),
                    matched_rule_kind: Some("allow".into()),
                    mode: self.mode,
                    evaluation_path: path,
                };
            }
            path.push(EvaluationStep {
                stage: "allow_rules".into(),
                rule: Some(rule.name.clone()),
                result: "no_match".into(),
                detail: None,
            });
        }

        // 4. Default action
        let (allowed, deny_reason) = match self.default {
            DefaultAction::Allow => (true, None),
            DefaultAction::Deny => (false, Some("denied by default policy".into())),
            _ => (false, Some("denied by default policy".into())),
        };
        let allowed = if is_dry_run && !allowed {
            true
        } else {
            allowed
        };
        path.push(EvaluationStep {
            stage: "default".into(),
            rule: None,
            result: if deny_reason.is_some() {
                "deny".into()
            } else {
                "allow".into()
            },
            detail: None,
        });

        EvaluationOutcome {
            allowed,
            deny_reason,
            matched_rule: None,
            matched_rule_kind: Some("default".into()),
            mode: self.mode,
            evaluation_path: path,
        }
    }
}

// --- Policy test runner ---

use checkrd_shared::{PolicyTestCase, PolicyTestResult, PolicyTestSummary};

/// Run a set of test cases against a policy. Creates a temporary engine with
/// no persistent state (fresh rate limiter per test). Pure function — no I/O.
///
/// This is the core of `checkrd test policy.yaml --tests tests.yaml`.
pub fn run_policy_tests(
    config: &PolicyConfig,
    tests: &[PolicyTestCase],
) -> Result<PolicyTestSummary, PolicyError> {
    let engine = PolicyEngine::from_config(config.clone())?;

    let results: Vec<PolicyTestResult> = tests
        .iter()
        .map(|tc| {
            // Fresh rate limiter per test case — tests are independent.
            let mut rl = RateLimiter::new();
            let request = EvaluationRequest {
                request_id: format!("test-{}", tc.name),
                method: tc.input.method,
                url: tc.input.url.clone(),
                headers: tc.input.headers.clone(),
                body: tc.input.body.clone(),
                timestamp: String::new(),
                timestamp_ms: tc.input.timestamp_ms,
                trace_id: "00000000000000000000000000000000".into(),
                span_id: "0000000000000000".into(),
                parent_span_id: None,
            };

            let parsed_url = util::parse_url(&request.url);
            let outcome = engine.evaluate_full(&request, &mut rl, &parsed_url);

            let allowed_match = outcome.allowed == tc.expect.allowed;
            let rule_match = match &tc.expect.matched_rule {
                Some(expected) => outcome.matched_rule.as_deref() == Some(expected.as_str()),
                None => true, // no rule assertion
            };

            PolicyTestResult {
                name: tc.name.clone(),
                passed: allowed_match && rule_match,
                expected_allowed: tc.expect.allowed,
                actual_allowed: outcome.allowed,
                expected_rule: tc.expect.matched_rule.clone(),
                actual_rule: outcome.matched_rule,
                error: None,
            }
        })
        .collect();

    let passed = results.iter().filter(|r| r.passed).count();
    let failed = results.len() - passed;

    Ok(PolicyTestSummary {
        total: results.len(),
        passed,
        failed,
        results,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use checkrd_shared::{PolicyRule, PolicyRuleKind};

    fn make_request(method: HttpMethod, url: &str) -> EvaluationRequest {
        EvaluationRequest {
            request_id: "test-req".into(),
            method,
            url: url.into(),
            headers: vec![],
            body: None,
            timestamp: "2026-03-28T14:30:00Z".into(),
            timestamp_ms: 1774708200000, // 2026-03-28T14:30:00Z
            trace_id: "0af7651916cd43dd8448eb211c80319c".into(),
            span_id: "b7ad6b7169203331".into(),
            parent_span_id: None,
        }
    }

    fn eval_request(
        engine: &PolicyEngine,
        rl: &mut RateLimiter,
        req: &EvaluationRequest,
    ) -> (bool, Option<String>) {
        let parsed = util::parse_url(&req.url);
        engine.evaluate(req, rl, &parsed)
    }

    // --- URL pattern matching ---

    /// Normalize a raw URL to "host/path" for url_matches tests.
    fn normalize(url: &str) -> String {
        let parsed = util::parse_url(url);
        format!("{}{}", parsed.host, parsed.path)
    }

    #[test]
    fn url_literal_match() {
        let segments = parse_pattern("api.stripe.com/v1/charges");
        assert!(url_matches(
            &segments,
            &normalize("https://api.stripe.com/v1/charges")
        ));
    }

    #[test]
    fn url_literal_no_match() {
        let segments = parse_pattern("api.stripe.com/v1/charges");
        assert!(!url_matches(
            &segments,
            &normalize("https://api.stripe.com/v1/customers")
        ));
    }

    #[test]
    fn url_wildcard_segment() {
        let segments = parse_pattern("api.salesforce.com/*/sobjects/Contact/*");
        assert!(url_matches(
            &segments,
            &normalize("https://api.salesforce.com/v58/sobjects/Contact/001")
        ));
        assert!(!url_matches(
            &segments,
            &normalize("https://api.salesforce.com/v58/sobjects/Account/001")
        ));
    }

    #[test]
    fn url_wildcard_all() {
        let segments = parse_pattern("*");
        assert!(url_matches(
            &segments,
            &normalize("https://anything.com/any/path")
        ));
    }

    #[test]
    fn url_different_segment_count() {
        let segments = parse_pattern("api.stripe.com/v1/charges");
        assert!(!url_matches(
            &segments,
            &normalize("https://api.stripe.com/v1")
        ));
        assert!(!url_matches(
            &segments,
            &normalize("https://api.stripe.com/v1/charges/extra")
        ));
    }

    #[test]
    fn url_trailing_slash() {
        let segments = parse_pattern("api.stripe.com/v1/charges");
        assert!(url_matches(
            &segments,
            &normalize("https://api.stripe.com/v1/charges/")
        ));
    }

    // --- Method filtering ---

    #[test]
    fn method_empty_matches_all() {
        let matcher = RequestMatcher {
            method: vec![],
            url: None,
            body: vec![],
            headers: vec![],
            time_outside: None,
            timezone: None,
        };
        assert!(method_matches(&matcher, &HttpMethod::GET));
        assert!(method_matches(&matcher, &HttpMethod::DELETE));
    }

    #[test]
    fn method_specific_match() {
        let matcher = RequestMatcher {
            method: vec![HttpMethod::GET, HttpMethod::POST],
            url: None,
            body: vec![],
            headers: vec![],
            time_outside: None,
            timezone: None,
        };
        assert!(method_matches(&matcher, &HttpMethod::GET));
        assert!(method_matches(&matcher, &HttpMethod::POST));
        assert!(!method_matches(&matcher, &HttpMethod::DELETE));
    }

    // --- Body inspection ---

    #[test]
    fn body_field_within_max() {
        let matcher = bm("$.amount", Some(50000));
        let body = Some(r#"{"amount": 1000}"#.into());
        assert!(body_matches(&matcher, &body, false, None));
    }

    #[test]
    fn body_field_exceeds_max() {
        let matcher = bm("$.amount", Some(50000));
        let body = Some(r#"{"amount": 100000}"#.into());
        assert!(!body_matches(&matcher, &body, false, None));
    }

    #[test]
    fn body_field_at_exact_max() {
        let matcher = bm("$.amount", Some(50000));
        let body = Some(r#"{"amount": 50000}"#.into());
        assert!(body_matches(&matcher, &body, false, None));
    }

    #[test]
    fn body_field_missing() {
        let matcher = bm("$.amount", Some(50000));
        let body = Some(r#"{"name": "test"}"#.into());
        assert!(!body_matches(&matcher, &body, false, None));
    }

    #[test]
    fn body_no_body() {
        let matcher = bm("$.amount", None);
        assert!(!body_matches(&matcher, &None, false, None));
    }

    #[test]
    fn body_nested_field() {
        let matcher = bm("$.data.amount", Some(100));
        let body = Some(r#"{"data": {"amount": 50}}"#.into());
        assert!(body_matches(&matcher, &body, false, None));
    }

    #[test]
    fn body_presence_check_no_max() {
        let matcher = bm("$.amount", None);
        let body = Some(r#"{"amount": 999999}"#.into());
        assert!(body_matches(&matcher, &body, false, None));
    }

    #[test]
    fn body_invalid_json() {
        let matcher = bm("$.amount", Some(50000));
        let body = Some("not valid json".into());
        assert!(!body_matches(&matcher, &body, false, None));
    }

    #[test]
    fn body_non_numeric_field_with_max() {
        let matcher = bm("$.amount", Some(50000));
        let body = Some(r#"{"amount": "not a number"}"#.into());
        assert!(!body_matches(&matcher, &body, false, None));
    }

    // --- Time-of-day ---

    #[test]
    fn time_within_business_hours() {
        // 14:30 UTC is within 09:00-17:00 UTC, so "outside" is false
        assert!(!time_outside_matches(
            "09:00-17:00",
            Some("UTC"),
            1774708200000
        ));
    }

    #[test]
    fn time_outside_business_hours() {
        // 03:00 UTC is outside 09:00-17:00 UTC
        let ts_3am = 1774666800000; // 2026-03-28T03:00:00Z
        assert!(time_outside_matches("09:00-17:00", Some("UTC"), ts_3am));
    }

    #[test]
    fn time_midnight_crossing() {
        // Range 22:00-06:00 means "active from 22:00 to 06:00"
        // 23:00 UTC is within range, so "outside" is false
        let ts_23 = 1774738800000; // 2026-03-28T23:00:00Z
        assert!(!time_outside_matches("22:00-06:00", Some("UTC"), ts_23));

        // 12:00 UTC is outside range 22:00-06:00, so "outside" is true
        let ts_12 = 1774699200000; // 2026-03-28T12:00:00Z
        assert!(time_outside_matches("22:00-06:00", Some("UTC"), ts_12));
    }

    #[test]
    fn time_unsupported_timezone_fails_open() {
        // IANA timezone not supported, fails closed (rule fires, denying the request)
        assert!(time_outside_matches(
            "09:00-17:00",
            Some("America/New_York"),
            1774708200000
        ));
    }

    #[test]
    fn time_fixed_offset() {
        // 14:30 UTC = 10:00 in UTC-04:30
        // 10:00 is within 09:00-17:00, so "outside" is false
        assert!(!time_outside_matches(
            "09:00-17:00",
            Some("-04:30"),
            1774708200000
        ));
    }

    // --- Full policy evaluation ---

    fn bm(jsonpath: &str, max: Option<i64>) -> BodyMatcher {
        BodyMatcher {
            jsonpath: jsonpath.into(),
            max,
            min: None,
            exact: None,
            in_values: None,
            prefix: None,
            suffix: None,
            contains: None,
            regex: None,
        }
    }

    fn matcher(method: Vec<HttpMethod>, url: Option<&str>) -> RequestMatcher {
        RequestMatcher {
            method,
            url: url.map(|s| s.to_string()),
            body: vec![],
            headers: vec![],
            time_outside: None,
            timezone: None,
        }
    }

    fn simple_policy() -> PolicyConfig {
        PolicyConfig {
            default: DefaultAction::Deny,
            mode: checkrd_shared::PolicyMode::default(),
            rules: vec![
                PolicyRule {
                    name: "allow-get-contacts".into(),
                    kind: PolicyRuleKind::Allow(matcher(
                        vec![HttpMethod::GET],
                        Some("api.salesforce.com/*/sobjects/Contact/*"),
                    )),
                    source: None,
                },
                PolicyRule {
                    name: "block-all-deletes".into(),
                    kind: PolicyRuleKind::Deny(matcher(vec![HttpMethod::DELETE], Some("*"))),
                    source: None,
                },
                PolicyRule {
                    name: "allow-small-charges".into(),
                    kind: PolicyRuleKind::Allow(RequestMatcher {
                        method: vec![HttpMethod::POST],
                        url: Some("api.stripe.com/v1/charges".into()),
                        body: vec![bm("$.amount", Some(50000))],
                        headers: vec![],
                        time_outside: None,
                        timezone: None,
                    }),
                    source: None,
                },
            ],
        }
    }

    #[test]
    fn eval_allow_matching_rule() {
        let engine = PolicyEngine::from_config(simple_policy()).unwrap();
        let mut rl = RateLimiter::new();
        let req = make_request(
            HttpMethod::GET,
            "https://api.salesforce.com/v58/sobjects/Contact/001",
        );
        let (allowed, reason) = eval_request(&engine, &mut rl, &req);
        assert!(allowed);
        assert!(reason.is_none());
    }

    #[test]
    fn eval_deny_matching_rule() {
        let engine = PolicyEngine::from_config(simple_policy()).unwrap();
        let mut rl = RateLimiter::new();
        let req = make_request(
            HttpMethod::DELETE,
            "https://api.salesforce.com/v58/sobjects/Contact/001",
        );
        let (allowed, reason) = eval_request(&engine, &mut rl, &req);
        assert!(!allowed);
        assert!(reason.unwrap().contains("block-all-deletes"));
    }

    #[test]
    fn eval_default_deny() {
        let engine = PolicyEngine::from_config(simple_policy()).unwrap();
        let mut rl = RateLimiter::new();
        let req = make_request(HttpMethod::GET, "https://unknown-api.com/something");
        let (allowed, reason) = eval_request(&engine, &mut rl, &req);
        assert!(!allowed);
        assert!(reason.unwrap().contains("default policy"));
    }

    #[test]
    fn eval_deny_overrides_allow() {
        // DELETE to contacts matches both the allow (GET contacts) and deny (all deletes)
        // But method filtering means the allow rule won't match DELETE.
        // And deny rules are checked before allow rules.
        let engine = PolicyEngine::from_config(simple_policy()).unwrap();
        let mut rl = RateLimiter::new();
        let req = make_request(
            HttpMethod::DELETE,
            "https://api.salesforce.com/v58/sobjects/Contact/001",
        );
        let (allowed, _) = eval_request(&engine, &mut rl, &req);
        assert!(!allowed);
    }

    #[test]
    fn eval_body_matcher_allows_small_charge() {
        let engine = PolicyEngine::from_config(simple_policy()).unwrap();
        let mut rl = RateLimiter::new();
        let mut req = make_request(HttpMethod::POST, "https://api.stripe.com/v1/charges");
        req.body = Some(r#"{"amount": 1000}"#.into());
        let (allowed, _) = eval_request(&engine, &mut rl, &req);
        assert!(allowed);
    }

    #[test]
    fn eval_body_matcher_denies_large_charge() {
        let engine = PolicyEngine::from_config(simple_policy()).unwrap();
        let mut rl = RateLimiter::new();
        let mut req = make_request(HttpMethod::POST, "https://api.stripe.com/v1/charges");
        req.body = Some(r#"{"amount": 100000}"#.into());
        let (allowed, reason) = eval_request(&engine, &mut rl, &req);
        assert!(!allowed);
        // Body doesn't match the allow rule, falls through to default deny
        assert!(reason.unwrap().contains("default policy"));
    }

    #[test]
    fn eval_default_allow_policy() {
        let config = PolicyConfig {
            default: DefaultAction::Allow,
            mode: checkrd_shared::PolicyMode::default(),
            rules: vec![],
        };
        let engine = PolicyEngine::from_config(config).unwrap();
        let mut rl = RateLimiter::new();
        let req = make_request(HttpMethod::GET, "https://anything.com/any/path");
        let (allowed, _) = eval_request(&engine, &mut rl, &req);
        assert!(allowed);
    }

    #[test]
    fn eval_rate_limit_exceeded() {
        let config = PolicyConfig {
            default: DefaultAction::Allow,
            mode: checkrd_shared::PolicyMode::default(),
            rules: vec![PolicyRule {
                name: "global-limit".into(),
                kind: PolicyRuleKind::Limit(RateLimitConfig {
                    calls_per_minute: 3,
                    per: checkrd_shared::RateLimitScope::Global,
                    field: None,
                }),
                source: None,
            }],
        };
        let engine = PolicyEngine::from_config(config).unwrap();
        let mut rl = RateLimiter::new();
        let req = make_request(HttpMethod::GET, "https://example.com/api");

        // First 3 calls succeed (default allow, within rate limit)
        for _ in 0..3 {
            let (allowed, _) = eval_request(&engine, &mut rl, &req);
            assert!(allowed);
        }

        // 4th call hits rate limit
        let (allowed, reason) = eval_request(&engine, &mut rl, &req);
        assert!(!allowed);
        assert!(reason.unwrap().contains("rate limit"));
    }

    #[test]
    fn eval_deny_with_time_restriction() {
        let config = PolicyConfig {
            default: DefaultAction::Allow,
            mode: checkrd_shared::PolicyMode::default(),
            rules: vec![PolicyRule {
                name: "business-hours-only".into(),
                kind: PolicyRuleKind::Deny(RequestMatcher {
                    method: vec![],
                    url: None,
                    body: vec![],
                    headers: vec![],
                    time_outside: Some("09:00-17:00".into()),
                    timezone: Some("UTC".into()),
                }),
                source: None,
            }],
        };
        let engine = PolicyEngine::from_config(config).unwrap();
        let mut rl = RateLimiter::new();

        // 14:30 UTC -- within business hours, deny rule does NOT fire, default allow
        let req = make_request(HttpMethod::GET, "https://example.com/api");
        let (allowed, _) = eval_request(&engine, &mut rl, &req);
        assert!(allowed, "expected allow during business hours");

        // 03:00 UTC -- outside business hours, deny rule fires
        let mut req = make_request(HttpMethod::GET, "https://example.com/api");
        req.timestamp_ms = 1774666800000; // 03:00 UTC
        let (allowed, reason) = eval_request(&engine, &mut rl, &req);
        assert!(!allowed, "expected deny outside business hours");
        assert!(reason.unwrap().contains("business-hours-only"));
    }

    #[test]
    fn eval_specificity_ordering() {
        // More specific URL pattern should be checked first
        let config = PolicyConfig {
            default: DefaultAction::Deny,
            mode: checkrd_shared::PolicyMode::default(),
            rules: vec![
                PolicyRule {
                    name: "allow-all-stripe".into(),
                    kind: PolicyRuleKind::Allow(RequestMatcher {
                        method: vec![],
                        url: Some("api.stripe.com/*".into()),
                        body: vec![],
                        headers: vec![],
                        time_outside: None,
                        timezone: None,
                    }),
                    source: None,
                },
                PolicyRule {
                    name: "allow-specific-charges".into(),
                    kind: PolicyRuleKind::Allow(RequestMatcher {
                        method: vec![HttpMethod::POST],
                        url: Some("api.stripe.com/v1/charges".into()),
                        body: vec![],
                        headers: vec![],
                        time_outside: None,
                        timezone: None,
                    }),
                    source: None,
                },
            ],
        };
        let engine = PolicyEngine::from_config(config).unwrap();

        // The specific rule (specificity 3) should be checked before the wildcard rule (specificity 1)
        // Both match, but the more specific one is first in the sorted list
        let mut rl = RateLimiter::new();
        let req = make_request(HttpMethod::POST, "https://api.stripe.com/v1/charges");
        let (allowed, _) = eval_request(&engine, &mut rl, &req);
        assert!(allowed);
    }

    // --- Body inspection: fail-closed behavior ---

    #[test]
    fn body_unparseable_fail_closed_true() {
        let matcher = bm("$.amount", Some(50000));
        // Empty string = body present but not decodable
        assert!(body_matches(&matcher, &Some("".into()), true, None));
        // Invalid JSON with fail_closed=true
        assert!(body_matches(&matcher, &Some("not json".into()), true, None));
    }

    #[test]
    fn body_unparseable_fail_closed_false() {
        let matcher = bm("$.amount", Some(50000));
        assert!(!body_matches(&matcher, &Some("".into()), false, None));
        assert!(!body_matches(
            &matcher,
            &Some("not json".into()),
            false,
            None
        ));
    }

    #[test]
    fn body_none_ignores_fail_closed() {
        let matcher = bm("$.amount", None);
        // None always returns false regardless of fail_closed
        assert!(!body_matches(&matcher, &None, true, None));
        assert!(!body_matches(&matcher, &None, false, None));
    }

    #[test]
    fn eval_deny_rule_fires_on_unparseable_body() {
        let config = PolicyConfig {
            default: DefaultAction::Allow,
            mode: checkrd_shared::PolicyMode::default(),
            rules: vec![PolicyRule {
                name: "block-large-charges".into(),
                kind: PolicyRuleKind::Deny(RequestMatcher {
                    method: vec![HttpMethod::POST],
                    url: Some("api.stripe.com/v1/charges".into()),
                    body: vec![bm("$.amount", Some(50000))],
                    headers: vec![],
                    time_outside: None,
                    timezone: None,
                }),
                source: None,
            }],
        };
        let engine = PolicyEngine::from_config(config).unwrap();
        let mut rl = RateLimiter::new();
        let mut req = make_request(HttpMethod::POST, "https://api.stripe.com/v1/charges");
        req.body = Some("".into()); // unparseable body
        let (allowed, reason) = eval_request(&engine, &mut rl, &req);
        assert!(!allowed, "deny rule must fire on unparseable body");
        assert!(reason.unwrap().contains("block-large-charges"));
    }

    #[test]
    fn eval_allow_rule_skips_on_unparseable_body() {
        let config = PolicyConfig {
            default: DefaultAction::Deny,
            mode: checkrd_shared::PolicyMode::default(),
            rules: vec![PolicyRule {
                name: "allow-small-charges".into(),
                kind: PolicyRuleKind::Allow(RequestMatcher {
                    method: vec![HttpMethod::POST],
                    url: Some("api.stripe.com/v1/charges".into()),
                    body: vec![bm("$.amount", Some(50000))],
                    headers: vec![],
                    time_outside: None,
                    timezone: None,
                }),
                source: None,
            }],
        };
        let engine = PolicyEngine::from_config(config).unwrap();
        let mut rl = RateLimiter::new();
        let mut req = make_request(HttpMethod::POST, "https://api.stripe.com/v1/charges");
        req.body = Some("".into()); // unparseable body
        let (allowed, reason) = eval_request(&engine, &mut rl, &req);
        assert!(
            !allowed,
            "allow rule must NOT fire on unparseable body; falls to default deny"
        );
        assert!(reason.unwrap().contains("default policy"));
    }

    // --- Rate limiting ---

    #[test]
    fn eval_rate_limit_endpoint_ignores_query_params() {
        let config = PolicyConfig {
            default: DefaultAction::Allow,
            mode: checkrd_shared::PolicyMode::default(),
            rules: vec![PolicyRule {
                name: "endpoint-limit".into(),
                kind: PolicyRuleKind::Limit(RateLimitConfig {
                    calls_per_minute: 2,
                    per: checkrd_shared::RateLimitScope::Endpoint,
                    field: None,
                }),
                source: None,
            }],
        };
        let engine = PolicyEngine::from_config(config).unwrap();
        let mut rl = RateLimiter::new();

        // Use up the limit with one query string
        let req1 = make_request(HttpMethod::GET, "https://api.example.com/v1/data?page=1");
        let (allowed, _) = eval_request(&engine, &mut rl, &req1);
        assert!(allowed, "first call should be allowed");

        let (allowed, _) = eval_request(&engine, &mut rl, &req1);
        assert!(allowed, "second call should be allowed");

        // Third call with a DIFFERENT query string must still be rate-limited
        let req2 = make_request(HttpMethod::GET, "https://api.example.com/v1/data?page=2");
        let (allowed, reason) = eval_request(&engine, &mut rl, &req2);
        assert!(
            !allowed,
            "third call must be rate-limited despite different query params"
        );
        assert!(reason.unwrap().contains("rate limit"));
    }

    // ================================================================
    // New operator tests
    // ================================================================

    // --- Body matcher: exact ---

    #[test]
    fn body_exact_string_match() {
        let m = BodyMatcher {
            exact: Some(serde_json::json!("gpt-4o")),
            ..bm("$.model", None)
        };
        let body = Some(r#"{"model": "gpt-4o"}"#.into());
        assert!(body_matches(&m, &body, false, None));
    }

    #[test]
    fn body_exact_string_mismatch() {
        let m = BodyMatcher {
            exact: Some(serde_json::json!("gpt-4o")),
            ..bm("$.model", None)
        };
        let body = Some(r#"{"model": "claude-3"}"#.into());
        assert!(!body_matches(&m, &body, false, None));
    }

    #[test]
    fn body_exact_number_match() {
        let m = BodyMatcher {
            exact: Some(serde_json::json!(42)),
            ..bm("$.count", None)
        };
        let body = Some(r#"{"count": 42}"#.into());
        assert!(body_matches(&m, &body, false, None));
    }

    #[test]
    fn body_exact_type_mismatch() {
        // String "42" != number 42 — no implicit coercion (industry standard)
        let m = BodyMatcher {
            exact: Some(serde_json::json!(42)),
            ..bm("$.count", None)
        };
        let body = Some(r#"{"count": "42"}"#.into());
        assert!(!body_matches(&m, &body, false, None));
    }

    // --- Body matcher: in (set membership) ---

    #[test]
    fn body_in_set_match() {
        let m = BodyMatcher {
            in_values: Some(vec![
                serde_json::json!("gpt-4o"),
                serde_json::json!("claude-3-5-sonnet"),
            ]),
            ..bm("$.model", None)
        };
        let body = Some(r#"{"model": "gpt-4o"}"#.into());
        assert!(body_matches(&m, &body, false, None));
    }

    #[test]
    fn body_in_set_no_match() {
        let m = BodyMatcher {
            in_values: Some(vec![
                serde_json::json!("gpt-4o"),
                serde_json::json!("claude-3-5-sonnet"),
            ]),
            ..bm("$.model", None)
        };
        let body = Some(r#"{"model": "llama-3"}"#.into());
        assert!(!body_matches(&m, &body, false, None));
    }

    // --- Body matcher: min ---

    #[test]
    fn body_min_passes() {
        let m = BodyMatcher {
            min: Some(10),
            ..bm("$.count", None)
        };
        let body = Some(r#"{"count": 15}"#.into());
        assert!(body_matches(&m, &body, false, None));
    }

    #[test]
    fn body_min_fails() {
        let m = BodyMatcher {
            min: Some(10),
            ..bm("$.count", None)
        };
        let body = Some(r#"{"count": 5}"#.into());
        assert!(!body_matches(&m, &body, false, None));
    }

    #[test]
    fn body_min_max_range() {
        let m = BodyMatcher {
            min: Some(10),
            max: Some(100),
            ..bm("$.count", None)
        };
        let body_ok = Some(r#"{"count": 50}"#.into());
        assert!(body_matches(&m, &body_ok, false, None));
        let body_low = Some(r#"{"count": 5}"#.into());
        assert!(!body_matches(&m, &body_low, false, None));
        let body_high = Some(r#"{"count": 200}"#.into());
        assert!(!body_matches(&m, &body_high, false, None));
    }

    // --- Body matcher: string operators ---

    #[test]
    fn body_prefix_match() {
        let m = BodyMatcher {
            prefix: Some("gpt-".into()),
            ..bm("$.model", None)
        };
        let body = Some(r#"{"model": "gpt-4o"}"#.into());
        assert!(body_matches(&m, &body, false, None));
    }

    #[test]
    fn body_suffix_match() {
        let m = BodyMatcher {
            suffix: Some("-turbo".into()),
            ..bm("$.model", None)
        };
        let body = Some(r#"{"model": "gpt-4-turbo"}"#.into());
        assert!(body_matches(&m, &body, false, None));
    }

    #[test]
    fn body_contains_match() {
        let m = BodyMatcher {
            contains: Some("stripe".into()),
            ..bm("$.url", None)
        };
        let body = Some(r#"{"url": "https://api.stripe.com/v1"}"#.into());
        assert!(body_matches(&m, &body, false, None));
    }

    #[test]
    fn body_string_op_on_number_fails() {
        // String operators require JSON string fields — number returns false
        let m = BodyMatcher {
            prefix: Some("10".into()),
            ..bm("$.count", None)
        };
        let body = Some(r#"{"count": 100}"#.into());
        assert!(!body_matches(&m, &body, false, None));
    }

    // --- Body matcher: regex ---

    #[test]
    fn body_regex_match() {
        let m = BodyMatcher {
            regex: Some("^gpt-4".into()),
            ..bm("$.model", None)
        };
        let re = regex::Regex::new("^gpt-4").unwrap();
        let body = Some(r#"{"model": "gpt-4o"}"#.into());
        assert!(body_matches(&m, &body, false, Some(&re)));
    }

    #[test]
    fn body_regex_no_match() {
        let m = BodyMatcher {
            regex: Some("^claude".into()),
            ..bm("$.model", None)
        };
        let re = regex::Regex::new("^claude").unwrap();
        let body = Some(r#"{"model": "gpt-4o"}"#.into());
        assert!(!body_matches(&m, &body, false, Some(&re)));
    }

    // --- Body matcher: AND semantics ---

    #[test]
    fn body_and_semantics_all_pass() {
        let m = BodyMatcher {
            exact: Some(serde_json::json!("gpt-4o")),
            prefix: Some("gpt".into()),
            ..bm("$.model", None)
        };
        let body = Some(r#"{"model": "gpt-4o"}"#.into());
        assert!(body_matches(&m, &body, false, None));
    }

    #[test]
    fn body_and_semantics_one_fails() {
        let m = BodyMatcher {
            exact: Some(serde_json::json!("gpt-4o")),
            prefix: Some("claude".into()), // This fails
            ..bm("$.model", None)
        };
        let body = Some(r#"{"model": "gpt-4o"}"#.into());
        assert!(!body_matches(&m, &body, false, None));
    }

    // --- Multiple body matchers (AND across matchers) ---

    #[test]
    fn multiple_body_matchers_all_pass() {
        let matchers = vec![
            BodyMatcher {
                max: Some(50000),
                ..bm("$.amount", None)
            },
            BodyMatcher {
                exact: Some(serde_json::json!("gpt-4o")),
                ..bm("$.model", None)
            },
        ];
        let body = Some(r#"{"amount": 1000, "model": "gpt-4o"}"#.into());
        assert!(body_matchers_match(&matchers, &body, false, &[None, None]));
    }

    #[test]
    fn multiple_body_matchers_one_fails() {
        let matchers = vec![
            BodyMatcher {
                max: Some(50000),
                ..bm("$.amount", None)
            },
            BodyMatcher {
                exact: Some(serde_json::json!("gpt-4o")),
                ..bm("$.model", None)
            },
        ];
        let body = Some(r#"{"amount": 1000, "model": "claude-3"}"#.into()); // model mismatch
        assert!(!body_matchers_match(&matchers, &body, false, &[None, None]));
    }

    // --- URL ** glob matching ---

    #[test]
    fn url_double_wildcard_matches_all() {
        let segments = parse_pattern("**");
        assert!(url_matches(&segments, "anything/any/path"));
    }

    #[test]
    fn url_double_wildcard_matches_deep_path() {
        let segments = parse_pattern("api.stripe.com/**");
        assert!(url_matches(
            &segments,
            "api.stripe.com/v1/charges/ch_123/refunds"
        ));
        assert!(url_matches(&segments, "api.stripe.com/v1"));
        assert!(url_matches(&segments, "api.stripe.com"));
    }

    #[test]
    fn url_double_wildcard_does_not_match_wrong_host() {
        let segments = parse_pattern("api.stripe.com/**");
        assert!(!url_matches(&segments, "api.openai.com/v1/completions"));
    }

    #[test]
    fn url_double_wildcard_in_middle() {
        let segments = parse_pattern("api.stripe.com/**/refunds");
        assert!(url_matches(
            &segments,
            "api.stripe.com/v1/charges/ch_123/refunds"
        ));
        assert!(url_matches(&segments, "api.stripe.com/refunds")); // ** matches zero segments
        assert!(!url_matches(&segments, "api.stripe.com/v1/charges"));
    }

    #[test]
    fn url_mixed_wildcards() {
        let segments = parse_pattern("api.stripe.com/*/charges/**/refunds");
        assert!(url_matches(
            &segments,
            "api.stripe.com/v1/charges/ch_123/refunds"
        ));
        assert!(url_matches(
            &segments,
            "api.stripe.com/v2/charges/a/b/c/refunds"
        ));
        assert!(!url_matches(&segments, "api.stripe.com/v1/charges/ch_123"));
    }

    // --- Header matching ---

    #[test]
    fn header_exact_match() {
        let matchers = vec![HeaderMatcher {
            name: "Content-Type".into(),
            exact: Some("application/json".into()),
            prefix: None,
            suffix: None,
            contains: None,
            regex: None,
            present: None,
        }];
        let headers = vec![("content-type".into(), "application/json".into())];
        assert!(headers_match(&matchers, &headers, &[None]));
    }

    #[test]
    fn header_case_insensitive_name() {
        let matchers = vec![HeaderMatcher {
            name: "X-Custom-Header".into(),
            exact: Some("value".into()),
            prefix: None,
            suffix: None,
            contains: None,
            regex: None,
            present: None,
        }];
        let headers = vec![("x-custom-header".into(), "value".into())];
        assert!(headers_match(&matchers, &headers, &[None]));
    }

    #[test]
    fn header_contains_match() {
        let matchers = vec![HeaderMatcher {
            name: "user-agent".into(),
            contains: Some("bot".into()),
            exact: None,
            prefix: None,
            suffix: None,
            regex: None,
            present: None,
        }];
        let headers = vec![("User-Agent".into(), "my-cool-bot/1.0".into())];
        assert!(headers_match(&matchers, &headers, &[None]));
    }

    #[test]
    fn header_present_true() {
        let matchers = vec![HeaderMatcher {
            name: "x-debug".into(),
            present: Some(true),
            exact: None,
            prefix: None,
            suffix: None,
            contains: None,
            regex: None,
        }];
        let headers = vec![("x-debug".into(), "1".into())];
        assert!(headers_match(&matchers, &headers, &[None]));
    }

    #[test]
    fn header_present_false() {
        let matchers = vec![HeaderMatcher {
            name: "x-debug".into(),
            present: Some(false),
            exact: None,
            prefix: None,
            suffix: None,
            contains: None,
            regex: None,
        }];
        let headers: Vec<(String, String)> = vec![];
        assert!(headers_match(&matchers, &headers, &[None]));
    }

    #[test]
    fn header_absent_fails_default() {
        let matchers = vec![HeaderMatcher {
            name: "x-required".into(),
            exact: Some("expected".into()),
            prefix: None,
            suffix: None,
            contains: None,
            regex: None,
            present: None,
        }];
        let headers: Vec<(String, String)> = vec![];
        assert!(!headers_match(&matchers, &headers, &[None]));
    }

    #[test]
    fn header_regex_match() {
        let matchers = vec![HeaderMatcher {
            name: "authorization".into(),
            regex: Some("^Bearer .+$".into()),
            exact: None,
            prefix: None,
            suffix: None,
            contains: None,
            present: None,
        }];
        let re = regex::Regex::new("^Bearer .+$").unwrap();
        let headers = vec![("Authorization".into(), "Bearer abc123".into())];
        assert!(headers_match(&matchers, &headers, &[Some(re)]));
    }

    #[test]
    fn header_multiple_matchers_and() {
        let matchers = vec![
            HeaderMatcher {
                name: "content-type".into(),
                contains: Some("json".into()),
                exact: None,
                prefix: None,
                suffix: None,
                regex: None,
                present: None,
            },
            HeaderMatcher {
                name: "x-api-version".into(),
                exact: Some("2024-01-01".into()),
                prefix: None,
                suffix: None,
                contains: None,
                regex: None,
                present: None,
            },
        ];
        let headers = vec![
            ("Content-Type".into(), "application/json".into()),
            ("X-Api-Version".into(), "2024-01-01".into()),
        ];
        assert!(headers_match(&matchers, &headers, &[None, None]));
    }

    // --- Body-field rate limiting ---

    #[test]
    fn eval_body_field_rate_limit() {
        let config = PolicyConfig {
            default: DefaultAction::Allow,
            mode: checkrd_shared::PolicyMode::default(),
            rules: vec![PolicyRule {
                name: "per-model-limit".into(),
                kind: PolicyRuleKind::Limit(RateLimitConfig {
                    calls_per_minute: 2,
                    per: checkrd_shared::RateLimitScope::BodyField,
                    field: Some("$.model".into()),
                }),
                source: None,
            }],
        };
        let engine = PolicyEngine::from_config(config).unwrap();
        let mut rl = RateLimiter::new();

        // Two calls with gpt-4o — both allowed
        let mut req = make_request(HttpMethod::POST, "https://api.openai.com/v1/completions");
        req.body = Some(r#"{"model": "gpt-4o"}"#.into());
        assert!(eval_request(&engine, &mut rl, &req).0);
        assert!(eval_request(&engine, &mut rl, &req).0);

        // Third gpt-4o call — rate limited
        assert!(!eval_request(&engine, &mut rl, &req).0);

        // But claude-3 still has budget (separate key)
        req.body = Some(r#"{"model": "claude-3"}"#.into());
        assert!(eval_request(&engine, &mut rl, &req).0);
    }

    // --- Array index in JSONPath ---

    #[test]
    fn body_array_index_access() {
        let m = BodyMatcher {
            exact: Some(serde_json::json!("system")),
            ..bm("$.messages.0.role", None)
        };
        let body = Some(r#"{"messages": [{"role": "system", "content": "hello"}]}"#.into());
        assert!(body_matches(&m, &body, false, None));
    }

    // --- Full integration: AI model restriction policy ---

    #[test]
    fn eval_ai_model_restriction_policy() {
        let config = PolicyConfig {
            default: DefaultAction::Deny,
            mode: checkrd_shared::PolicyMode::default(),
            rules: vec![
                PolicyRule {
                    name: "allow-approved-models".into(),
                    kind: PolicyRuleKind::Allow(RequestMatcher {
                        method: vec![HttpMethod::POST],
                        url: Some("api.openai.com/v1/chat/completions".into()),
                        body: vec![BodyMatcher {
                            in_values: Some(vec![
                                serde_json::json!("gpt-4o"),
                                serde_json::json!("gpt-4o-mini"),
                            ]),
                            ..bm("$.model", None)
                        }],
                        headers: vec![],
                        time_outside: None,
                        timezone: None,
                    }),
                    source: None,
                },
                PolicyRule {
                    name: "per-model-rate-limit".into(),
                    kind: PolicyRuleKind::Limit(RateLimitConfig {
                        calls_per_minute: 100,
                        per: checkrd_shared::RateLimitScope::BodyField,
                        field: Some("$.model".into()),
                    }),
                    source: None,
                },
            ],
        };
        let engine = PolicyEngine::from_config(config).unwrap();
        let mut rl = RateLimiter::new();

        // gpt-4o: allowed (in approved set)
        let mut req = make_request(
            HttpMethod::POST,
            "https://api.openai.com/v1/chat/completions",
        );
        req.body = Some(r#"{"model": "gpt-4o", "messages": []}"#.into());
        let (allowed, _) = eval_request(&engine, &mut rl, &req);
        assert!(allowed, "gpt-4o should be allowed");

        // llama-3: denied (not in approved set, falls to default deny)
        req.body = Some(r#"{"model": "llama-3", "messages": []}"#.into());
        let (allowed, reason) = eval_request(&engine, &mut rl, &req);
        assert!(!allowed, "llama-3 should be denied");
        assert!(reason.unwrap().contains("default policy"));
    }

    // ================================================================
    // Edge case tests (from audit)
    // ================================================================

    #[test]
    fn body_in_empty_set_always_fails() {
        let m = BodyMatcher {
            in_values: Some(vec![]),
            ..bm("$.model", None)
        };
        let body = Some(r#"{"model": "gpt-4o"}"#.into());
        assert!(!body_matches(&m, &body, false, None));
    }

    #[test]
    fn body_min_equals_max_accepts_exact() {
        let m = BodyMatcher {
            min: Some(42),
            max: Some(42),
            ..bm("$.count", None)
        };
        let body = Some(r#"{"count": 42}"#.into());
        assert!(body_matches(&m, &body, false, None));
        let body_off = Some(r#"{"count": 43}"#.into());
        assert!(!body_matches(&m, &body_off, false, None));
    }

    #[test]
    fn body_negative_number_range() {
        let m = BodyMatcher {
            min: Some(-100),
            max: Some(-10),
            ..bm("$.temperature", None)
        };
        let body = Some(r#"{"temperature": -50}"#.into());
        assert!(body_matches(&m, &body, false, None));
        let body_too_low = Some(r#"{"temperature": -200}"#.into());
        assert!(!body_matches(&m, &body_too_low, false, None));
    }

    #[test]
    fn body_exact_empty_string() {
        let m = BodyMatcher {
            exact: Some(serde_json::json!("")),
            ..bm("$.name", None)
        };
        let body = Some(r#"{"name": ""}"#.into());
        assert!(body_matches(&m, &body, false, None));
        let body_nonempty = Some(r#"{"name": "alice"}"#.into());
        assert!(!body_matches(&m, &body_nonempty, false, None));
    }

    #[test]
    fn body_exact_null() {
        let m = BodyMatcher {
            exact: Some(serde_json::json!(null)),
            ..bm("$.field", None)
        };
        let body = Some(r#"{"field": null}"#.into());
        assert!(body_matches(&m, &body, false, None));
    }

    #[test]
    fn body_exact_boolean() {
        let m = BodyMatcher {
            exact: Some(serde_json::json!(true)),
            ..bm("$.active", None)
        };
        let body = Some(r#"{"active": true}"#.into());
        assert!(body_matches(&m, &body, false, None));
        let body_false = Some(r#"{"active": false}"#.into());
        assert!(!body_matches(&m, &body_false, false, None));
    }

    #[test]
    fn body_in_with_mixed_types() {
        let m = BodyMatcher {
            in_values: Some(vec![
                serde_json::json!("a"),
                serde_json::json!(1),
                serde_json::json!(true),
            ]),
            ..bm("$.val", None)
        };
        let body_str = Some(r#"{"val": "a"}"#.into());
        assert!(body_matches(&m, &body_str, false, None));
        let body_num = Some(r#"{"val": 1}"#.into());
        assert!(body_matches(&m, &body_num, false, None));
        let body_bool = Some(r#"{"val": true}"#.into());
        assert!(body_matches(&m, &body_bool, false, None));
        let body_miss = Some(r#"{"val": "b"}"#.into());
        assert!(!body_matches(&m, &body_miss, false, None));
    }

    #[test]
    fn header_multi_value_combined() {
        // RFC 9110: multiple headers with same name are combined with ", "
        let matchers = vec![HeaderMatcher {
            name: "accept".into(),
            contains: Some("json".into()),
            exact: None,
            prefix: None,
            suffix: None,
            regex: None,
            present: None,
        }];
        let headers = vec![
            ("Accept".into(), "text/html".into()),
            ("Accept".into(), "application/json".into()),
        ];
        assert!(headers_match(&matchers, &headers, &[None]));
    }

    #[test]
    fn header_empty_value() {
        let matchers = vec![HeaderMatcher {
            name: "x-empty".into(),
            exact: Some("".into()),
            prefix: None,
            suffix: None,
            contains: None,
            regex: None,
            present: None,
        }];
        let headers = vec![("x-empty".into(), "".into())];
        assert!(headers_match(&matchers, &headers, &[None]));
    }

    #[test]
    fn body_field_rate_limit_missing_field_shares_bucket() {
        let config = PolicyConfig {
            default: DefaultAction::Allow,
            mode: checkrd_shared::PolicyMode::default(),
            rules: vec![PolicyRule {
                name: "per-model-limit".into(),
                kind: PolicyRuleKind::Limit(RateLimitConfig {
                    calls_per_minute: 1,
                    per: checkrd_shared::RateLimitScope::BodyField,
                    field: Some("$.model".into()),
                }),
                source: None,
            }],
        };
        let engine = PolicyEngine::from_config(config).unwrap();
        let mut rl = RateLimiter::new();

        // Request with no body — falls to __unknown__ bucket
        let req = make_request(HttpMethod::POST, "https://api.openai.com/v1/completions");
        assert!(eval_request(&engine, &mut rl, &req).0);
        // Second no-body request — hits the shared __unknown__ limit
        assert!(!eval_request(&engine, &mut rl, &req).0);
    }

    #[test]
    fn url_double_wildcard_pathological_pattern() {
        // Ensure multiple ** don't cause excessive recursion on reasonable URLs
        let segments = parse_pattern("host/**/**/end");
        assert!(url_matches(&segments, "host/a/b/c/d/end"));
        assert!(!url_matches(&segments, "host/a/b/c/d/nope"));
    }

    // ================================================================
    // Phase 2: Structured decision audit + dry-run mode
    // ================================================================

    fn eval_full(
        engine: &PolicyEngine,
        rl: &mut RateLimiter,
        req: &EvaluationRequest,
    ) -> EvaluationOutcome {
        let parsed = util::parse_url(&req.url);
        engine.evaluate_full(req, rl, &parsed)
    }

    #[test]
    fn audit_allow_rule_reports_matched_rule() {
        let engine = PolicyEngine::from_config(simple_policy()).unwrap();
        let mut rl = RateLimiter::new();
        let req = make_request(
            HttpMethod::GET,
            "https://api.salesforce.com/v58/sobjects/Contact/001",
        );
        let outcome = eval_full(&engine, &mut rl, &req);
        assert!(outcome.allowed);
        assert_eq!(outcome.matched_rule.as_deref(), Some("allow-get-contacts"));
        assert_eq!(outcome.matched_rule_kind.as_deref(), Some("allow"));
        assert_eq!(outcome.mode, PolicyMode::Enforce);
    }

    #[test]
    fn audit_deny_rule_reports_matched_rule() {
        let engine = PolicyEngine::from_config(simple_policy()).unwrap();
        let mut rl = RateLimiter::new();
        let req = make_request(
            HttpMethod::DELETE,
            "https://api.salesforce.com/v58/sobjects/Contact/001",
        );
        let outcome = eval_full(&engine, &mut rl, &req);
        assert!(!outcome.allowed);
        assert_eq!(outcome.matched_rule.as_deref(), Some("block-all-deletes"));
        assert_eq!(outcome.matched_rule_kind.as_deref(), Some("deny"));
    }

    #[test]
    fn audit_default_deny_reports_no_matched_rule() {
        let engine = PolicyEngine::from_config(simple_policy()).unwrap();
        let mut rl = RateLimiter::new();
        let req = make_request(HttpMethod::GET, "https://unknown.com/path");
        let outcome = eval_full(&engine, &mut rl, &req);
        assert!(!outcome.allowed);
        assert!(outcome.matched_rule.is_none());
        assert_eq!(outcome.matched_rule_kind.as_deref(), Some("default"));
    }

    #[test]
    fn audit_rate_limit_reports_rule_name() {
        let config = PolicyConfig {
            default: DefaultAction::Allow,
            mode: checkrd_shared::PolicyMode::default(),
            rules: vec![PolicyRule {
                name: "global-limit".into(),
                kind: PolicyRuleKind::Limit(RateLimitConfig {
                    calls_per_minute: 1,
                    per: checkrd_shared::RateLimitScope::Global,
                    field: None,
                }),
                source: None,
            }],
        };
        let engine = PolicyEngine::from_config(config).unwrap();
        let mut rl = RateLimiter::new();
        let req = make_request(HttpMethod::GET, "https://example.com/api");
        // First call passes
        let _ = eval_full(&engine, &mut rl, &req);
        // Second call hits rate limit
        let outcome = eval_full(&engine, &mut rl, &req);
        assert!(!outcome.allowed);
        assert_eq!(outcome.matched_rule.as_deref(), Some("global-limit"));
        assert_eq!(outcome.matched_rule_kind.as_deref(), Some("rate_limit"));
    }

    #[test]
    fn audit_evaluation_path_records_all_stages() {
        let engine = PolicyEngine::from_config(simple_policy()).unwrap();
        let mut rl = RateLimiter::new();
        let req = make_request(
            HttpMethod::GET,
            "https://api.salesforce.com/v58/sobjects/Contact/001",
        );
        let outcome = eval_full(&engine, &mut rl, &req);
        // Path should record: deny rules (no_match) then allow rules (matched)
        assert!(!outcome.evaluation_path.is_empty());
        let deny_steps: Vec<_> = outcome
            .evaluation_path
            .iter()
            .filter(|s| s.stage == "deny_rules")
            .collect();
        assert_eq!(deny_steps.len(), 1); // one deny rule checked
        assert_eq!(deny_steps[0].result, "no_match");
        let allow_steps: Vec<_> = outcome
            .evaluation_path
            .iter()
            .filter(|s| s.stage == "allow_rules" && s.result == "matched")
            .collect();
        assert_eq!(allow_steps.len(), 1);
        assert_eq!(allow_steps[0].rule.as_deref(), Some("allow-get-contacts"));
    }

    // --- Dry-run mode ---

    #[test]
    fn dry_run_deny_still_allows() {
        let config = PolicyConfig {
            default: DefaultAction::Deny,
            mode: PolicyMode::DryRun,
            rules: vec![PolicyRule {
                name: "block-deletes".into(),
                kind: PolicyRuleKind::Deny(matcher(vec![HttpMethod::DELETE], Some("*"))),
                source: None,
            }],
        };
        let engine = PolicyEngine::from_config(config).unwrap();
        let mut rl = RateLimiter::new();
        let req = make_request(HttpMethod::DELETE, "https://example.com/api");
        let outcome = eval_full(&engine, &mut rl, &req);
        // In dry-run, the decision is logged as deny but allowed=true
        assert!(outcome.allowed, "dry_run must always allow");
        assert_eq!(outcome.matched_rule.as_deref(), Some("block-deletes"));
        assert_eq!(outcome.matched_rule_kind.as_deref(), Some("deny"));
        assert_eq!(outcome.mode, PolicyMode::DryRun);
        assert!(outcome.deny_reason.is_some()); // reason still recorded
    }

    #[test]
    fn dry_run_default_deny_still_allows() {
        let config = PolicyConfig {
            default: DefaultAction::Deny,
            mode: PolicyMode::DryRun,
            rules: vec![],
        };
        let engine = PolicyEngine::from_config(config).unwrap();
        let mut rl = RateLimiter::new();
        let req = make_request(HttpMethod::GET, "https://example.com/api");
        let outcome = eval_full(&engine, &mut rl, &req);
        assert!(outcome.allowed, "dry_run default deny must still allow");
        assert!(outcome.deny_reason.is_some());
        assert_eq!(outcome.mode, PolicyMode::DryRun);
    }

    #[test]
    fn dry_run_skips_rate_limit_counters() {
        let config = PolicyConfig {
            default: DefaultAction::Allow,
            mode: PolicyMode::DryRun,
            rules: vec![PolicyRule {
                name: "limit".into(),
                kind: PolicyRuleKind::Limit(RateLimitConfig {
                    calls_per_minute: 1,
                    per: checkrd_shared::RateLimitScope::Global,
                    field: None,
                }),
                source: None,
            }],
        };
        let engine = PolicyEngine::from_config(config).unwrap();
        let mut rl = RateLimiter::new();
        let req = make_request(HttpMethod::GET, "https://example.com/api");
        // In dry-run, rate limits are skipped — counters not incremented
        for _ in 0..5 {
            let outcome = eval_full(&engine, &mut rl, &req);
            assert!(outcome.allowed, "dry_run must never rate-limit");
        }
        // Verify the rate limit step was logged as "skipped"
        let outcome = eval_full(&engine, &mut rl, &req);
        let rl_steps: Vec<_> = outcome
            .evaluation_path
            .iter()
            .filter(|s| s.stage == "rate_limit")
            .collect();
        assert_eq!(rl_steps.len(), 1);
        assert_eq!(rl_steps[0].result, "skipped");
    }

    #[test]
    fn dry_run_allow_rules_still_work() {
        let config = PolicyConfig {
            default: DefaultAction::Deny,
            mode: PolicyMode::DryRun,
            rules: vec![PolicyRule {
                name: "allow-all".into(),
                kind: PolicyRuleKind::Allow(matcher(vec![], Some("*"))),
                source: None,
            }],
        };
        let engine = PolicyEngine::from_config(config).unwrap();
        let mut rl = RateLimiter::new();
        let req = make_request(HttpMethod::GET, "https://example.com/api");
        let outcome = eval_full(&engine, &mut rl, &req);
        assert!(outcome.allowed);
        assert_eq!(outcome.matched_rule.as_deref(), Some("allow-all"));
        assert_eq!(outcome.matched_rule_kind.as_deref(), Some("allow"));
        assert_eq!(outcome.mode, PolicyMode::DryRun);
    }

    #[test]
    fn enforce_mode_is_default() {
        let config = PolicyConfig {
            default: DefaultAction::Allow,
            mode: checkrd_shared::PolicyMode::default(),
            rules: vec![],
        };
        let engine = PolicyEngine::from_config(config).unwrap();
        assert_eq!(engine.mode(), PolicyMode::Enforce);
    }

    #[test]
    fn dry_run_mode_deserializes_from_json() {
        let json = r#"{"agent": "test", "default": "deny", "mode": "dry_run", "rules": []}"#;
        let config: PolicyConfig = serde_json::from_str(json).unwrap();
        assert_eq!(config.mode, PolicyMode::DryRun);
    }

    #[test]
    fn mode_omitted_defaults_to_enforce() {
        let json = r#"{"agent": "test", "default": "deny", "rules": []}"#;
        let config: PolicyConfig = serde_json::from_str(json).unwrap();
        assert_eq!(config.mode, PolicyMode::Enforce);
    }

    // ================================================================
    // Policy test runner
    // ================================================================

    use checkrd_shared::{PolicyTestCase, PolicyTestExpectation, PolicyTestInput};

    #[test]
    fn test_runner_passing_tests() {
        let config = simple_policy();
        let tests = vec![
            PolicyTestCase {
                name: "allow GET contacts".into(),
                input: PolicyTestInput {
                    method: HttpMethod::GET,
                    url: "https://api.salesforce.com/v58/sobjects/Contact/001".into(),
                    headers: vec![],
                    body: None,
                    timestamp_ms: 1774708200000,
                },
                expect: PolicyTestExpectation {
                    allowed: true,
                    matched_rule: Some("allow-get-contacts".into()),
                },
            },
            PolicyTestCase {
                name: "deny DELETE".into(),
                input: PolicyTestInput {
                    method: HttpMethod::DELETE,
                    url: "https://api.salesforce.com/v58/sobjects/Contact/001".into(),
                    headers: vec![],
                    body: None,
                    timestamp_ms: 1774708200000,
                },
                expect: PolicyTestExpectation {
                    allowed: false,
                    matched_rule: Some("block-all-deletes".into()),
                },
            },
        ];

        let summary = run_policy_tests(&config, &tests).unwrap();
        assert_eq!(summary.total, 2);
        assert_eq!(summary.passed, 2);
        assert_eq!(summary.failed, 0);
    }

    #[test]
    fn test_runner_failing_test() {
        let config = simple_policy();
        let tests = vec![PolicyTestCase {
            name: "wrong expectation".into(),
            input: PolicyTestInput {
                method: HttpMethod::GET,
                url: "https://unknown.com/path".into(),
                headers: vec![],
                body: None,
                timestamp_ms: 1774708200000,
            },
            expect: PolicyTestExpectation {
                allowed: true, // WRONG — default is deny
                matched_rule: None,
            },
        }];

        let summary = run_policy_tests(&config, &tests).unwrap();
        assert_eq!(summary.total, 1);
        assert_eq!(summary.passed, 0);
        assert_eq!(summary.failed, 1);
        assert!(!summary.results[0].passed);
        assert!(!summary.results[0].actual_allowed);
    }

    #[test]
    fn test_runner_wrong_rule_name_fails() {
        let config = simple_policy();
        let tests = vec![PolicyTestCase {
            name: "wrong rule name".into(),
            input: PolicyTestInput {
                method: HttpMethod::DELETE,
                url: "https://api.salesforce.com/v58/sobjects/Contact/001".into(),
                headers: vec![],
                body: None,
                timestamp_ms: 1774708200000,
            },
            expect: PolicyTestExpectation {
                allowed: false,
                matched_rule: Some("wrong-name".into()), // wrong
            },
        }];

        let summary = run_policy_tests(&config, &tests).unwrap();
        assert_eq!(summary.failed, 1);
        assert_eq!(
            summary.results[0].actual_rule.as_deref(),
            Some("block-all-deletes")
        );
    }

    // ================================================================
    // Policy diff
    // ================================================================

    #[test]
    fn diff_no_changes() {
        let a = simple_policy();
        let b = simple_policy();
        let diff = checkrd_shared::diff_policies(&a, &b);
        assert_eq!(diff.summary.added, 0);
        assert_eq!(diff.summary.modified, 0);
        assert_eq!(diff.summary.removed, 0);
        assert_eq!(diff.summary.unchanged, 3);
        assert_eq!(diff.default_action.action, "no_op");
    }

    #[test]
    fn diff_added_rule() {
        let a = PolicyConfig {
            default: DefaultAction::Deny,
            mode: checkrd_shared::PolicyMode::default(),
            rules: vec![],
        };
        let b = PolicyConfig {
            default: DefaultAction::Deny,
            mode: checkrd_shared::PolicyMode::default(),
            rules: vec![PolicyRule {
                name: "new-rule".into(),
                kind: PolicyRuleKind::Allow(matcher(vec![HttpMethod::GET], Some("*"))),
                source: None,
            }],
        };
        let diff = checkrd_shared::diff_policies(&a, &b);
        assert_eq!(diff.summary.added, 1);
        assert_eq!(diff.summary.removed, 0);
        assert_eq!(diff.rules[0].action, "create");
        assert_eq!(diff.rules[0].name, "new-rule");
    }

    #[test]
    fn diff_removed_rule() {
        let a = PolicyConfig {
            default: DefaultAction::Deny,
            mode: checkrd_shared::PolicyMode::default(),
            rules: vec![PolicyRule {
                name: "old-rule".into(),
                kind: PolicyRuleKind::Allow(matcher(vec![HttpMethod::GET], Some("*"))),
                source: None,
            }],
        };
        let b = PolicyConfig {
            default: DefaultAction::Deny,
            mode: checkrd_shared::PolicyMode::default(),
            rules: vec![],
        };
        let diff = checkrd_shared::diff_policies(&a, &b);
        assert_eq!(diff.summary.removed, 1);
        assert_eq!(diff.rules[0].action, "delete");
    }

    #[test]
    fn diff_default_action_changed() {
        let a = PolicyConfig {
            default: DefaultAction::Allow,
            mode: checkrd_shared::PolicyMode::default(),
            rules: vec![],
        };
        let b = PolicyConfig {
            default: DefaultAction::Deny,
            mode: checkrd_shared::PolicyMode::default(),
            rules: vec![],
        };
        let diff = checkrd_shared::diff_policies(&a, &b);
        assert_eq!(diff.default_action.action, "update");
    }

    #[test]
    fn diff_mode_changed() {
        let a = PolicyConfig {
            default: DefaultAction::Deny,
            mode: PolicyMode::Enforce,
            rules: vec![],
        };
        let b = PolicyConfig {
            default: DefaultAction::Deny,
            mode: PolicyMode::DryRun,
            rules: vec![],
        };
        let diff = checkrd_shared::diff_policies(&a, &b);
        assert_eq!(diff.mode.action, "update");
    }

    // ================================================================
    // Phase 3: Policy merge + conflict detection
    // ================================================================

    fn org_policy() -> PolicyConfig {
        PolicyConfig {
            default: DefaultAction::Deny,
            mode: PolicyMode::default(),
            rules: vec![
                PolicyRule {
                    name: "org-block-deletes".into(),
                    kind: PolicyRuleKind::Deny(matcher(vec![HttpMethod::DELETE], Some("*"))),
                    source: None,
                },
                PolicyRule {
                    name: "org-allow-read".into(),
                    kind: PolicyRuleKind::Allow(matcher(vec![HttpMethod::GET], Some("**"))),
                    source: None,
                },
                PolicyRule {
                    name: "org-global-rate-limit".into(),
                    kind: PolicyRuleKind::Limit(RateLimitConfig {
                        calls_per_minute: 100,
                        per: checkrd_shared::RateLimitScope::Global,
                        field: None,
                    }),
                    source: None,
                },
            ],
        }
    }

    fn agent_policy() -> PolicyConfig {
        PolicyConfig {
            default: DefaultAction::Allow,
            mode: PolicyMode::default(),
            rules: vec![
                PolicyRule {
                    name: "agent-allow-stripe".into(),
                    kind: PolicyRuleKind::Allow(matcher(
                        vec![HttpMethod::POST],
                        Some("api.stripe.com/**"),
                    )),
                    source: None,
                },
                PolicyRule {
                    name: "agent-rate-limit".into(),
                    kind: PolicyRuleKind::Limit(RateLimitConfig {
                        calls_per_minute: 50,
                        per: checkrd_shared::RateLimitScope::Global,
                        field: None,
                    }),
                    source: None,
                },
            ],
        }
    }

    #[test]
    fn merge_deny_rules_unioned() {
        let merged = checkrd_shared::merge_policies(&org_policy(), &agent_policy());
        let deny_rules: Vec<_> = merged
            .rules
            .iter()
            .filter(|r| matches!(r.kind, PolicyRuleKind::Deny(_)))
            .collect();
        assert_eq!(deny_rules.len(), 1, "org deny rule should be present");
        assert_eq!(deny_rules[0].source.as_deref(), Some("org"));
    }

    #[test]
    fn merge_agent_allows_replace_org_allows() {
        let merged = checkrd_shared::merge_policies(&org_policy(), &agent_policy());
        let allow_rules: Vec<_> = merged
            .rules
            .iter()
            .filter(|r| matches!(r.kind, PolicyRuleKind::Allow(_)))
            .collect();
        // Agent has allow rules, so agent's allows replace org's allows
        assert_eq!(allow_rules.len(), 1);
        assert_eq!(allow_rules[0].name, "agent-allow-stripe");
        assert_eq!(allow_rules[0].source.as_deref(), Some("agent"));
    }

    #[test]
    fn merge_org_allows_used_when_agent_has_none() {
        let agent = PolicyConfig {
            default: DefaultAction::Deny,
            mode: PolicyMode::default(),
            rules: vec![], // No rules at all
        };
        let merged = checkrd_shared::merge_policies(&org_policy(), &agent);
        let allow_rules: Vec<_> = merged
            .rules
            .iter()
            .filter(|r| matches!(r.kind, PolicyRuleKind::Allow(_)))
            .collect();
        // Agent has no allows, so org's allows are used
        assert_eq!(allow_rules.len(), 1);
        assert_eq!(allow_rules[0].name, "org-allow-read");
        assert_eq!(allow_rules[0].source.as_deref(), Some("org"));
    }

    #[test]
    fn merge_agent_default_overrides_org() {
        let merged = checkrd_shared::merge_policies(&org_policy(), &agent_policy());
        assert_eq!(merged.default, DefaultAction::Allow); // agent's default
    }

    #[test]
    fn merge_rate_limits_most_restrictive() {
        let merged = checkrd_shared::merge_policies(&org_policy(), &agent_policy());
        let rate_limits: Vec<_> = merged
            .rules
            .iter()
            .filter(|r| matches!(r.kind, PolicyRuleKind::Limit(_)))
            .collect();
        assert_eq!(rate_limits.len(), 1);
        if let PolicyRuleKind::Limit(config) = &rate_limits[0].kind {
            assert_eq!(
                config.calls_per_minute, 50,
                "most restrictive rate limit should win"
            );
        } else {
            panic!("expected Limit rule");
        }
    }

    // --- Conflict detection ---

    #[test]
    fn analyze_contradictory_allow_deny() {
        let config = PolicyConfig {
            default: DefaultAction::Deny,
            mode: PolicyMode::default(),
            rules: vec![
                PolicyRule {
                    name: "deny-all-deletes".into(),
                    kind: PolicyRuleKind::Deny(matcher(vec![HttpMethod::DELETE], Some("*"))),
                    source: None,
                },
                PolicyRule {
                    name: "allow-all-deletes".into(),
                    kind: PolicyRuleKind::Allow(matcher(vec![HttpMethod::DELETE], Some("*"))),
                    source: None,
                },
            ],
        };
        let analysis = checkrd_shared::analyze_policy(&config);
        assert!(
            analysis
                .warnings
                .iter()
                .any(|w| w.kind == "contradictory_rules"),
            "should detect contradictory allow/deny rules"
        );
    }

    #[test]
    fn analyze_redundant_deny_rules() {
        let config = PolicyConfig {
            default: DefaultAction::Allow,
            mode: PolicyMode::default(),
            rules: vec![
                PolicyRule {
                    name: "deny-a".into(),
                    kind: PolicyRuleKind::Deny(matcher(vec![HttpMethod::DELETE], Some("*"))),
                    source: None,
                },
                PolicyRule {
                    name: "deny-b".into(),
                    kind: PolicyRuleKind::Deny(matcher(vec![HttpMethod::DELETE], Some("*"))),
                    source: None,
                },
            ],
        };
        let analysis = checkrd_shared::analyze_policy(&config);
        assert!(
            analysis.warnings.iter().any(|w| w.kind == "redundant_rule"),
            "should detect redundant deny rules"
        );
    }

    #[test]
    fn analyze_overly_broad_allow() {
        let config = PolicyConfig {
            default: DefaultAction::Deny,
            mode: PolicyMode::default(),
            rules: vec![PolicyRule {
                name: "allow-everything".into(),
                kind: PolicyRuleKind::Allow(matcher(vec![], Some("*"))),
                source: None,
            }],
        };
        let analysis = checkrd_shared::analyze_policy(&config);
        assert!(
            analysis
                .warnings
                .iter()
                .any(|w| w.kind == "overly_broad_allow"),
            "should detect overly broad allow rule"
        );
    }

    #[test]
    fn analyze_no_warnings_on_clean_policy() {
        let analysis = checkrd_shared::analyze_policy(&simple_policy());
        assert_eq!(
            analysis.summary.warnings, 0,
            "clean policy should have no warnings"
        );
    }

    #[test]
    fn analyze_unreachable_default_deny_no_allows() {
        let config = PolicyConfig {
            default: DefaultAction::Deny,
            mode: PolicyMode::default(),
            rules: vec![PolicyRule {
                name: "deny-deletes".into(),
                kind: PolicyRuleKind::Deny(matcher(vec![HttpMethod::DELETE], Some("*"))),
                source: None,
            }],
        };
        let analysis = checkrd_shared::analyze_policy(&config);
        assert!(
            analysis
                .warnings
                .iter()
                .any(|w| w.kind == "unreachable_config"),
            "should warn about default deny with no allow rules"
        );
    }

    #[test]
    fn analyze_redundant_rate_limits() {
        let config = PolicyConfig {
            default: DefaultAction::Allow,
            mode: PolicyMode::default(),
            rules: vec![
                PolicyRule {
                    name: "limit-100".into(),
                    kind: PolicyRuleKind::Limit(RateLimitConfig {
                        calls_per_minute: 100,
                        per: checkrd_shared::RateLimitScope::Global,
                        field: None,
                    }),
                    source: None,
                },
                PolicyRule {
                    name: "limit-50".into(),
                    kind: PolicyRuleKind::Limit(RateLimitConfig {
                        calls_per_minute: 50,
                        per: checkrd_shared::RateLimitScope::Global,
                        field: None,
                    }),
                    source: None,
                },
            ],
        };
        let analysis = checkrd_shared::analyze_policy(&config);
        assert!(
            analysis
                .warnings
                .iter()
                .any(|w| w.kind == "redundant_rate_limit"),
            "should detect redundant rate limits"
        );
    }

    // ============================================================
    // Property-based URL matching tests
    //
    // The example tests above pin specific URL/pattern shapes;
    // these properties exercise the gitignore-style matcher across
    // arbitrary inputs, catching bugs in `**` handling, trailing-
    // slash normalization, and specificity scoring that would only
    // fail under unusual segment combinations.
    // ============================================================

    use proptest::prelude::*;

    /// Strategy: a single non-empty URL segment built from a small alphabet
    /// that excludes `/`, `*`, and whitespace — so the segment is always a
    /// literal token, never a glob meta-character or path separator.
    fn segment_strategy() -> impl Strategy<Value = String> {
        proptest::string::string_regex("[a-zA-Z0-9._-]{1,12}")
            .unwrap()
            .prop_filter("non-empty", |s| !s.is_empty())
    }

    /// Strategy: a vector of 1-5 URL segments, joined to form `host/p1/p2`.
    fn url_segments_strategy() -> impl Strategy<Value = Vec<String>> {
        proptest::collection::vec(segment_strategy(), 1..6)
    }

    proptest! {
        #![proptest_config(ProptestConfig::with_cases(256))]

        /// Identity: a literal pattern always matches its own URL.
        ///
        /// This is the most basic correctness property — if it ever
        /// regresses, every literal-rule policy in production is broken.
        #[test]
        fn property_literal_pattern_matches_self(segments in url_segments_strategy()) {
            let url = segments.join("/");
            let pattern = parse_pattern(&url);
            prop_assert!(
                url_matches(&pattern, &url),
                "literal pattern should match its own URL: {url}"
            );
        }

        /// Single-segment wildcard `*` matches any URL — this is the
        /// documented "match-everything" shorthand.
        #[test]
        fn property_star_matches_any_url(segments in url_segments_strategy()) {
            let url = segments.join("/");
            let pattern = parse_pattern("*");
            prop_assert!(url_matches(&pattern, &url), "* should match {url}");
        }

        /// Single-segment double-wildcard `**` matches any URL.
        #[test]
        fn property_double_star_matches_any_url(segments in url_segments_strategy()) {
            let url = segments.join("/");
            let pattern = parse_pattern("**");
            prop_assert!(url_matches(&pattern, &url), "** should match {url}");
        }

        /// Specificity counts literal segments only — a `*` or `**` adds
        /// zero to the score. The evaluator relies on this to apply more-
        /// specific rules before broader ones; an off-by-one would silently
        /// reorder rule precedence.
        #[test]
        fn property_specificity_counts_literals_only(
            literal_count in 0usize..6,
            wildcard_count in 0usize..3,
            doublewild_count in 0usize..3,
        ) {
            let mut segments = Vec::new();
            for i in 0..literal_count {
                segments.push(PatternSegment::Literal(format!("seg{i}")));
            }
            for _ in 0..wildcard_count {
                segments.push(PatternSegment::Wildcard);
            }
            for _ in 0..doublewild_count {
                segments.push(PatternSegment::DoubleWildcard);
            }
            prop_assert_eq!(specificity(&segments), literal_count as u32);
        }

        /// `prefix/**` matches any URL whose initial segments equal
        /// `prefix`. This is the common allow-listing pattern (e.g.,
        /// `api.stripe.com/v1/**`) and a regression here would silently
        /// over-deny or over-allow.
        #[test]
        fn property_double_star_suffix_matches_prefix(
            prefix in url_segments_strategy(),
            tail in url_segments_strategy(),
        ) {
            let prefix_str = prefix.join("/");
            let pattern_str = format!("{prefix_str}/**");
            let url = format!("{prefix_str}/{}", tail.join("/"));
            let pattern = parse_pattern(&pattern_str);
            prop_assert!(
                url_matches(&pattern, &url),
                "{pattern_str} should match {url}"
            );
        }

        /// Without `**`, segment counts must agree exactly. A literal
        /// pattern of N segments cannot match a URL of M ≠ N segments.
        #[test]
        fn property_literal_pattern_rejects_wrong_segment_count(
            pattern_segments in url_segments_strategy(),
            url_segments in url_segments_strategy(),
        ) {
            // Only test when the lengths actually differ.
            prop_assume!(pattern_segments.len() != url_segments.len());
            let pattern = parse_pattern(&pattern_segments.join("/"));
            let url = url_segments.join("/");
            prop_assert!(
                !url_matches(&pattern, &url),
                "pattern of len {} should not match URL of len {}",
                pattern_segments.len(),
                url_segments.len()
            );
        }
    }
}
