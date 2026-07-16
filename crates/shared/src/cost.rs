//! Integer micro-USD money math for cost metering (TDD §4.4, ADR-003).
//!
//! Every LLM call's cost is computed **in the WASM core, inside the
//! Ed25519-signed telemetry batch** (ADR-002), so the figure is a signed,
//! independently verifiable spend record rather than a wrapper-side estimate.
//! This module is the arithmetic primitive that computation stands on; the
//! SKU-resolution and `settle_usage` FFI that consume it live in
//! `crates/core` (M-4).
//!
//! # Why integer micro-USD
//!
//! Money is **never** represented as floating point on this path (ADR-003):
//! binary floats cannot represent most decimal currency amounts exactly, so
//! two implementations adding the same prices can disagree in the last digit
//! — fatal for a *signed, reproducible* spend record. Instead a dollar is an
//! integer count of **micro-USD** (millionths of a dollar):
//!
//! ```text
//! 1 USD = 1_000_000 micro-USD        (so $1.23 == 1_230_000 micros)
//! ```
//!
//! Micros are not a formal standard — they are the integer-money convention of
//! the Google Ads API family and sit one scale below ISO 4217's two-decimal
//! minor unit, giving sub-cent precision for per-token prices (a model at
//! `$0.000003 / token` is `3` micros/token, exact). FOCUS 1.2 exports divide
//! these micros by `10^6` to recover the decimal `BilledCost` (TDD App. D).
//!
//! # LLM pricing is quoted per 1,000,000 tokens
//!
//! Provider list prices are quoted per million tokens (e.g. `$3.00 / 1M input
//! tokens`). A `PriceSku` therefore stores
//! `*_usd_micros_per_unit` with `unit = per_1m_tokens`, and a line item is:
//!
//! ```text
//! cost_micros = round_half_up( tokens * micros_per_1m_tokens / 1_000_000 )
//! ```
//!
//! # Saturate, never trap
//!
//! Per ADR-003 the engine **MUST NOT** panic on arithmetic overflow. Every
//! operation here widens to `i128` (which holds any `i64 * i64` product
//! exactly), and if the result cannot fit back into `i64` it **saturates** to
//! [`i64::MAX`]/[`i64::MIN`] and sets [`Cost::overflow`]. A caller surfaces the
//! flag as `SettleResult.overflow = true` so the dashboard marks the figure
//! approximate instead of dropping the event or crashing the agent's process.
//! In practice overflow is unreachable (a single call would need ~9.2e18 micros
//! ≈ $9.2 trillion); the flag exists so the property is *total*, not aspirational.
//!
//! # Round-half-up, declared in the bundle
//!
//! The rounding mode is `half_up` and is declared inside the signed pricing
//! bundle (`Rounding`) so any independent
//! implementation reproduces byte-identical signed values. "Half up" here means
//! *round halves toward positive infinity*; on the non-negative money domain
//! (token counts and prices are `≥ 0`) that is identical to round-half-away-
//! from-zero. Inputs outside that domain still return a deterministic value and
//! never panic, but the round-half-up guarantee is stated only for `≥ 0`.

/// Micro-USD in one US dollar. `$1.23 == 1_230_000` micros.
pub const MICROS_PER_USD: i64 = 1_000_000;

/// Tokens in one pricing unit. Provider list prices are quoted per 1,000,000
/// tokens, so a `PriceSku`'s `*_per_unit` price is the
/// cost of this many tokens.
pub const TOKENS_PER_PRICE_UNIT: i64 = 1_000_000;

/// A money amount in integer micro-USD, carrying a saturation flag.
///
/// Construct line items with [`Cost::line_item`] and combine them with
/// [`Cost::saturating_add`]; the `overflow` flag is **sticky** (any saturated
/// term taints the total) so a single `Cost` faithfully maps onto a
/// `SettleResult`'s `{ cost_usd_micros, overflow }` pair (TDD §4.4).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub struct Cost {
    /// Cost in micro-USD (millionths of a dollar). Saturated to
    /// [`i64::MAX`]/[`i64::MIN`] when `overflow` is set.
    pub micros: i64,
    /// `true` iff some operation saturated rather than representing the exact
    /// value. Sticky across [`Cost::saturating_add`].
    pub overflow: bool,
}

