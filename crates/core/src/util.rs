/// Re-export from checkrd-shared so the rest of core can use parse_url
/// without referencing the shared crate directly at each call site.
pub(crate) use checkrd_shared::url::{parse_url, ParsedUrl};
