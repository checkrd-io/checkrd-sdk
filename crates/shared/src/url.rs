/// Parsed representation of a request URL with host and normalized path.
pub struct ParsedUrl {
    pub host: String,
    pub path: String,
}

/// Parse and normalize a request URL into its host and path components.
///
/// Handles `https://`, `http://`, and scheme-less inputs. Strips userinfo,
/// default ports (80/443), query strings, fragments, and percent-encodes.
/// Applies `.`/`..` segment normalization to prevent path-traversal bypasses
/// and calls [`crate::parameterize_path`] to replace dynamic segments with
/// `{id}` before any pattern matching occurs.
pub fn parse_url(url: &str) -> ParsedUrl {
    let without_scheme = url
        .strip_prefix("https://")
        .or_else(|| url.strip_prefix("http://"))
        .unwrap_or(url);

    // Strip userinfo (user:pass@host). Only look for @ before the first
    // slash — an @ in the path (e.g. /users/john@email.com/profile) is
    // part of the URL path, not userinfo.
    let authority_end = without_scheme.find('/').unwrap_or(without_scheme.len());
    let without_userinfo = match without_scheme[..authority_end].rfind('@') {
        Some(idx) => &without_scheme[idx + 1..],
        None => without_scheme,
    };

    let (raw_host, raw_path) = match without_userinfo.find('/') {
        Some(idx) => (&without_userinfo[..idx], &without_userinfo[idx..]),
        None => (without_userinfo, "/"),
    };

    // Normalize host: lowercase, strip default ports
    let host = raw_host.to_ascii_lowercase();
    let host = host
        .strip_suffix(":443")
        .or_else(|| host.strip_suffix(":80"))
        .unwrap_or(&host)
        .to_string();

    // Decode percent-encoding in path, strip query string and fragment
    let path_without_query = match raw_path.find('?') {
        Some(idx) => &raw_path[..idx],
        None => raw_path,
    };
    let path_without_fragment = match path_without_query.find('#') {
        Some(idx) => &path_without_query[..idx],
        None => path_without_query,
    };
    let decoded = percent_decode(path_without_fragment);
    let normalized = normalize_path(&decoded);
    let path = crate::parameterize_path(&normalized);

    ParsedUrl { host, path }
}

fn percent_decode(input: &str) -> String {
    let mut output = String::with_capacity(input.len());
    let mut chars = input.bytes();
    while let Some(b) = chars.next() {
        if b == b'%' {
            let hi = chars.next();
            let lo = chars.next();
            if let (Some(h), Some(l)) = (hi, lo) {
                if let (Some(hv), Some(lv)) = (hex_val(h), hex_val(l)) {
                    output.push((hv << 4 | lv) as char);
                    continue;
                }
            }
            // Malformed percent-encoding: keep as-is
            output.push('%');
        } else {
            output.push(b as char);
        }
    }
    output
}

/// Resolve `.` and `..` segments and collapse empty segments (double slashes).
/// This prevents path traversal bypasses where `/v1/../v2/charges` would evade
/// a rule matching `/v2/charges`.
fn normalize_path(path: &str) -> String {
    let mut segments: Vec<&str> = Vec::new();
    for seg in path.split('/') {
        match seg {
            "" | "." => {}
            ".." => {
                segments.pop();
            }
            s => segments.push(s),
        }
    }
    let mut result = String::with_capacity(path.len());
    result.push('/');
    result.push_str(&segments.join("/"));
    result
}

