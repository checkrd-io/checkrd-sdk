use thiserror::Error;

#[derive(Debug, Error)]
#[non_exhaustive]
pub enum PolicyError {
    #[error("invalid policy configuration: {0}")]
    InvalidConfig(String),

    #[error("unknown HTTP method: {0}")]
    UnknownMethod(String),

    #[error("invalid URL pattern: {0}")]
    InvalidUrlPattern(String),

    #[error("policy deserialization failed: {0}")]
    DeserializationError(#[from] serde_json::Error),
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn error_display() {
        let err = PolicyError::InvalidConfig("missing agent field".into());
        assert_eq!(
            err.to_string(),
            "invalid policy configuration: missing agent field"
        );
    }

    #[test]
    fn error_from_serde() {
        let serde_err = serde_json::from_str::<String>("not valid json").unwrap_err();
        let policy_err = PolicyError::from(serde_err);
        assert!(matches!(policy_err, PolicyError::DeserializationError(_)));
    }
}
