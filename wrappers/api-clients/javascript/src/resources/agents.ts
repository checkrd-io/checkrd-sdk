/**
 * Agent resource — wraps `/v1/agents/*`.
 *
 * Mirrors OpenAI's ``client.fineTuning.jobs.list()`` pattern:
 * ``client.agents.list()`` returns a paginating async iterator;
 * ``retrieve()`` is the singular fetcher; mutations take typed
 * argument objects.
 *
 * Models come from the generator output under
 * ``_generated/types.gen.ts`` — generator is the source of truth
 * for field shapes; this file is the source of truth for the
 * ergonomic surface.
 */

import type { Checkrd, RequestOptions } from "../client.js";
import { Page, PagePromise } from "../pagination.js";
import type {
  Agent,
  CreateAgentRequest,
  DeleteResponse,
  KillSwitchRequest,
  RegisterPublicKeyRequest,
  RegisterPublicKeyResponse,
  UpdateAgentRequest,
} from "../_generated/types.gen.js";

export type {
  Agent,
  CreateAgentRequest,
  DeleteResponse,
  KillSwitchRequest,
  RegisterPublicKeyRequest,
  RegisterPublicKeyResponse,
  UpdateAgentRequest,
};

/** Query parameters accepted by ``Agents#list``. */
export interface AgentListParams {
  /** Maximum items per page (1-100, default 20). */
  limit?: number;
  /** Opaque cursor from a previous ``page.nextCursor``. */
  cursor?: string;
}

/**
 * Agent management. Reached via ``client.agents``.
 *
 * @example
 * ```ts
 * for await (const agent of client.agents.list()) {
 *   console.log(agent.name);
 * }
 *
 * const created = await client.agents.create({ name: "my-bot" });
 * await client.agents.toggleKillSwitch(created.id, { active: true });
 * ```
 */
export class Agents {
  constructor(private readonly client: Checkrd) {}

  /**
   * List agents in the caller's workspace.
   *
   * Returns a {@link PagePromise} that is awaitable to get the
   * first page and async-iterable to walk every page.
   */
  list(params: AgentListParams = {}, opts?: RequestOptions): PagePromise<Agent> {
    return new PagePromise(
      this.client._getApiList<Agent>(
        "/v1/agents",
        { limit: params.limit ?? 20, cursor: params.cursor },
        opts,
      ),
    );
  }

  /** Fetch a single agent by ID. */
  retrieve(agentId: string, opts?: RequestOptions): Promise<Agent> {
    return this.client._get<Agent>(`/v1/agents/${encodeURIComponent(agentId)}`, {}, opts);
  }

  /** Create an agent in the caller's workspace. */
  create(body: CreateAgentRequest, opts?: RequestOptions): Promise<Agent> {
    return this.client._post<Agent>("/v1/agents", { body }, opts);
  }

  /** Update an agent's name and/or description. Omitted fields are unchanged. */
  update(agentId: string, body: UpdateAgentRequest, opts?: RequestOptions): Promise<Agent> {
    return this.client._put<Agent>(`/v1/agents/${encodeURIComponent(agentId)}`, { body }, opts);
  }

  /** Soft-delete an agent. Requires the Admin role. */
  delete(agentId: string, opts?: RequestOptions): Promise<DeleteResponse> {
    return this.client._delete<DeleteResponse>(
      `/v1/agents/${encodeURIComponent(agentId)}`,
      {},
      opts,
    );
  }

  /**
   * Engage or release the kill switch.
   *
   * ``active=true`` causes every outbound call from the agent's
   * SDK to start denying within ~1 second. ``reason`` shows up in
   * the audit log.
   */
  toggleKillSwitch(
    agentId: string,
    body: KillSwitchRequest,
    opts?: RequestOptions,
  ): Promise<Agent> {
    return this.client._post<Agent>(
      `/v1/agents/${encodeURIComponent(agentId)}/kill-switch`,
      { body },
      opts,
    );
  }

  /**
   * Bind an Ed25519 public key to the agent. Idempotent — same
   * key is a no-op; different key returns 409.
   */
  registerPublicKey(
    agentId: string,
    body: RegisterPublicKeyRequest,
    opts?: RequestOptions,
  ): Promise<RegisterPublicKeyResponse> {
    return this.client._post<RegisterPublicKeyResponse>(
      `/v1/agents/${encodeURIComponent(agentId)}/public-key`,
      { body },
      opts,
    );
  }
}
