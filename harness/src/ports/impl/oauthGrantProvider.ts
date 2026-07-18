/**
 * REAL OAuthGrantProvider - resolves ONE tenant's Agent SDK grant (Concern 2).
 * Live path is operator-gated (needs the per-tenant token store the
 * hosted-oauth-spike lane lands). It COMPILES now and states the corrected reality:
 *
 *   - Web lane: the tenant's OWN "sign in with Claude" OAuth token (individual-use;
 *     one per end user, NEVER pooled - research/agentsdk-usage-visibility.md). The
 *     token draws on that user's subscription rate windows; there is no balance API.
 *   - Enterprise lane: a BYO API key.
 *
 * INVARIANT (contract/AUTH.md section 0): this NEVER touches the Auth0 platform JWT.
 * The tenant JWT answers "which workspace"; this answers "whose Anthropic credit" -
 * two different concerns with two different cardinalities. They must never mingle.
 *
 * This stub reads from an injected per-tenant token store abstraction; a real store
 * (encrypted-at-rest, per-tenant) is the sibling lane's deliverable.
 */

import { existsSync, readFileSync } from "node:fs";
import type { AgentGrant, OAuthGrantProvider } from "../index.js";

/** The per-tenant grant store the hosted-oauth-spike lane will provide. */
export interface TenantGrantStore {
  /** Return the tenant's stored grant, or null if the tenant has not linked one. */
  get(tenantId: string): Promise<AgentGrant | null>;
}

/**
 * Concrete grant store for the demo/single-operator lane: resolves ONE OAuth grant
 * from the env var `CLAUDE_CODE_OAUTH_TOKEN`, else from the file named by env
 * `LEAF_GRANT_FILE` (default the proven hosted-oauth-spike grant path). This is the
 * operator's OWN 1-year subscription token (individual-use). The token VALUE is only
 * ever returned inside the AgentGrant; it is NEVER printed or logged here.
 *
 * NOTE: a real multi-tenant deployment replaces this with a per-tenant, encrypted-
 * at-rest store keyed by tenantId (each user's OWN "sign in with Claude" token). A
 * single operator token serving many tenants is the individual-use / anti-bridging
 * violation the research doc flags; this store is a single-operator demo convenience.
 */
export interface EnvOrFileGrantStoreOptions {
  /** Default: env LEAF_GRANT_FILE, then C:/tmp/hosted-oauth-spike/.grant/token. */
  grantFile?: string;
}

const DEFAULT_GRANT_FILE = "C:/tmp/hosted-oauth-spike/.grant/token";

export class EnvOrFileGrantStore implements TenantGrantStore {
  private readonly grantFile: string;
  constructor(opts: EnvOrFileGrantStoreOptions = {}) {
    this.grantFile = opts.grantFile ?? process.env.LEAF_GRANT_FILE ?? DEFAULT_GRANT_FILE;
  }

  async get(_tenantId: string): Promise<AgentGrant | null> {
    const fromEnv = process.env.CLAUDE_CODE_OAUTH_TOKEN;
    if (fromEnv && fromEnv.trim()) {
      return { kind: "oauth", oauthToken: fromEnv.trim() };
    }
    if (existsSync(this.grantFile)) {
      const fromFile = readFileSync(this.grantFile, "utf8").trim();
      if (fromFile) return { kind: "oauth", oauthToken: fromFile };
    }
    return null;
  }
}

export interface OAuthGrantProviderOptions {
  store: TenantGrantStore;
  /**
   * Optional enterprise fallback: a BYO API key resolver (e.g. from tenant config).
   * NEVER a shared operator subscription token - that is the individual-use / anti-
   * bridging violation the research doc flags.
   */
  enterpriseApiKey?: (tenantId: string) => Promise<string | null>;
}

export class OAuthGrantProviderImpl implements OAuthGrantProvider {
  constructor(private readonly opts: OAuthGrantProviderOptions) {}

  async getGrant(tenantId: string): Promise<AgentGrant> {
    const linked = await this.opts.store.get(tenantId);
    if (linked) return linked;

    if (this.opts.enterpriseApiKey) {
      const key = await this.opts.enterpriseApiKey(tenantId);
      if (key) return { kind: "api_key", apiKey: key };
    }

    throw new Error(
      `tenant ${tenantId} has no linked Claude grant - the user must "sign in with Claude" ` +
        `(per-user OAuth) or provide a BYO API key before the author loop can run.`,
    );
  }
}