impl Cost {
    /// The additive identity: zero micros, no overflow.
    pub const ZERO: Cost = Cost {
        micros: 0,
        overflow: false,
    };

    /// An exact, known-good amount in micro-USD (no rounding or saturation).
    pub const fn from_micros(micros: i64) -> Self {
        Cost {
            micros,
            overflow: false,
        }
    }

    /// Line-item cost of `tokens` priced at `micros_per_1m_tokens`
    /// (a `PriceSku` `*_usd_micros_per_unit` value),
    /// rounded half-up, saturating to [`i64::MAX`] with `overflow = true`.
    ///
    /// The money domain is non-negative: `tokens` is a validated count `≥ 0`
    /// and prices are `≥ 0` in any well-formed bundle. The computation never
    /// panics for *any* `i64` inputs, but the round-half-up guarantee holds for
    /// `≥ 0` (see the module docs).
    pub fn line_item(tokens: i64, micros_per_1m_tokens: i64) -> Self {
        // i128 holds any i64 * i64 product exactly, so the multiply cannot
        // overflow before we round and saturate deliberately.
        let product = i128::from(tokens) * i128::from(micros_per_1m_tokens);
        let divisor = i128::from(TOKENS_PER_PRICE_UNIT);
        // Round half up: add half the divisor before truncating division. On
        // the non-negative domain truncating division floors, so this is exact
        // round-half-toward-+infinity.
        let rounded = (product + divisor / 2) / divisor;
        Self::from_i128_saturating(rounded)
    }

    /// Saturating sum of two costs. Commutative and (on the non-negative money
    /// domain) associative, so a batch total is independent of summation order.
    /// `overflow` is the logical OR of both operands' flags and any saturation
    /// in this addition.
    #[must_use]
    pub fn saturating_add(self, other: Cost) -> Cost {
        let sum = i128::from(self.micros) + i128::from(other.micros);
        let mut total = Self::from_i128_saturating(sum);
        total.overflow |= self.overflow | other.overflow;
        total
    }

