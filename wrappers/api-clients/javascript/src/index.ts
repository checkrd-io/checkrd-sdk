/**
 * `@checkrd/api` — Checkrd Control Plane API client.
 *
 * Single recommended entry point: the {@link Checkrd} class.
 * Mirrors the OpenAI / Anthropic / Stripe TS SDK shape.
 *
 * @example
 * ```ts
 * import { Checkrd } from "@checkrd/api";
 *
 * const client = new Checkrd({ apiKey: process.env.CHECKRD_API_KEY });
 * for await (const agent of client.agents.list()) {
 *   console.log(agent.name, agent.kill_switch_active);
 * }
 * ```
 */

export {
  Checkrd,
  DEFAULT_API_VERSION,
  DEFAULT_BASE_URL,
  DEFAULT_MAX_RETRIES,
  DEFAULT_TIMEOUT_MS,
} from "./client.js";
export type { CheckrdOptions, RequestOptions } from "./client.js";

export { Page, PagePromise } from "./pagination.js";
export type { PaginatedBody } from "./pagination.js";

export {
  APIConnectionError,
  APIError,
  APIStatusError,
  APITimeoutError,
  AuthenticationError,
  BadRequestError,
  CheckrdError,
  ConflictError,
  InternalServerError,
  NotFoundError,
  PermissionDeniedError,
  RateLimitError,
  UnprocessableEntityError,
} from "./errors.js";
export type { ErrorBody } from "./errors.js";

// Resource type re-exports — import models without reaching into
// the resource module yourself. ``import type { Agent } from
// "@checkrd/api"`` is the documented path; ``@checkrd/api/resources/
// agents`` is private.
export type {
  Agent,
  AgentListParams,
  CreateAgentRequest,
  DeleteResponse,
  KillSwitchRequest,
  RegisterPublicKeyRequest,
  RegisterPublicKeyResponse,
  UpdateAgentRequest,
} from "./resources/agents.js";

// Default export so callers can ``import Checkrd from "@checkrd/api"``
// (matches the OpenAI SDK convention).
import { Checkrd } from "./client.js";
export default Checkrd;
