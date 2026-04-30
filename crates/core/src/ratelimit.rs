use std::collections::{HashMap, VecDeque};

const WINDOW_MS: u64 = 60_000; // 60 seconds
const MAX_KEYS: usize = 10_000;

struct RateLimitWindow {
    timestamps: VecDeque<u64>,
    last_accessed: u64,
}

#[derive(Default)]
pub struct RateLimiter {
    windows: HashMap<String, RateLimitWindow>,
}

pub enum RateLimitResult {
    Allowed { remaining: u32 },
    Exceeded,
}

impl RateLimitResult {
    pub fn is_exceeded(&self) -> bool {
        matches!(self, Self::Exceeded)
    }
}

impl RateLimiter {
    pub fn new() -> Self {
        Self {
            windows: HashMap::new(),
        }
    }

    pub fn check(&mut self, key: &str, limit: u32, timestamp_ms: u64) -> RateLimitResult {
        if !self.windows.contains_key(key) {
            if self.windows.len() >= MAX_KEYS {
                self.evict(timestamp_ms);
            }
            self.windows.insert(
                key.to_string(),
                RateLimitWindow {
                    timestamps: VecDeque::new(),
                    last_accessed: timestamp_ms,
                },
            );
        }

        let window = self.windows.get_mut(key).unwrap();
        window.last_accessed = timestamp_ms;
        let cutoff = timestamp_ms.saturating_sub(WINDOW_MS);

        // Remove expired entries
        while window.timestamps.front().is_some_and(|&ts| ts < cutoff) {
            window.timestamps.pop_front();
        }

        if window.timestamps.len() >= limit as usize {
            RateLimitResult::Exceeded
        } else {
            window.timestamps.push_back(timestamp_ms);
            RateLimitResult::Allowed {
                remaining: limit - window.timestamps.len() as u32,
            }
        }
    }

    /// Two-phase eviction: first remove entries with all timestamps expired,
    /// then evict least-recently-accessed if still over capacity.
    fn evict(&mut self, now_ms: u64) {
        let cutoff = now_ms.saturating_sub(WINDOW_MS);

        // Phase 1: Remove entries whose timestamps have all expired.
        self.windows.retain(|_, w| {
            while w.timestamps.front().is_some_and(|&ts| ts < cutoff) {
                w.timestamps.pop_front();
            }
            !w.timestamps.is_empty()
        });

        // Phase 2: If still at capacity, evict the least-recently-accessed entries.
        if self.windows.len() >= MAX_KEYS {
            let mut entries: Vec<(String, u64)> = self
                .windows
                .iter()
                .map(|(k, w)| (k.clone(), w.last_accessed))
                .collect();
            entries.sort_unstable_by_key(|(_, ts)| *ts);

            let to_remove = self.windows.len() - MAX_KEYS + 1;
            for (key, _) in entries.into_iter().take(to_remove) {
                self.windows.remove(&key);
            }
        }
    }