    /// Clamp an `i128` into an `i64` `Cost`, flagging overflow on saturation.
    fn from_i128_saturating(value: i128) -> Self {
        if value > i128::from(i64::MAX) {
            Cost {
                micros: i64::MAX,
                overflow: true,
            }
        } else if value < i128::from(i64::MIN) {
            Cost {
                micros: i64::MIN,
                overflow: true,
            }
        } else {
            Cost {
                micros: value as i64,
                overflow: false,
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    // --- worked examples -------------------------------------------------

    #[test]
    fn zero_tokens_cost_nothing() {
        assert_eq!(Cost::line_item(0, 3_000_000), Cost::ZERO);
        assert_eq!(Cost::line_item(1500, 0), Cost::ZERO);
    }

    #[test]
    fn exact_line_item() {
        // 1500 input tokens at $3.00 / 1M = 3_000_000 micros / 1M tokens.
        // 1500 * 3_000_000 / 1_000_000 = 4500 micros ($0.0045), exact.
        assert_eq!(Cost::line_item(1500, 3_000_000), Cost::from_micros(4500));
    }

    #[test]
    fn rounds_half_up_at_the_boundary() {
        // 1 token * 1_500_000 / 1_000_000 = 1.5 -> 2 (half up).
        assert_eq!(Cost::line_item(1, 1_500_000), Cost::from_micros(2));
        // exactly one half: 0.5 -> 1.
        assert_eq!(Cost::line_item(1, 500_000), Cost::from_micros(1));
        // just below the half rounds down: 0.499999 -> 0.
        assert_eq!(Cost::line_item(1, 499_999), Cost::from_micros(0));
        // just above the half rounds up: 0.500001 -> 1.
        assert_eq!(Cost::line_item(1, 500_001), Cost::from_micros(1));
    }

    #[test]
    fn overflow_saturates_and_flags_instead_of_panicking() {
        let c = Cost::line_item(i64::MAX, i64::MAX);
        assert!(c.overflow);
        assert_eq!(c.micros, i64::MAX);
    }

    #[test]
    fn saturating_add_is_sticky_on_overflow() {
        let overflowed = Cost::line_item(i64::MAX, i64::MAX);
        let small = Cost::from_micros(5);
        // The taint propagates even though 5 + MAX would itself saturate.
        assert!(overflowed.saturating_add(small).overflow);
        // And a clean pair stays clean.
        let clean = Cost::from_micros(10).saturating_add(Cost::from_micros(20));
        assert_eq!(clean, Cost::from_micros(30));
    }

    #[test]
    fn saturating_add_overflow_flag_is_a_logical_or_of_operand_flags() {
        // 10 + 20 = 30 never saturates by value, so the result's flag can only
        // come from OR-ing the two operand flags. This isolates the OR from a
        // hypothetical `&`/`^`: one-flagged proves it is not `&` (would be
        // false), both-flagged proves it is not `^` (would be false).
        let flagged = Cost {
            micros: 10,
            overflow: true,
        };
        let clean = Cost {
            micros: 20,
            overflow: false,
        };
        assert_eq!(
            flagged.saturating_add(clean),
            Cost {
                micros: 30,
                overflow: true
            }
        );
        assert_eq!(
            clean.saturating_add(flagged),
            Cost {
                micros: 30,
                overflow: true
            }
        );
        let flagged2 = Cost {
            micros: 20,
            overflow: true,
        };
        assert_eq!(
            flagged.saturating_add(flagged2),
            Cost {
                micros: 30,
                overflow: true
            }
        );
        // Neither flagged, no value saturation → stays clean.
        assert_eq!(
            clean.saturating_add(Cost {
                micros: 5,
                overflow: false,
            }),
            Cost {
                micros: 25,
                overflow: false
            }
        );
    }

    #[test]
    fn exact_i64_boundary_micros_do_not_falsely_flag_overflow() {
        // A total landing EXACTLY on i64::MAX/MIN is representable and MUST NOT
        // set overflow — this pins the STRICT `>` / `<` comparisons in
        // from_i128_saturating (a `>=` / `<=` there would spuriously flag the
        // exactly-representable extreme).
        assert_eq!(
            Cost::from_micros(i64::MAX).saturating_add(Cost::ZERO),
            Cost {
                micros: i64::MAX,
                overflow: false
            }
        );
        assert_eq!(
            Cost::from_micros(i64::MIN).saturating_add(Cost::ZERO),
            Cost {
                micros: i64::MIN,
                overflow: false
            }
        );
        // One step past the boundary DOES saturate + flag (the other branch).
        assert_eq!(
            Cost::from_micros(i64::MAX).saturating_add(Cost::from_micros(1)),
            Cost {
                micros: i64::MAX,
                overflow: true
            }
        );
        assert_eq!(
            Cost::from_micros(i64::MIN).saturating_add(Cost::from_micros(-1)),
            Cost {
                micros: i64::MIN,
                overflow: true
            }
        );
    }

    #[test]
    fn zero_is_the_additive_identity() {
        let c = Cost::from_micros(42);
        assert_eq!(c.saturating_add(Cost::ZERO), c);
        assert_eq!(Cost::ZERO.saturating_add(c), c);
    }

    #[test]
    fn rounds_half_up_at_exact_half_across_many_scales() {
        // The single-token boundary test above only exercises the smallest
        // scale. A half-up bug can hide at larger token counts (e.g. an
        // off-by-one in the `+ divisor/2` term, or an accidental float path)
        // that 1-token cases never reach. Construct an EXACT half at each scale
        // and check against an INDEPENDENT closed form — `ceil(value)` for a
        // value landing exactly on `N + 0.5` — *not* the impl's own
        // `(product + half) / divisor` formula.
        //
        // price = 500_000 micros/1M-tokens means each token costs 0.5 micros,
        // so an ODD token count `t` yields a product of exactly `t/2` micros,
        // i.e. a value of `t * 0.5` that sits precisely on a `.5` boundary.
        // Round-half-toward-+inf of `t * 0.5` for odd `t ≥ 0` is `(t + 1) / 2`.
        for t in [1i64, 3, 5, 7, 9, 101, 999, 1_001, 123_457, 1_000_001] {
            assert_eq!(t % 2, 1, "test fixture must be odd to land on .5");
            let independent = (t + 1) / 2; // ceil(t * 0.5) by integer arithmetic
            assert_eq!(
                Cost::line_item(t, 500_000),
                Cost::from_micros(independent),
                "exact-half (price=500_000) at t={t} must round up to {independent}"
            );
        }

        // A second, larger price scale so the half lands on a non-trivial
        // integer part. price = 1_500_000 (1.5 micros/token); odd `t` gives a
        // value of `t * 1.5 = (3t)/2`, again exactly on a `.5` boundary.
        // Independent half-up: `(3t + 1) / 2`.
        for t in [1i64, 3, 5, 99, 12_345, 999_999] {
            let independent = (3 * t + 1) / 2;
            assert_eq!(
                Cost::line_item(t, 1_500_000),
                Cost::from_micros(independent),
                "exact-half (price=1_500_000) at t={t} must round up to {independent}"
            );
        }
    }

    #[test]
    fn negative_inputs_are_deterministic_and_do_not_panic() {
        // The module docs guarantee round-half-up only on the non-negative
        // money domain; for negative price/token the result is "deterministic
        // and never panics" but NOT half-up. Nothing else pins what actually
        // happens, so a refactor could silently change the negative behavior.
        // These cases lock in the truncate-toward-zero reality of the i128
        // division (Rust `/` truncates; it does NOT floor), which differs from
        // round-half-toward-+infinity precisely in this domain.
        //
        // value = tokens * price / 1_000_000 (exact rational), shown for context.

        // -1.5  -> truncates the `(−1_000_000)/1_000_000` numerator to -1.
        assert_eq!(Cost::line_item(-1, 1_500_000), Cost::from_micros(-1));
        // Sign carried by the price instead of the token count: same result.
        assert_eq!(Cost::line_item(1, -1_500_000), Cost::from_micros(-1));
        // Two negatives -> positive product -> ordinary half-up (+2 from +1.5).
        assert_eq!(Cost::line_item(-1, -1_500_000), Cost::from_micros(2));
        // -0.5 boundary: numerator is exactly 0 after `+ half`, so -> 0
        // (truncation toward zero, NOT -1 that a floor would give).
        assert_eq!(Cost::line_item(-1, 500_000), Cost::from_micros(0));
        // -1.0 exact: `-1_000_000 + 500_000 = -500_000`, truncates to 0
        // (a documented non-half-up artifact on the negative domain).
        assert_eq!(Cost::line_item(-1, 1_000_000), Cost::from_micros(0));
        // None of these set the overflow flag — they are in range.
        for (t, p) in [(-1, 1_500_000), (1, -1_500_000), (-7, 999_999)] {
            assert!(
                !Cost::line_item(t, p).overflow,
                "in-range negative inputs must not flag overflow: ({t}, {p})"
            );
        }
        // Determinism: identical inputs yield byte-identical output.
        assert_eq!(Cost::line_item(-12_345, -67), Cost::line_item(-12_345, -67));
    }

    #[test]
    fn line_item_saturates_to_min_on_large_negative_product() {
        // Mirror of the MAX-side saturation test, on the i64::MIN side: a large
        // negative product must clamp to i64::MIN *and* raise overflow rather
        // than wrapping or panicking.
        let c = Cost::line_item(i64::MIN, i64::MAX);
        assert!(c.overflow, "underflow must set the overflow flag");
        assert_eq!(c.micros, i64::MIN, "must clamp to i64::MIN, not wrap");

        // i64::MIN * i64::MIN is a huge POSITIVE product -> saturates to MAX.
        let c = Cost::line_item(i64::MIN, i64::MIN);
        assert!(c.overflow);
        assert_eq!(c.micros, i64::MAX);
    }

    #[test]
    fn boundary_operands_zero_and_extremes() {
        // A zero operand annihilates the product regardless of how extreme the
        // other operand is — no overflow, exact ZERO.
        assert_eq!(Cost::line_item(0, i64::MAX), Cost::ZERO);
        assert_eq!(Cost::line_item(i64::MAX, 0), Cost::ZERO);
        assert_eq!(Cost::line_item(0, i64::MIN), Cost::ZERO);
        assert_eq!(Cost::line_item(i64::MIN, 0), Cost::ZERO);

        // MICROS_PER_USD / TOKENS_PER_PRICE_UNIT is the "one whole unit" anchor:
        // a price of $1.00/1M tokens (1_000_000 micros) on exactly 1M tokens is
        // 1_000_000 micros = $1.00, exact, no rounding.
        assert_eq!(
            Cost::line_item(TOKENS_PER_PRICE_UNIT, MICROS_PER_USD),
            Cost::from_micros(MICROS_PER_USD)
        );

        // from_micros is a pure constructor: it carries the value verbatim and
        // never flags overflow, even at the i64 extremes.
        assert_eq!(Cost::from_micros(i64::MAX).micros, i64::MAX);
        assert!(!Cost::from_micros(i64::MAX).overflow);
        assert_eq!(Cost::from_micros(i64::MIN).micros, i64::MIN);
        assert!(!Cost::from_micros(i64::MIN).overflow);
    }

    #[test]
    fn saturating_add_clamps_and_flags_on_both_signs() {
        // MAX + MAX would be 2*i64::MAX -> clamp to i64::MAX with overflow.
        let hi = Cost::from_micros(i64::MAX).saturating_add(Cost::from_micros(i64::MAX));
        assert_eq!(hi.micros, i64::MAX);
        assert!(hi.overflow, "positive sum past i64::MAX must flag overflow");

        // MIN + MIN -> clamp to i64::MIN with overflow (the underflow side that
        // the existing stickiness test never exercises).
        let lo = Cost::from_micros(i64::MIN).saturating_add(Cost::from_micros(i64::MIN));
        assert_eq!(lo.micros, i64::MIN);
        assert!(
            lo.overflow,
            "negative sum past i64::MIN must flag underflow"
        );

        // MAX + MIN == -1 exactly and fits: no spurious overflow flag.
        let mixed = Cost::from_micros(i64::MAX).saturating_add(Cost::from_micros(i64::MIN));
        assert_eq!(mixed.micros, -1);
        assert!(!mixed.overflow, "an in-range sum must not flag overflow");
    }

    // --- property tests (TDD §10: proptest >= 256 cases) -----------------

    proptest! {
        #![proptest_config(ProptestConfig::with_cases(512))]

        /// Totality: no input panics, the result is deterministic, and it
        /// saturates with the overflow flag exactly when the rounded value
        /// leaves the i64 range.
        #[test]
        fn line_item_never_panics_and_saturates(tokens in any::<i64>(), price in any::<i64>()) {
            let c = Cost::line_item(tokens, price);
            // Independent reference using the same documented formula.
            let rounded = (i128::from(tokens) * i128::from(price) + 500_000) / 1_000_000;
            if rounded > i128::from(i64::MAX) {
                prop_assert!(c.overflow);
                prop_assert_eq!(c.micros, i64::MAX);
            } else if rounded < i128::from(i64::MIN) {
                prop_assert!(c.overflow);
                prop_assert_eq!(c.micros, i64::MIN);
            } else {
                prop_assert!(!c.overflow);
                prop_assert_eq!(i128::from(c.micros), rounded);
            }
            // Determinism.
            prop_assert_eq!(c, Cost::line_item(tokens, price));
        }

        /// Round-half-up correctness on the non-negative money domain, checked
        /// against an *independent* remainder-based rounding rule. Bounded so
        /// the product cannot overflow (1e9 * 1e8 = 1e17 < i64::MAX).
        #[test]
        fn line_item_rounds_half_up(
            tokens in 0i64..1_000_000_000,
            price in 0i64..100_000_000,
        ) {
            let c = Cost::line_item(tokens, price);
            prop_assert!(!c.overflow);
            let product = i128::from(tokens) * i128::from(price);
            let q = product / 1_000_000;
            let r = product % 1_000_000;
            // Half or above rounds up (r * 2 >= divisor); below rounds down.
            let expected = if r * 2 >= 1_000_000 { q + 1 } else { q };
            prop_assert_eq!(i128::from(c.micros), expected);
        }

        /// `saturating_add` is commutative for arbitrary costs.
        #[test]
        fn add_is_commutative(
            (t1, p1) in (0i64..i64::MAX, 0i64..i64::MAX),
            (t2, p2) in (0i64..i64::MAX, 0i64..i64::MAX),
        ) {
            let a = Cost::line_item(t1, p1);
            let b = Cost::line_item(t2, p2);
            prop_assert_eq!(a.saturating_add(b), b.saturating_add(a));
        }

        /// `saturating_add` is associative on the non-negative domain.
        /// Together with commutativity this makes any batch total independent
        /// of summation order — the "commutativity of batch order" invariant.
        #[test]
        fn add_is_associative_on_non_negative(
            (t1, p1) in (0i64..i64::MAX, 0i64..1_000_000i64),
            (t2, p2) in (0i64..i64::MAX, 0i64..1_000_000i64),
            (t3, p3) in (0i64..i64::MAX, 0i64..1_000_000i64),
        ) {
            let a = Cost::line_item(t1, p1);
            let b = Cost::line_item(t2, p2);
            let c = Cost::line_item(t3, p3);
            let left = a.saturating_add(b).saturating_add(c);
            let right = a.saturating_add(b.saturating_add(c));
            prop_assert_eq!(left, right);
        }

        /// A batch sum is identical forwards and reversed — the concrete
        /// statement of order independence a telemetry batch relies on.
        #[test]
        fn batch_sum_is_order_independent(
            items in proptest::collection::vec(
                (0i64..10_000_000i64, 0i64..50_000_000i64),
                0..64,
            ),
        ) {
            let forward = items
                .iter()
                .fold(Cost::ZERO, |acc, &(t, p)| acc.saturating_add(Cost::line_item(t, p)));
            let backward = items
                .iter()
                .rev()
                .fold(Cost::ZERO, |acc, &(t, p)| acc.saturating_add(Cost::line_item(t, p)));
            prop_assert_eq!(forward, backward);
        }

        /// Totality holds on the *negative* domain too: arbitrary negative
        /// inputs never panic and are deterministic. The non-negative-only
        /// correctness properties say nothing here, so this guards the
        /// out-of-domain promise the module docs make ("still returns a
        /// deterministic value and never panics").
        #[test]
        fn negative_domain_is_total_and_deterministic(
            tokens in i64::MIN..=0i64,
            price in any::<i64>(),
        ) {
            let c = Cost::line_item(tokens, price);
            prop_assert_eq!(c, Cost::line_item(tokens, price));
            // Saturation flag is consistent with the value being clamped to an
            // i64 extreme — never a silent wrap.
            if c.overflow {
                prop_assert!(c.micros == i64::MAX || c.micros == i64::MIN);
            }
        }

        /// Round-half-up correctness checked against a reference that does NOT
        /// reuse the impl's `(product + half) / divisor` shortcut. Instead it
        /// rounds via the exact rational comparison `2*r` vs `divisor` on the
        /// quotient/remainder pair — a structurally different algorithm — so a
        /// matching result is evidence of correctness, not a tautology. Spans
        /// the whole non-negative i64 token range (the price is bounded only so
        /// the product stays under saturation, where "the rounded value" is
        /// still well-defined).
        #[test]
        fn line_item_matches_independent_divmod_reference(
            tokens in 0i64..=i64::MAX,
            price in 0i64..=4_000i64,
        ) {
            let product = i128::from(tokens) * i128::from(price);
            let divisor = i128::from(TOKENS_PER_PRICE_UNIT);
            let q = product / divisor;
            let r = product - q * divisor; // exact remainder, 0 <= r < divisor
            let expected = if 2 * r >= divisor { q + 1 } else { q };
            let c = Cost::line_item(tokens, price);
            // Price ceiling keeps the product < ~9.2e21/... well under i64::MAX
            // after /1e6, so no saturation is expected in this slice.
            prop_assert!(!c.overflow);
            prop_assert_eq!(i128::from(c.micros), expected);
        }
    }
}
