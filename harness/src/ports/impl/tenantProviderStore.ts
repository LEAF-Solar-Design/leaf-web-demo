// Per-tenant converse provider override (default OFF for every tenant).
//
// One small JSON record per tenant, beside that tenant's grant files in the grant directory
// (`<grantsDir>/<tenant>.provider.json`), routes ONLY that tenant's assistant turns to an
// OpenAI- or Anthropic-compatible server such as the Mac mini's local model. No record means the tenant is
// unchanged. The record never holds a credential: it names an environment variable
// (`LEAF_PROVIDER_KEY_*`) that the harness task receives from its secret store, and the value
// is resolved server-side per turn.
//
// Fails closed: a record that names a host outside LEAF_TENANT_PROVIDER_HOSTS, a key variable
// outside the LEAF_PROVIDER_KEY_ namespace, or a key that is not present ends the turn with an
// error instead of silently falling back to another provider. The host allowlist is what stops
// a record from pointing any harness credential at an arbitrary server.
import { mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { randomUUID } from "node:crypto";

/** Same tenant-id rule as the grant store (server/tenant_id_validator.py). */
const TENANT_ID_RE = /^[a-z0-9][a-z0-9_-]{0,62}$/;
const KEY_ENV_RE = /^LEAF_PROVIDER_KEY_[A-Z0-9_]+$/;
const MODEL_RE = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/;
const LABEL_RE = /^[A-Za-z0-9][A-Za-z0-9 .,()'&/+-]{0,63}$/;
const HOST_RE = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$/;
const MAX_RECORD_BYTES = 4096;

export interface TenantProviderOverride {
  provider: "openai_compatible" | "anthropic_compatible";
  /** Absolute https base URL of a compatible server, no trailing slash. */
  base_url: string;
  model: string;
  /** Name (never value) of the harness environment variable holding the bearer key. */
  api_key_env: string;
  /** Human label for where the model runs, shown with the model badge (e.g. "local on Mac mini"). */
  label: string;
  /** Optional turn deadline for this tenant, 10..1800 s. */
  turn_timeout_s?: number;
}

export class TenantProviderConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "TenantProviderConfigError";
  }
}

function tenantBase(tenantId: string): string {
  if (typeof tenantId !== "string" || !TENANT_ID_RE.test(tenantId)) {
    throw new TenantProviderConfigError("invalid tenant id");
  }
  return tenantId;
}

/** Exact hostnames from LEAF_TENANT_PROVIDER_HOSTS (comma separated). Empty = feature off. */
export function allowedProviderHosts(env: NodeJS.ProcessEnv): ReadonlySet<string> {
  const hosts = (env.LEAF_TENANT_PROVIDER_HOSTS ?? "").split(",")
    .map(host => host.trim().toLowerCase()).filter(Boolean);
  for (const host of hosts) {
    if (!HOST_RE.test(host)) throw new TenantProviderConfigError("invalid LEAF_TENANT_PROVIDER_HOSTS");
  }
  return new Set(hosts);
}

/** Validate an untrusted record. Returns a normalized copy or throws a value-free error. */
export function parseTenantProviderOverride(raw: unknown, allowedHosts: ReadonlySet<string>): TenantProviderOverride {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new TenantProviderConfigError("invalid record");
  const record = raw as Record<string, unknown>;
  const known = new Set(["provider", "base_url", "model", "api_key_env", "label", "turn_timeout_s", "updated_at"]);
  if (Object.keys(record).some(key => !known.has(key))) throw new TenantProviderConfigError("unknown field");
  if (record.provider !== "openai_compatible" && record.provider !== "anthropic_compatible") {
    throw new TenantProviderConfigError("invalid provider");
  }
  if (typeof record.base_url !== "string" || record.base_url.length > 256) {
    throw new TenantProviderConfigError("invalid base_url");
  }
  let url: URL;
  try {
    url = new URL(record.base_url);
  } catch {
    throw new TenantProviderConfigError("invalid base_url");
  }
  if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash || url.port) {
    throw new TenantProviderConfigError("invalid base_url");
  }
  if (!allowedHosts.has(url.hostname.toLowerCase())) throw new TenantProviderConfigError("base_url host not allowed");
  if (typeof record.model !== "string" || !MODEL_RE.test(record.model)) throw new TenantProviderConfigError("invalid model");
  if (typeof record.api_key_env !== "string" || !KEY_ENV_RE.test(record.api_key_env)) {
    throw new TenantProviderConfigError("invalid api_key_env");
  }
  if (record.label !== undefined && (typeof record.label !== "string" || !LABEL_RE.test(record.label))) {
    throw new TenantProviderConfigError("invalid label");
  }
  const timeout = record.turn_timeout_s;
  if (timeout !== undefined && (typeof timeout !== "number" || !Number.isInteger(timeout) || timeout < 10 || timeout > 1800)) {
    throw new TenantProviderConfigError("invalid turn_timeout_s");
  }
  return {
    provider: record.provider,
    base_url: url.href.replace(/\/+$/, ""),
    model: record.model,
    api_key_env: record.api_key_env,
    label: record.label ?? "tenant model",
    ...(timeout !== undefined ? { turn_timeout_s: timeout as number } : {}),
  };
}

/** File-backed store in the grant directory. Reads are uncached so an admin write applies on the next turn. */
export class FileTenantProviderStore {
  constructor(private readonly dir: string) {}

  private path(tenantId: string): string {
    return join(this.dir, `${tenantBase(tenantId)}.provider.json`);
  }

  /** The raw record, or null when the tenant has no override. */
  async get(tenantId: string): Promise<unknown | null> {
    let text: string;
    try {
      text = await readFile(this.path(tenantId), "utf8");
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
      throw new TenantProviderConfigError("provider record unreadable");
    }
    if (Buffer.byteLength(text) > MAX_RECORD_BYTES) throw new TenantProviderConfigError("provider record too large");
    try {
      return JSON.parse(text);
    } catch {
      throw new TenantProviderConfigError("provider record unreadable");
    }
  }

  /** Validate and write atomically. Returns the stored record. */
  async put(tenantId: string, raw: unknown, allowedHosts: ReadonlySet<string>): Promise<TenantProviderOverride> {
    const record = parseTenantProviderOverride(raw, allowedHosts);
    const target = this.path(tenantId);
    await mkdir(this.dir, { recursive: true });
    const temp = `${target}.${randomUUID()}.tmp`;
    await writeFile(temp, JSON.stringify({ ...record, updated_at: new Date().toISOString() }) + "\n",
      { encoding: "utf8", mode: 0o600 });
    await rename(temp, target);
    return record;
  }

  async remove(tenantId: string): Promise<void> {
    await rm(this.path(tenantId), { force: true });
  }
}

export interface ResolvedTenantProvider {
  override: TenantProviderOverride;
  apiKey: string;
}

/**
 * Resolve one tenant's override for a turn: null when the tenant has none (the unchanged
 * path). Throws a value-free error when a record exists but cannot be honoured.
 */
export async function resolveTenantProvider(store: Pick<FileTenantProviderStore, "get">, tenantId: string,
  env: NodeJS.ProcessEnv): Promise<ResolvedTenantProvider | null> {
  const raw = await store.get(tenantId);
  if (raw === null) return null;
  const override = parseTenantProviderOverride(raw, allowedProviderHosts(env));
  const apiKey = env[override.api_key_env]?.trim();
  if (!apiKey) throw new TenantProviderConfigError("provider key unavailable");
  return { override, apiKey };
}