    #[cfg(test)]
    fn key_count(&self) -> usize {
        self.windows.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn under_limit() {
        let mut rl = RateLimiter::new();
        let result = rl.check("test", 5, 1000);
        assert!(matches!(result, RateLimitResult::Allowed { remaining: 4 }));
    }

    #[test]
    fn at_limit() {
        let mut rl = RateLimiter::new();
        for i in 0..5 {
            rl.check("test", 5, 1000 + i);
        }
        let result = rl.check("test", 5, 1005);
        assert!(result.is_exceeded());
    }

    #[test]
    fn window_expiry() {
        let mut rl = RateLimiter::new();
        // Fill the window at t=0
        for i in 0..5 {
            rl.check("test", 5, i);
        }
        // At t=60001, the old entries have expired
        let result = rl.check("test", 5, 60_001);
        assert!(matches!(result, RateLimitResult::Allowed { .. }));
    }

    #[test]
    fn per_key_isolation() {
        let mut rl = RateLimiter::new();
        for i in 0..5 {
            rl.check("endpoint-a", 5, 1000 + i);
        }
        // endpoint-a is full, but endpoint-b is empty
        assert!(rl.check("endpoint-a", 5, 1005).is_exceeded());
        assert!(!rl.check("endpoint-b", 5, 1005).is_exceeded());
    }

    #[test]
    fn sliding_window() {
        let mut rl = RateLimiter::new();
        // 3 calls at t=0
        for _ in 0..3 {
            rl.check("test", 5, 0);
        }
        // 2 calls at t=30000 (30 seconds)
        for _ in 0..2 {
            rl.check("test", 5, 30_000);
        }
        // At t=30000, window has 5 entries (all within 60s), should be exceeded
        assert!(rl.check("test", 5, 30_000).is_exceeded());

        // At t=60001, the first 3 entries (t=0) expire, 2 remain
        let result = rl.check("test", 5, 60_001);
        assert!(matches!(result, RateLimitResult::Allowed { remaining: 2 }));
    }

    #[test]
    fn eviction_removes_expired_keys() {
        let mut rl = RateLimiter::new();
        // Fill to MAX_KEYS with entries at t=0
        for i in 0..MAX_KEYS {
            rl.check(&format!("key-{i}"), 100, 0);
        }
        assert_eq!(rl.key_count(), MAX_KEYS);

        // Insert one more at t=60001 (all previous timestamps expired)
        rl.check("new-key", 100, 60_001);

        // Phase 1 eviction should have cleaned up all expired entries
        // Only the newly inserted key (and any with active timestamps) remain
        assert!(
            rl.key_count() <= 2,
            "expected most expired keys evicted, got {}",
            rl.key_count()
        );
        assert!(rl.windows.contains_key("new-key"));
    }

    #[test]
    fn eviction_lru_order() {
        let mut rl = RateLimiter::new();
        // Fill to MAX_KEYS. Key "key-0" accessed at t=0, "key-1" at t=1, etc.
        // All within the window (no timestamp expiry).
        for i in 0..MAX_KEYS {
            rl.check(&format!("key-{i}"), 100, i as u64);
        }
        assert_eq!(rl.key_count(), MAX_KEYS);

        // Access key-0 again at a recent time to make it NOT the oldest-accessed
        rl.check("key-0", 100, MAX_KEYS as u64);

        // Insert a new key -- should trigger LRU eviction
        rl.check("overflow", 100, MAX_KEYS as u64 + 1);

        // key-1 was the oldest-accessed (accessed at t=1), should be evicted
        assert!(
            !rl.windows.contains_key("key-1"),
            "oldest-accessed key should be evicted"
        );
        // key-0 was re-accessed, should survive
        assert!(
            rl.windows.contains_key("key-0"),
            "recently-accessed key should survive"
        );
        // new key should exist
        assert!(rl.windows.contains_key("overflow"));
    }

    #[test]
    fn normal_operation_unaffected_under_capacity() {
        let mut rl = RateLimiter::new();
        // Normal usage well under MAX_KEYS
        for i in 0..100 {
            let result = rl.check(&format!("endpoint-{i}"), 10, 1000);
            assert!(!result.is_exceeded());
        }
        assert_eq!(rl.key_count(), 100);
        // All keys survive -- no eviction triggered
        for i in 0..100 {
            assert!(rl.windows.contains_key(&format!("endpoint-{i}")));
        }
    }

    // ============================================================
    // Property-based tests
    //
    // Example tests verify specific scenarios; these verify the
    // sliding-window invariants hold under arbitrary call sequences.
    // Bugs that pass example tests but break under unusual inputs
    // (large gaps, monotonically increasing timestamps, exotic limit
    // values) surface here.
    // ============================================================

    use proptest::prelude::*;

    proptest! {
        #![proptest_config(ProptestConfig::with_cases(256))]

        /// At most `limit` `Allowed` results across any sequence of checks
        /// against a single key with timestamps inside one window.
        ///
        /// This is the core safety guarantee of the rate limiter: no matter
        /// how many checks come in within a 60s window, at most `limit`
        /// succeed. Counterexamples here would mean the limiter undercounts
        /// requests — a security bug.
        #[test]
        fn property_allowed_count_never_exceeds_limit(
            limit in 1u32..50,
            // All timestamps in [0, WINDOW_MS - 1] => same window
            timestamps in proptest::collection::vec(0u64..WINDOW_MS - 1, 0..200),
        ) {
            let mut rl = RateLimiter::new();
            let mut allowed_count = 0u32;
            for ts in &timestamps {
                if !rl.check("k", limit, *ts).is_exceeded() {
                    allowed_count += 1;
                }
            }
            prop_assert!(
                allowed_count <= limit,
                "allowed_count {allowed_count} exceeded limit {limit}"
            );
        }

        /// Per-key isolation: requests to key A never affect key B's count.
        ///
        /// If two keys' counters bled together a malicious tenant could
        /// exhaust another tenant's budget. This property is the structural
        /// guarantee that prevents that.
        #[test]
        fn property_keys_are_independent(
            limit in 1u32..20,
            count_a in 0u32..40,
            count_b in 0u32..40,
            ts in 0u64..WINDOW_MS - 1,
        ) {
            let mut rl = RateLimiter::new();
            // Pound on key A first
            for _ in 0..count_a {
                rl.check("a", limit, ts);
            }
            // Now key B should still get up to `limit` requests
            let mut b_allowed = 0u32;
            for _ in 0..count_b {
                if !rl.check("b", limit, ts).is_exceeded() {
                    b_allowed += 1;
                }
            }
            let expected_b_allowed = count_b.min(limit);
            prop_assert_eq!(b_allowed, expected_b_allowed);
        }

        /// After an entire window has elapsed, all prior timestamps expire
        /// and a fresh request is `Allowed` regardless of how many requests
        /// came before.
        ///
        /// This pins the "sliding" semantics: a hammered key recovers
        /// fully once the window slides past every prior timestamp.
        #[test]
        fn property_expired_window_recovers(
            limit in 1u32..20,
            initial_count in 0u32..100,
            // Choose a fresh timestamp strictly past the window boundary
            // for every initial timestamp (which all live at t=0).
            future_offset in (WINDOW_MS + 1)..(WINDOW_MS * 10),
        ) {
            let mut rl = RateLimiter::new();
            for _ in 0..initial_count {
                rl.check("k", limit, 0);
            }
            // After WINDOW_MS+ elapses, a fresh request must be allowed.
            let result = rl.check("k", limit, future_offset);
            prop_assert!(
                !result.is_exceeded(),
                "limiter did not recover after window expired ({future_offset}ms)"
            );
        }
    }
}
