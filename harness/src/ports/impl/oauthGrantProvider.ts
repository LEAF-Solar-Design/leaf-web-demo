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

import { chmodSync, existsSync, mkdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { DEFAULT_TENANT } from "../index.js";
import type {
  AgentGrant,
  GrantKind,
  GrantStatus,
  OAuthGrantProvider,
  TenantGrantAdminStore,
} from "../index.js";

/**
 * Auto-detect a grant's credential kind from the token PREFIX (CONTRACT-ADDENDUM §17):
 *   `sk-ant-api…` → api_key (enterprise BYO key)
 *   `sk-ant-oat…` → oauth   ("sign in with Claude" per-user token)
 *   anything else → oauth   (the web-lane default; the proven demo token has no fixed
 *                            public prefix, so oauth is the safe default).
 * The token VALUE is never logged here — only its leading marker informs the kind.
 */
export function detectGrantKind(token: string): GrantKind {
  const t = (token ?? "").trim();
  if (t.startsWith("sk-ant-api")) return "api_key";
  if (t.startsWith("sk-ant-oat")) return "oauth";
  return "oauth";
}

/** Build the AgentGrant of a given kind from a raw token value. */
function grantFor(kind: GrantKind, token: string): AgentGrant {
  return kind === "api_key" ? { kind: "api_key", apiKey: token } : { kind: "oauth", oauthToken: token };
}

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
 * can never escape `$LEAF_GRANTS_DIR`. Returns the safe base name.
 *
 * F13 (security-audit 2026-07-18): the accepted charset MIRRORS the ONE shared
 * tenant-id rule in server/tenant_id_validator.py — `^[a-z0-9][a-z0-9_-]{0,62}$`,
 * REJECT-don't-collapse — so the TS grant store, the Python store keys, and
 * tenant_paths all AGREE on which ids are legal. (Was `^[A-Za-z0-9._-]+$`, which
 * accepted uppercase/dots the Python side would fold or reject, causing the three
 * validators to disagree.) `demo-tenant` and real Auth0 ids like `org_acme_solar`
 * stay valid; the leading-alphanumeric requirement subsumes the old `.`/`..` guards.
 */
const TENANT_ID_RE = /^[a-z0-9][a-z0-9_-]{0,62}$/;

function safeBase(tenantId: string): string {
  const tid = (tenantId ?? "").trim();
  const base = tid.replace(/\\/g, "/").split("/").pop() ?? "";
  if (!base || base !== tid || !TENANT_ID_RE.test(base)) {
    throw new Error(`invalid tenant id for grant store: ${JSON.stringify(tenantId)}`);
  }
  return base;
}

function grantFileName(tenantId: string): string {
  return `${safeBase(tenantId)}.token`;
}

/** Sidecar file recording the grant KIND (never the token). */
function grantKindFileName(tenantId: string): string {
  return `${safeBase(tenantId)}.kind`;
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
 * mtime + kind. Nothing here logs the token. `linked_at` is the token file's mtime.
 *
 * Grant KIND (§17): the credential kind (`oauth` per-user token vs `api_key` enterprise
 * BYO key) is persisted in a sidecar `<tid>.kind` file. On `put`, an omitted kind is
 * AUTO-DETECTED from the token prefix. On `get`, an api_key grant surfaces as
 * `{kind:"api_key", apiKey}` (→ `ANTHROPIC_API_KEY`); an oauth grant as
 * `{kind:"oauth", oauthToken}` (→ `CLAUDE_CODE_OAUTH_TOKEN`). A legacy token file with
 * no sidecar falls back to prefix auto-detection (so pre-§17 files keep working).
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

  private kindFile(tenantId: string): string {
    return join(this.dir, grantKindFileName(tenantId));
  }

  private readToken(tenantId: string): string | null {
    const p = this.file(tenantId);
    if (!existsSync(p)) return null;
    const raw = readFileSync(p, "utf8").trim();
    return raw ? raw : null;
  }

  /** The persisted grant kind for a tenant (sidecar), or null when no sidecar exists. */
  private readKind(tenantId: string): GrantKind | null {
    const p = this.kindFile(tenantId);
    if (!existsSync(p)) return null;
    const raw = readFileSync(p, "utf8").trim();
    return raw === "api_key" || raw === "oauth" ? raw : null;
  }

  async get(tenantId: string): Promise<AgentGrant | null> {
    const tok = this.readToken(tenantId);
    if (tok) {
      // Sidecar kind wins; a legacy file with no sidecar falls back to prefix detection.
      const kind = this.readKind(tenantId) ?? detectGrantKind(tok);
      return grantFor(kind, tok);
    }
    if (tenantId === this.defaultTenant) return this.envFallback.get(tenantId);
    return null;
  }

  async put(tenantId: string, token: string, kind?: GrantKind): Promise<GrantStatus> {
    const t = (token ?? "").trim();
    const k: GrantKind = kind ?? detectGrantKind(t);
    mkdirSync(this.dir, { recursive: true });
    // 0600 where the platform honors it; content is the raw token and is NEVER logged.
    writeFileSync(this.file(tenantId), t, { encoding: "utf8", mode: 0o600 });
    writeFileSync(this.kindFile(tenantId), k, { encoding: "utf8", mode: 0o600 });
    // `mode:` applies only at CREATION — an overwrite keeps the file's old bits
    // (sol-critic F7). Tighten explicitly on every write; no-op on Windows,
    // load-bearing on the Linux container volume.
    chmodSync(this.file(tenantId), 0o600);
    chmodSync(this.kindFile(tenantId), 0o600);
    return this.status(tenantId);
  }

  async status(tenantId: string): Promise<GrantStatus> {
    const p = this.file(tenantId);
    if (existsSync(p)) {
      const raw = readFileSync(p, "utf8").trim();
      if (raw) {
        const kind = this.readKind(tenantId) ?? detectGrantKind(raw);
        return { linked: true, linked_at: statSync(p).mtime.toISOString(), kind };
      }
    }
    if (tenantId === this.defaultTenant) {
      const g = await this.envFallback.get(tenantId);
      if (g) return { linked: true, linked_at: null, kind: g.kind };
    }
    return { linked: false, linked_at: null };
  }

  async remove(tenantId: string): Promise<void> {
    rmSync(this.file(tenantId), { force: true });
    rmSync(this.kindFile(tenantId), { force: true });
  }
}

/**
 * GRANT-STORE SEAM (F18, security-audit 2026-07-18) — the backing store for grants at
 * rest is selectable via env `LEAF_GRANT_STORE`, so a production deployment can swap the
 * on-disk file store for a sealed secret store WITHOUT touching call sites:
 *
 *   - `file`  (DEFAULT): `FileTenantGrantStore` — one mode-0600 token file per tenant
 *             under `$LEAF_GRANTS_DIR`. The demo / single-operator default.
 *   - `vault`: a production secret store (HashiCorp Vault / AWS Secrets Manager /
 *             Windows DPAPI). NOT IMPLEMENTED here — this is the documented seam.
 *             Implement `TenantGrantStore & TenantGrantAdminStore` against your vault
 *             and return it from the `case "vault"` branch below.
 *
 * WHY a seam and not just the file store: a token on local disk is not a production
 * secret boundary even at mode 0600 (any process running as the service user can read
 * it, and it survives on the volume). The seam lets ops opt into a sealed backend and
 * fails LOUDLY if `vault` is requested but unwired, rather than silently persisting
 * tokens to disk when the operator believed they were sealed.
 */
export type GrantStoreBackend = "file" | "vault";

/** The configured grant-store backend (env `LEAF_GRANT_STORE`, default `file`). */
export function resolveGrantStoreBackend(): GrantStoreBackend {
  return (process.env.LEAF_GRANT_STORE ?? "file").trim().toLowerCase() === "vault" ? "vault" : "file";
}

/**
 * Construct the tenant grant store for the configured backend. Default `file` yields the
 * mode-0600 `FileTenantGrantStore`; `vault` is the documented-but-unwired production seam.
 */
export function createTenantGrantStore(
  opts: FileTenantGrantStoreOptions = {},
): TenantGrantStore & TenantGrantAdminStore {
  const backend = resolveGrantStoreBackend();
  switch (backend) {
    case "vault":
      // SEAM: wire a real vault / DPAPI-backed `TenantGrantStore & TenantGrantAdminStore`
      // here for production. Intentionally unimplemented — fail loudly rather than fall
      // back to on-disk tokens when an operator explicitly asked for the sealed store.
      throw new Error(
        "LEAF_GRANT_STORE=vault is a documented seam, not yet implemented — provide a " +
          "vault/DPAPI-backed TenantGrantStore (see F18 in docs/security-audit-2026-07-18.md).",
      );
    case "file":
    default:
      return new FileTenantGrantStore(opts);
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
