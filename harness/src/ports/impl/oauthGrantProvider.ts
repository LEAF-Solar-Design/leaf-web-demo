/**
 * REAL OAuthGrantProvider - resolves ONE tenant's Agent SDK grant (Concern 2).
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
 * Wave 4 (multi-tenant): the grant is resolved from a PER-TENANT store keyed by
 * tenantId. `FileTenantGrantStore` persists one token file per tenant under
 * `$LEAF_GRANTS_DIR/<tenantId>.token` (default C:/tmp/leaf-grants). The token VALUE is
 * only ever returned inside the AgentGrant or written to its own file; it is NEVER
 * printed or logged here. A real deployment swaps the file store for a vault / DPAPI
 * secret store (same interface).
 */

import { existsSync, mkdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { DEFAULT_TENANT } from "../index.js";
import type {
  AgentGrant,
  GrantStatus,
  OAuthGrantProvider,
  TenantGrantAdminStore,
} from "../index.js";

/** The per-tenant grant store the OAuthGrantProviderImpl reads from. */
export interface TenantGrantStore {
  /** Return the tenant's stored grant, or null if the tenant has not linked one. */
  get(tenantId: string): Promise<AgentGrant | null>;
}

/**
 * Thrown when a tenant has no linked grant. Distinct type so the HTTP shell can map it
 * to a clean 401 with a `grant_required` marker (instead of an opaque 500), which the
 * app proxy surfaces to the frontend as GRANT_REQUIRED. Carries NO token.
 */
export class GrantRequiredError extends Error {
  readonly grantRequired = true;
  constructor(readonly tenantId: string, message?: string) {
    super(
      message ??
        `tenant ${tenantId} has no linked Claude grant - the user must "sign in with Claude" ` +
          `(per-user OAuth) or provide a BYO API key before the author loop can run.`,
    );
    this.name = "GrantRequiredError";
  }
}

/**
 * Concrete grant store for the demo/single-operator lane: resolves ONE OAuth grant
 * from the env var `CLAUDE_CODE_OAUTH_TOKEN`, else from the file named by env
 * `LEAF_GRANT_FILE` (default the proven hosted-oauth-spike grant path). This is the
 * operator's OWN 1-year subscription token (individual-use). The token VALUE is only
 * ever returned inside the AgentGrant; it is NEVER printed or logged here.
 *
 * In wave 4 this survives as the BACK-COMPAT fallback the per-tenant store consults for
 * the demo tenant only, so the proven single-tenant demo loop keeps working unchanged.
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

const DEFAULT_GRANTS_DIR = "C:/tmp/leaf-grants";

/**
 * Reject anything but a single, traversal-free path component so a crafted tenant id
 * can never escape `$LEAF_GRANTS_DIR`. Returns the `.token` filename.
 */
function grantFileName(tenantId: string): string {
  const tid = (tenantId ?? "").trim();
  const base = tid.replace(/\\/g, "/").split("/").pop() ?? "";
  if (!base || base === "." || base === ".." || base !== tid || !/^[A-Za-z0-9._-]+$/.test(base)) {
    throw new Error(`invalid tenant id for grant store: ${JSON.stringify(tenantId)}`);
  }
  return `${base}.token`;
}

export interface FileTenantGrantStoreOptions {
  /** Default: env LEAF_GRANTS_DIR, then C:/tmp/leaf-grants. */
  dir?: string;
  /** Demo-tenant back-compat fallback (default EnvOrFileGrantStore). */
  envFallback?: TenantGrantStore;
  /** Which tenant id gets the env fallback (default DEFAULT_TENANT). */
  defaultTenant?: string;
}

/**
 * PER-TENANT file grant store (wave 4). One file per tenant:
 * `$LEAF_GRANTS_DIR/<tenantId>.token`. Implements both the read side
 * (`TenantGrantStore.get`, used by OAuthGrantProviderImpl) and the admin side
 * (`TenantGrantAdminStore`, backing the harness /grants endpoints).
 *
 * Secret discipline: the token is written to / read from its own file and returned only
 * inside an AgentGrant. `status()` NEVER reads-and-returns the token — only linked +
 * mtime. Nothing here logs the token. `linked_at` is the token file's mtime.
 *
 * BACK-COMPAT: if the demo tenant has no per-tenant file, `get()`/`status()` fall back
 * to the env/file grant (EnvOrFileGrantStore), so the proven demo loop is unchanged.
 * PRODUCTION swaps this class for a vault/DPAPI-backed store (same interface).
 */
export class FileTenantGrantStore implements TenantGrantStore, TenantGrantAdminStore {
  private readonly dir: string;
  private readonly envFallback: TenantGrantStore;
  private readonly defaultTenant: string;

  constructor(opts: FileTenantGrantStoreOptions = {}) {
    this.dir = opts.dir ?? process.env.LEAF_GRANTS_DIR ?? DEFAULT_GRANTS_DIR;
    this.envFallback = opts.envFallback ?? new EnvOrFileGrantStore();
    this.defaultTenant = opts.defaultTenant ?? DEFAULT_TENANT;
  }

  private file(tenantId: string): string {
    return join(this.dir, grantFileName(tenantId));
  }

  private readToken(tenantId: string): string | null {
    const p = this.file(tenantId);
    if (!existsSync(p)) return null;
    const raw = readFileSync(p, "utf8").trim();
    return raw ? raw : null;
  }

  async get(tenantId: string): Promise<AgentGrant | null> {
    const tok = this.readToken(tenantId);
    if (tok) return { kind: "oauth", oauthToken: tok };
    if (tenantId === this.defaultTenant) return this.envFallback.get(tenantId);
    return null;
  }

  async put(tenantId: string, token: string): Promise<GrantStatus> {
    const t = (token ?? "").trim();
    mkdirSync(this.dir, { recursive: true });
    // 0600 where the platform honors it; content is the raw token and is NEVER logged.
    writeFileSync(this.file(tenantId), t, { encoding: "utf8", mode: 0o600 });
    return this.status(tenantId);
  }

  async status(tenantId: string): Promise<GrantStatus> {
    const p = this.file(tenantId);
    if (existsSync(p)) {
      const raw = readFileSync(p, "utf8").trim();
      if (raw) {
        return { linked: true, linked_at: statSync(p).mtime.toISOString() };
      }
    }
    if (tenantId === this.defaultTenant) {
      const g = await this.envFallback.get(tenantId);
      if (g) return { linked: true, linked_at: null };
    }
    return { linked: false, linked_at: null };
  }

  async remove(tenantId: string): Promise<void> {
    rmSync(this.file(tenantId), { force: true });
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

    throw new GrantRequiredError(tenantId);
  }
}
