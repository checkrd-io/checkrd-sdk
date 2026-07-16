/**
 * Shared RFC 9457 `application/problem+json` body for policy-deny
 * responses across the framework adapters (Next.js, Hono, Cloudflare
 * Workers). One builder so every adapter emits the byte-identical shape
 * — and the same one the control plane returns (`crates/api/src/errors.rs`).
 *
 * The deny envelope used to be the nested, non-standard
 * `{"error":{"type":"policy_denied",…}}`. It is now flat problem+json:
 * standard members (type/title/status/detail) first, then extension
 * members (`code`, `request_id`, `dashboard_url`, and `rule_name` /
 * `suggestion` when present).
 */

import type { CheckrdPolicyDenied } from "../exceptions.js";

/** Media type for RFC 9457 problem details. Set as the response Content-Type. */
export const PROBLEM_JSON_CONTENT_TYPE = "application/problem+json";

/**
 * RFC 9457 problem-details body for a policy deny. `type` resolves to
 * `…/errors/policy_denied` (errors.mdx#policy_denied + the /errors/[code]
 * redirect), so it is link-check-safe.
 *
 * `dashboard_url` is kept even when null (matches the prior envelope —
 * clients branch on its presence). `rule_name` / `suggestion` are
 * extension members, omitted when the deny carries no such detail.
 */
export interface PolicyDeniedProblem {
  type: string;
  title: string;
  status: number;
  detail: string;
  code: string;
  request_id: string;
  dashboard_url: string | null;
  rule_name?: string;
  suggestion?: string;
}

/** The deny `type` URI. Single literal so every adapter agrees. */
const POLICY_DENIED_TYPE = "https://checkrd.io/errors/policy_denied";

/**
 * Build the flat RFC 9457 body for a {@link CheckrdPolicyDenied}.
 * Standard members are inserted first, then extension members, so the
 * serialized JSON key order matches the documented shape.
 */
export function policyDeniedProblem(
  err: CheckrdPolicyDenied,
): PolicyDeniedProblem {
  const body: PolicyDeniedProblem = {
    type: POLICY_DENIED_TYPE,
    title: "Request denied by policy",
    status: 403,
    detail: err.reason,
    code: "policy_denied",
    request_id: err.requestId,
    dashboard_url: err.dashboardUrl ?? null,
  };
  if (err.ruleName !== undefined) body.rule_name = err.ruleName;
  if (err.suggestion !== undefined) body.suggestion = err.suggestion;
  return body;
}