fn hex_val(b: u8) -> Option<u8> {
    match b {
        b'0'..=b'9' => Some(b - b'0'),
        b'a'..=b'f' => Some(b - b'a' + 10),
        b'A'..=b'F' => Some(b - b'A' + 10),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn https_url() {
        let parsed = parse_url("https://api.stripe.com/v1/charges");
        assert_eq!(parsed.host, "api.stripe.com");
        assert_eq!(parsed.path, "/v1/charges");
    }

    #[test]
    fn http_url() {
        let parsed = parse_url("http://example.com/api/test");
        assert_eq!(parsed.host, "example.com");
        assert_eq!(parsed.path, "/api/test");
    }

    #[test]
    fn no_scheme() {
        let parsed = parse_url("api.salesforce.com/data/v58.0");
        assert_eq!(parsed.host, "api.salesforce.com");
        assert_eq!(parsed.path, "/data/v58.0");
    }

    #[test]
    fn host_only_no_path() {
        let parsed = parse_url("https://example.com");
        assert_eq!(parsed.host, "example.com");
        assert_eq!(parsed.path, "/");
    }

    #[test]
    fn host_only_no_scheme() {
        let parsed = parse_url("example.com");
        assert_eq!(parsed.host, "example.com");
        assert_eq!(parsed.path, "/");
    }

    // --- Security: URL normalization ---

    #[test]
    fn percent_encoded_path() {
        let parsed = parse_url("https://api.stripe.com/v1%2Fcharges");
        assert_eq!(parsed.host, "api.stripe.com");
        assert_eq!(parsed.path, "/v1/charges");
    }

    #[test]
    fn host_case_normalized() {
        let parsed = parse_url("https://API.STRIPE.COM/v1/charges");
        assert_eq!(parsed.host, "api.stripe.com");
    }

    #[test]
    fn default_port_stripped() {
        let parsed = parse_url("https://api.stripe.com:443/v1/charges");
        assert_eq!(parsed.host, "api.stripe.com");
        assert_eq!(parsed.path, "/v1/charges");
    }

    #[test]
    fn non_default_port_preserved() {
        let parsed = parse_url("https://api.stripe.com:8080/v1/charges");
        assert_eq!(parsed.host, "api.stripe.com:8080");
    }

    #[test]
    fn userinfo_stripped() {
        let parsed = parse_url("https://user:pass@api.stripe.com/v1/charges");
        assert_eq!(parsed.host, "api.stripe.com");
        assert_eq!(parsed.path, "/v1/charges");
    }

    #[test]
    fn query_string_stripped() {
        let parsed = parse_url("https://api.stripe.com/v1/charges?key=value&foo=bar");
        assert_eq!(parsed.path, "/v1/charges");
    }

    #[test]
    fn fragment_stripped() {
        let parsed = parse_url("https://api.stripe.com/v1/charges#section");
        assert_eq!(parsed.path, "/v1/charges");
    }

    // --- Security: path traversal normalization ---

    #[test]
    fn path_traversal_resolved() {
        let parsed = parse_url("https://api.stripe.com/v1/../v2/charges");
        assert_eq!(parsed.path, "/v2/charges");
    }

    #[test]
    fn path_traversal_at_root() {
        let parsed = parse_url("https://api.stripe.com/../../v1/charges");
        assert_eq!(parsed.path, "/v1/charges");
    }

    #[test]
    fn double_slash_collapsed() {
        let parsed = parse_url("https://api.stripe.com//v1//charges");
        assert_eq!(parsed.path, "/v1/charges");
    }

    #[test]
    fn dot_segments_removed() {
        let parsed = parse_url("https://api.stripe.com/./v1/./charges");
        assert_eq!(parsed.path, "/v1/charges");
    }

    #[test]
    fn encoded_traversal_resolved() {
        // %2E%2E = ".."  after percent decoding
        let parsed = parse_url("https://api.stripe.com/v1/%2E%2E/v2/charges");
        assert_eq!(parsed.path, "/v2/charges");
    }

    #[test]
    fn root_path_stays_root() {
        let parsed = parse_url("https://api.stripe.com/");
        assert_eq!(parsed.path, "/");
    }

    // --- PII safety: path parameterization via parse_url ---

    #[test]
    fn uuid_parameterized() {
        let parsed =
            parse_url("https://api.example.com/users/550e8400-e29b-41d4-a716-446655440000/profile");
        assert_eq!(parsed.path, "/users/{id}/profile");
    }

    #[test]
    fn numeric_id_parameterized() {
        let parsed = parse_url("https://api.example.com/patients/12345/records");
        assert_eq!(parsed.path, "/patients/{id}/records");
    }

    #[test]
    fn hex_id_parameterized() {
        let parsed = parse_url("https://api.example.com/transactions/a1b2c3d4e5f6a7b8");
        assert_eq!(parsed.path, "/transactions/{id}");
    }

    #[test]
    fn stripe_id_parameterized() {
        let parsed = parse_url("https://api.stripe.com/v1/charges/ch_abc123def456ghi");
        assert_eq!(parsed.path, "/v1/charges/{id}");
    }

    #[test]
    fn email_in_path_parameterized() {
        let parsed = parse_url("https://api.example.com/users/john.doe@email.com/profile");
        assert_eq!(parsed.host, "api.example.com");
        assert_eq!(parsed.path, "/users/{id}/profile");
    }

    #[test]
    fn api_version_preserved() {
        let parsed = parse_url("https://api.stripe.com/v1/charges");
        assert_eq!(parsed.path, "/v1/charges");
    }

    #[test]
    fn static_segments_preserved() {
        let parsed = parse_url("https://api.salesforce.com/services/data/v58.0/sobjects/Contact");
        assert_eq!(parsed.path, "/services/data/v58.0/sobjects/Contact");
    }

    #[test]
    fn mixed_static_and_dynamic() {
        let parsed = parse_url("https://api.example.com/v2/orgs/12345/agents/550e8400-e29b-41d4-a716-446655440000/policies");
        assert_eq!(parsed.path, "/v2/orgs/{id}/agents/{id}/policies");
    }

    #[test]
    fn long_token_parameterized() {
        let parsed = parse_url("https://api.example.com/auth/eyJhbGciOiJIUzI1NiJ9/refresh");
        assert_eq!(parsed.path, "/auth/{id}/refresh");
    }

    #[test]
    fn ssn_like_numeric_parameterized() {
        let parsed = parse_url("https://api.hospital.com/patients/123456789/records");
        assert_eq!(parsed.path, "/patients/{id}/records");
    }
}
