/**
 * REAL OAuthGrantProvider - resolves ONE tenant's Agent SDK grant (Concern 2).
 *
 *   - Subscription lane: tenant-owner mounted Pro, Max, Team, or Enterprise Claude
 *     credentials, routed only inside that tenant.
 *   - API lane: a tenant-owned Anthropic API key.
 *
 * INVARIANT (contract/AUTH.md section 0): this NEVER touches the Auth0 platform JWT.
 * The tenant JWT answers "which workspace"; this answers "whose Anthropic credit" -
 * two different concerns with two different cardinalities. They must never mingle.
 *
 * Wave 4 (multi-tenant): the grant is resolved from a PER-TENANT store keyed by
 * tenantId. `FileTenantGrantStore` persists one private atomic record per tenant under
 * `$LEAF_GRANTS_DIR/<tenantId>.grant.json` (default C:/tmp/leaf-grants). The token VALUE is
 * only ever returned inside the AgentGrant or written to that record; it is NEVER
 * printed or logged here. A real deployment swaps the file store for a vault / DPAPI
 * secret store (same interface).
 */

import { randomUUID } from "node:crypto";
import {
  closeSync,
  existsSync,
  fsyncSync,
  lstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { dirname, join } from "node:path";
import { DEFAULT_TENANT } from "../index.js";
import type {
  AgentGrant,
  GrantDiagnostic,
  GrantKind,
  GrantLease,
  GrantPlan,
  GrantSettlement,
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
  acquire?(tenantId: string): Promise<GrantLease>;
  settle?(tenantId: string, leaseId: string, outcome: GrantSettlement): Promise<void>;
}

/**
 * Thrown when a tenant has no eligible linked grant. Distinct type so the HTTP shell can map it
 * to a clean 401 with a `grant_required` marker (instead of an opaque 500), which the
 * app proxy surfaces to the frontend as GRANT_REQUIRED. Carries NO token.
 */
export class GrantRequiredError extends Error {
  readonly grantRequired = true;
  constructor(readonly tenantId: string, message?: string) {
    super(
      message ??
        `tenant ${tenantId} has no eligible Claude grant. The tenant owner must mount ` +
          `an attested Claude subscription credential, or a tenant-owned Anthropic API key.`,
    );
    this.name = "GrantRequiredError";
  }
}

/**
 * Concrete grant store for the legacy demo/single-operator lane: resolves ONE OAuth grant
 * from the env var `CLAUDE_CODE_OAUTH_TOKEN`, else from the file named by env
 * `LEAF_GRANT_FILE` (default the proven hosted-oauth-spike grant path). This is the
 * legacy operator token. The token VALUE is only
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

function grantRecordFileName(tenantId: string): string {
  return `${safeBase(tenantId)}.grant.json`;
}

interface PersistedGrantRecordV1 {
  version: 1;
  kind: GrantKind;
  token: string;
}

interface PersistedGrantAccount {
  id: string;
  label: string;
  kind: GrantKind;
  token: string;
  linked_at: string;
}

export class GrantPoolUnavailableError extends Error {
  readonly retryAfterS: number;
  constructor(retryAfterS: number) {
    super("all authorized Claude mounts are temporarily unavailable");
    this.name = "GrantPoolUnavailableError";
    this.retryAfterS = retryAfterS;
  }
}

interface PersistedGrantRecordV2 {
  version: 2;
  active_account_id: string;
  accounts: PersistedGrantAccount[];
}

interface PersistedGrantLease {
  id: string;
  expires_at: string;
}

interface PersistedGrantAccountV3 extends PersistedGrantAccount {
  plan: GrantPlan | null;
  usage_tokens: number;
  selection_count: number;
  last_used_at: string | null;
  cooldown_until: string | null;
  leases: PersistedGrantLease[];
}

interface PersistedGrantRecordV3 {
  version: 3;
  active_account_id: string;
  accounts: PersistedGrantAccountV3[];
}

type PersistedGrantRecord = PersistedGrantRecordV1 | PersistedGrantRecordV2 | PersistedGrantRecordV3;

const LEASE_TTL_MS = 5 * 60 * 1000;
const LEASE_RESERVATION_TOKENS = 100_000;
const ELIGIBLE_OAUTH_PLANS: ReadonlySet<GrantPlan> = new Set(["pro", "max", "team", "enterprise"]);
const DEFAULT_QUOTA_COOLDOWN_S = 15 * 60;

function cleanLabel(label: string | undefined, kind: GrantKind): string {
  const value = (label ?? "").trim();
  if (value.length > 120) throw new Error("grant account label must be at most 120 characters");
  return value || (kind === "api_key" ? "Anthropic API key" : "Claude subscription");
}

function writePrivateFileAtomic(path: string, content: string): void {
  const tmp = `${path}.${process.pid}.${randomUUID()}.tmp`;
  let fd: number | null = null;
  try {
    fd = openSync(tmp, "wx", 0o600);
    writeFileSync(fd, content, "utf8");
    fsyncSync(fd);
    closeSync(fd);
    fd = null;
    renameSync(tmp, path);
    try {
      const dirFd = openSync(dirname(path), "r");
      try {
        fsyncSync(dirFd);
      } finally {
        closeSync(dirFd);
      }
    } catch {
      // Windows does not permit opening directories for fsync.
    }
  } finally {
    if (fd !== null) closeSync(fd);
    rmSync(tmp, { force: true });
  }
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
 * PER-TENANT file grant store (wave 4). New writes use one atomic
 * `$LEAF_GRANTS_DIR/<tenantId>.grant.json` record. Implements both the read side
 * (`TenantGrantStore.get`, used by OAuthGrantProviderImpl) and the admin side
 * (`TenantGrantAdminStore`, backing the harness /grants endpoints).
 *
 * Secret discipline: the token is written to / read from a private record and returned only
 * inside an AgentGrant. `status()` NEVER reads-and-returns the token — only linked +
 * mtime + kind. Nothing here logs the token. `linked_at` is the active record's mtime.
 *
 * Grant KIND (§17): the credential kind (`oauth` per-user token vs `api_key` enterprise
 * BYO key) is persisted with the token in the atomic record. On `put`, an omitted kind is
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

  private recordFile(tenantId: string): string {
    return join(this.dir, grantRecordFileName(tenantId));
  }

  private readRecord(tenantId: string): PersistedGrantRecord | null {
    const p = this.recordFile(tenantId);
    if (!existsSync(p)) return null;
    let raw: unknown;
    try {
      raw = JSON.parse(readFileSync(p, "utf8"));
    } catch {
      throw new Error(`invalid persisted grant record for tenant ${JSON.stringify(tenantId)}`);
    }
    const candidate = raw as Record<string, unknown>;
    if (typeof raw !== "object" || raw === null) {
      throw new Error(`invalid persisted grant record for tenant ${JSON.stringify(tenantId)}`);
    }
    if (candidate.version === 1) {
      if (
        (candidate.kind !== "oauth" && candidate.kind !== "api_key") ||
        typeof candidate.token !== "string" ||
        !candidate.token.trim()
      ) throw new Error(`invalid persisted grant record for tenant ${JSON.stringify(tenantId)}`);
      return { version: 1, kind: candidate.kind, token: candidate.token.trim() };
    }
    if (candidate.version === 2 && Array.isArray(candidate.accounts)) {
      const accounts = candidate.accounts as Array<Record<string, unknown>>;
      const valid = accounts.length > 0 && accounts.every((account) =>
        typeof account.id === "string" && !!account.id &&
        typeof account.label === "string" && account.label.length <= 120 &&
        (account.kind === "oauth" || account.kind === "api_key") &&
        typeof account.token === "string" && !!account.token.trim() &&
        typeof account.linked_at === "string" && !Number.isNaN(Date.parse(account.linked_at))
      );
      const ids = accounts.map((account) => account.id as string);
      if (!valid || new Set(ids).size !== ids.length ||
          typeof candidate.active_account_id !== "string" ||
          !ids.includes(candidate.active_account_id)) {
        throw new Error(`invalid persisted grant record for tenant ${JSON.stringify(tenantId)}`);
      }
      return {
        version: 2,
        active_account_id: candidate.active_account_id,
        accounts: accounts.map((account) => ({
          id: account.id as string,
          label: account.label as string,
          kind: account.kind as GrantKind,
          token: (account.token as string).trim(),
          linked_at: account.linked_at as string,
        })),
      };
    }
    if (candidate.version === 3 && Array.isArray(candidate.accounts)) {
      const accounts = candidate.accounts as Array<Record<string, unknown>>;
      const valid = accounts.length > 0 && accounts.every((account) => {
        const leases = account.leases;
        return typeof account.id === "string" && !!account.id &&
          typeof account.label === "string" && account.label.length <= 120 &&
          (account.kind === "oauth" || account.kind === "api_key") &&
          typeof account.token === "string" && !!account.token.trim() &&
          typeof account.linked_at === "string" && !Number.isNaN(Date.parse(account.linked_at)) &&
          (account.plan === null || ELIGIBLE_OAUTH_PLANS.has(account.plan as GrantPlan)) &&
          Number.isSafeInteger(account.usage_tokens) && (account.usage_tokens as number) >= 0 &&
          Number.isSafeInteger(account.selection_count) && (account.selection_count as number) >= 0 &&
          (account.last_used_at === null ||
            (typeof account.last_used_at === "string" && !Number.isNaN(Date.parse(account.last_used_at)))) &&
          (account.cooldown_until === null ||
            (typeof account.cooldown_until === "string" && !Number.isNaN(Date.parse(account.cooldown_until)))) &&
          Array.isArray(leases) && leases.every((lease) => {
            const item = lease as Record<string, unknown>;
            return typeof item.id === "string" && !!item.id &&
              typeof item.expires_at === "string" && !Number.isNaN(Date.parse(item.expires_at));
          });
      });
      const ids = accounts.map((account) => account.id as string);
      if (!valid || new Set(ids).size !== ids.length ||
          typeof candidate.active_account_id !== "string" ||
          !ids.includes(candidate.active_account_id)) {
        throw new Error(`invalid persisted grant record for tenant ${JSON.stringify(tenantId)}`);
      }
      return {
        version: 3,
        active_account_id: candidate.active_account_id,
        accounts: accounts.map((account) => ({
          id: account.id as string,
          label: account.label as string,
          kind: account.kind as GrantKind,
          token: (account.token as string).trim(),
          linked_at: account.linked_at as string,
          plan: account.plan as GrantPlan | null,
          usage_tokens: account.usage_tokens as number,
          selection_count: account.selection_count as number,
          last_used_at: account.last_used_at as string | null,
          cooldown_until: account.cooldown_until as string | null,
          leases: (account.leases as Array<Record<string, unknown>>).map((lease) => ({
            id: lease.id as string,
            expires_at: lease.expires_at as string,
          })),
        })),
      };
    }
    throw new Error(`invalid persisted grant record for tenant ${JSON.stringify(tenantId)}`);
  }

  private asV3(tenantId: string, record: PersistedGrantRecord): PersistedGrantRecordV3 {
    if (record.version === 3) return record;
    const linkedAt = record.version === 1
      ? statSync(this.recordFile(tenantId)).mtime.toISOString()
      : null;
    const accounts = record.version === 1
      ? [{
          id: "legacy",
          label: cleanLabel(undefined, record.kind),
          kind: record.kind,
          token: record.token,
          linked_at: linkedAt!,
        }]
      : record.accounts;
    return {
      version: 3,
      active_account_id: record.version === 1 ? "legacy" : record.active_account_id,
      accounts: accounts.map((account) => ({
        ...account,
        plan: null,
        usage_tokens: 0,
        selection_count: 0,
        last_used_at: null,
        cooldown_until: null,
        leases: [],
      })),
    };
  }

  private writeRecord(tenantId: string, record: PersistedGrantRecordV3): void {
    writePrivateFileAtomic(this.recordFile(tenantId), JSON.stringify(record) + "\n");
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
    const record = this.readRecord(tenantId);
    if (record) {
      if (record.version === 1) return grantFor(record.kind, record.token);
      const active = record.accounts.find((account) => account.id === record.active_account_id)!;
      return grantFor(active.kind, active.token);
    }

    const tok = this.readToken(tenantId);
    if (tok) {
      // Sidecar kind wins; a legacy file with no sidecar falls back to prefix detection.
      const kind = this.readKind(tenantId) ?? detectGrantKind(tok);
      return grantFor(kind, tok);
    }
    if (tenantId === this.defaultTenant) return this.envFallback.get(tenantId);
    return null;
  }

  async put(tenantId: string, token: string, kind?: GrantKind, label?: string, plan?: GrantPlan): Promise<GrantStatus> {
    const t = (token ?? "").trim();
    if (!t) throw new Error("grant token must not be empty");
    const k: GrantKind = kind ?? detectGrantKind(t);
    mkdirSync(this.dir, { recursive: true });
    const existing = this.readRecord(tenantId);
    const record = existing ? this.asV3(tenantId, existing) : {
      version: 3 as const,
      active_account_id: "",
      accounts: [],
    };
    const id = randomUUID();
    record.accounts.push({
      id,
      label: cleanLabel(label, k),
      kind: k,
      token: t,
      linked_at: new Date().toISOString(),
      plan: k === "oauth" ? plan ?? null : null,
      usage_tokens: 0,
      selection_count: 0,
      last_used_at: null,
      cooldown_until: null,
      leases: [],
    });
    record.active_account_id = id;
    this.writeRecord(tenantId, record);
    return this.status(tenantId);
  }

  async activate(tenantId: string, accountId: string): Promise<GrantStatus> {
    const existing = this.readRecord(tenantId);
    if (!existing) throw new Error("grant account not found");
    const record = this.asV3(tenantId, existing);
    if (!record.accounts.some((account) => account.id === accountId)) {
      throw new Error("grant account not found");
    }
    record.active_account_id = accountId;
    this.writeRecord(tenantId, record);
    return this.status(tenantId);
  }

  async acquire(tenantId: string): Promise<GrantLease> {
    const existing = this.readRecord(tenantId);
    if (!existing) throw new GrantRequiredError(tenantId);
    const record = this.asV3(tenantId, existing);
    const now = Date.now();
    let changed = existing.version !== 3;
    for (const account of record.accounts) {
      const leases = account.leases.filter((lease) => Date.parse(lease.expires_at) > now);
      if (leases.length !== account.leases.length) changed = true;
      account.leases = leases;
    }
    const attested = record.accounts.filter(
      (account) => account.kind === "api_key" ||
        (account.plan !== null && ELIGIBLE_OAUTH_PLANS.has(account.plan)),
    );
    if (!attested.length) {
      if (changed) this.writeRecord(tenantId, record);
      throw new GrantRequiredError(
        tenantId,
        `tenant ${tenantId} must mount an attested Claude subscription or API key before live conversation`,
      );
    }
    const eligible = attested.filter(
      (account) => account.cooldown_until === null || Date.parse(account.cooldown_until) <= now,
    );
    if (!eligible.length) {
      if (changed) this.writeRecord(tenantId, record);
      const next = Math.min(...attested
        .map((account) => account.cooldown_until ? Date.parse(account.cooldown_until) : now)
        .filter(Number.isFinite));
      throw new GrantPoolUnavailableError(Math.max(1, Math.ceil((next - now) / 1000)));
    }
    eligible.sort((a, b) => {
      const aScore = a.usage_tokens + a.leases.length * LEASE_RESERVATION_TOKENS;
      const bScore = b.usage_tokens + b.leases.length * LEASE_RESERVATION_TOKENS;
      return aScore - bScore || a.selection_count - b.selection_count ||
        a.linked_at.localeCompare(b.linked_at) || a.id.localeCompare(b.id);
    });
    const selected = eligible[0]!;
    const leaseId = randomUUID();
    selected.leases.push({ id: leaseId, expires_at: new Date(now + LEASE_TTL_MS).toISOString() });
    selected.selection_count += 1;
    selected.last_used_at = new Date(now).toISOString();
    record.active_account_id = selected.id;
    this.writeRecord(tenantId, record);
    return {
      grant: grantFor(selected.kind, selected.token),
      account_id: selected.id,
      lease_id: leaseId,
    };
  }

  async settle(tenantId: string, leaseId: string, outcome: GrantSettlement): Promise<void> {
    const existing = this.readRecord(tenantId);
    if (!existing) return;
    const record = this.asV3(tenantId, existing);
    const account = record.accounts.find((candidate) =>
      candidate.leases.some((lease) => lease.id === leaseId));
    if (!account) return; // idempotent settlement or an expired crash lease
    account.leases = account.leases.filter((lease) => lease.id !== leaseId);
    const costTokens = Number.isSafeInteger(outcome.usage.cost_tokens) && outcome.usage.cost_tokens > 0
      ? outcome.usage.cost_tokens
      : 0;
    account.usage_tokens += costTokens;
    if (outcome.stop_reason === "llm_quota_exhausted") {
      const retryAfterS = Number.isFinite(outcome.retry_after_s) && (outcome.retry_after_s ?? 0) > 0
        ? outcome.retry_after_s!
        : DEFAULT_QUOTA_COOLDOWN_S;
      account.cooldown_until = new Date(Date.now() + retryAfterS * 1000).toISOString();
    }
    this.writeRecord(tenantId, record);
  }

  async status(tenantId: string): Promise<GrantStatus> {
    const recordPath = this.recordFile(tenantId);
    const record = this.readRecord(tenantId);
    if (record) {
      if (record.version === 1) {
        const linkedAt = statSync(recordPath).mtime.toISOString();
        return {
          linked: true, linked_at: linkedAt, kind: record.kind, active_account_id: "legacy",
          accounts: [{ id: "legacy", label: cleanLabel(undefined, record.kind), kind: record.kind, linked_at: linkedAt, active: true }],
        };
      }
      const normalized = this.asV3(tenantId, record);
      const active = normalized.accounts.find((account) => account.id === normalized.active_account_id)!;
      const now = Date.now();
      return {
        linked: true,
        linked_at: active.linked_at,
        kind: active.kind,
        active_account_id: active.id,
        accounts: normalized.accounts.map((account) => ({
          id: account.id,
          label: account.label,
          kind: account.kind,
          linked_at: account.linked_at,
          active: account.id === active.id,
          plan: account.plan,
          eligible: account.kind === "api_key" || account.plan !== null,
          usage_tokens: account.usage_tokens,
          cooldown_until: account.cooldown_until && Date.parse(account.cooldown_until) > now
            ? account.cooldown_until
            : null,
        })),
      };
    }

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

  async diagnostic(tenantId: string): Promise<GrantDiagnostic> {
    const legacyPresent = existsSync(this.file(tenantId)) || existsSync(this.kindFile(tenantId));
    const base = {
      schema: "leaf.grant-diagnostic.v1" as const,
      backend: "file" as const,
      legacy_fallback_present: legacyPresent,
    };
    const owner = (path: string): GrantDiagnostic["owner"] => {
      const st = lstatSync(path);
      return {
        uid: typeof st.uid === "number" ? st.uid : null,
        gid: typeof st.gid === "number" ? st.gid : null,
        mode: (st.mode & 0o777).toString(8).padStart(4, "0"),
      };
    };
    try {
      const recordPath = this.recordFile(tenantId);
      const record = this.readRecord(tenantId);
      if (record) {
        const diagnosticKind = record.version === 1
          ? record.kind
          : record.accounts.find((account) => account.id === record.active_account_id)!.kind;
        const st = statSync(recordPath);
        return {
          ...base,
          linked: true,
          kind: diagnosticKind,
          linked_at: st.mtime.toISOString(),
          path_class: (process.env.LEAF_RUNTIME_ENV ?? "").trim().toLowerCase() === "production"
            ? "efs_access_point"
            : "local_file",
          record_format: record.version === 3 ? "v3" : record.version === 2 ? "v2" : "v1",
          owner: owner(recordPath),
          persistence: {
            atomic_publish: true,
            file_fsync: true,
            directory_fsync: process.platform !== "win32",
          },
          degraded: false,
        };
      }

      const legacyPath = this.file(tenantId);
      const legacyToken = this.readToken(tenantId);
      if (legacyToken) {
        const st = statSync(legacyPath);
        return {
          ...base,
          linked: true,
          kind: this.readKind(tenantId) ?? detectGrantKind(legacyToken),
          linked_at: st.mtime.toISOString(),
          path_class: (process.env.LEAF_RUNTIME_ENV ?? "").trim().toLowerCase() === "production"
            ? "efs_access_point"
            : "local_file",
          record_format: "legacy",
          owner: owner(legacyPath),
          persistence: { atomic_publish: false, file_fsync: false, directory_fsync: false },
          degraded: true,
        };
      }

      if (tenantId === this.defaultTenant) {
        const fallback = await this.envFallback.get(tenantId);
        if (fallback) {
          return {
            ...base,
            linked: true,
            kind: fallback.kind,
            linked_at: null,
            path_class: "environment",
            record_format: "environment",
            owner: { uid: null, gid: null, mode: null },
            persistence: { atomic_publish: false, file_fsync: false, directory_fsync: false },
            degraded: true,
          };
        }
      }

      return {
        ...base,
        linked: false,
        kind: "missing",
        linked_at: null,
        path_class: (process.env.LEAF_RUNTIME_ENV ?? "").trim().toLowerCase() === "production"
          ? "efs_access_point"
          : "local_file",
        record_format: "missing",
        owner: { uid: null, gid: null, mode: null },
        persistence: { atomic_publish: false, file_fsync: false, directory_fsync: false },
        degraded: false,
      };
    } catch {
      return {
        ...base,
        linked: false,
        kind: "missing",
        linked_at: null,
        path_class: (process.env.LEAF_RUNTIME_ENV ?? "").trim().toLowerCase() === "production"
          ? "efs_access_point"
          : "local_file",
        record_format: "invalid",
        owner: { uid: null, gid: null, mode: null },
        persistence: { atomic_publish: false, file_fsync: false, directory_fsync: false },
        degraded: true,
      };
    }
  }

  async remove(tenantId: string, accountId?: string): Promise<void> {
    if (accountId) {
      const existing = this.readRecord(tenantId);
      if (!existing) throw new Error("grant account not found");
      const record = this.asV3(tenantId, existing);
      const accounts = record.accounts.filter((account) => account.id !== accountId);
      if (accounts.length === record.accounts.length) throw new Error("grant account not found");
      if (accounts.length) {
        record.accounts = accounts;
        if (record.active_account_id === accountId) record.active_account_id = accounts[0].id;
        this.writeRecord(tenantId, record);
        return;
      }
    }
    rmSync(this.recordFile(tenantId), { force: true });
    rmSync(this.file(tenantId), { force: true });
    rmSync(this.kindFile(tenantId), { force: true });
  }
}

/**
 * GRANT-STORE SEAM (F18, security-audit 2026-07-18) — the backing store for grants at
 * rest is selectable via env `LEAF_GRANT_STORE`, so a production deployment can swap the
 * on-disk file store for a sealed secret store WITHOUT touching call sites:
 *
 *   - `file`  (DEFAULT): `FileTenantGrantStore` — one mode-0600 grant record per tenant
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

  async acquireGrant(tenantId: string): Promise<GrantLease> {
    if (!this.opts.store.acquire) {
      throw new Error("grant routing is not configured");
    }
    return this.opts.store.acquire(tenantId);
  }

  async settleGrant(tenantId: string, leaseId: string, outcome: GrantSettlement): Promise<void> {
    await this.opts.store.settle?.(tenantId, leaseId, outcome);
  }
}
