/** Server-owned tenant catalog authority. Project repository authority is separate. */
import { constants, closeSync, fstatSync, lstatSync, openSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, join } from "node:path";
import { ForgeRemoteAuthority, type ForgeCredential } from "../../vendor/mushy-author/ports/impl/forgeRemoteAuthority.js";

type Local = { mode: "local" };
type Remote = { mode: "forge"; remoteUrl: string; credential_ref: string };
type Entry = Local | Remote;

export interface TenantForgeComposition {
  /** Always resolves a server-owned origin or an explicitly local tenant. */
  repoRef(tenantId: string): Promise<string>;
  remoteRef(tenantId: string): Promise<string>;
  remoteAuthority(tenantId: string): ForgeRemoteAuthority | undefined;
}

function refused(): never { throw new Error("Tenant Forge configuration or credential unavailable"); }

function keys(value: unknown, expected: string[]): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).sort().join("\0") === [...expected].sort().join("\0");
}

/** Only protected local files supplied by server deployment, never caller paths. */
function protectedJson(path: string, maximum: number): unknown {
  if (!isAbsolute(path)) refused();
  // Protect parents as well: a writable parent permits replacement of a read-only file.
  let parent = dirname(path);
  for (;;) {
    const stat = lstatSync(parent);
    if (!stat.isDirectory() || stat.isSymbolicLink()
      || (process.platform !== "win32" && (stat.mode & 0o022) !== 0)) refused();
    const next = dirname(parent);
    if (next === parent) break;
    parent = next;
  }
  if (lstatSync(path).isSymbolicLink()) refused();
  const fd = openSync(path, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
  try {
    const stat = fstatSync(fd);
    if (!stat.isFile() || stat.size < 1 || stat.size > maximum
      || (process.platform !== "win32" && (stat.mode & 0o022) !== 0)) refused();
    const raw = readFileSync(fd);
    if (raw.length > maximum) refused();
    return JSON.parse(raw.toString("utf8"));
  } finally { closeSync(fd); }
}

function origin(value: unknown): value is string {
  if (typeof value !== "string") return false;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.hostname === "forge.leafdesign.ai"
      && !url.port && !url.username && !url.password && !url.search && !url.hash
      && url.href === value && /^\/[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+\.git$/.test(url.pathname)
      && !url.pathname.split("/").some(part => part === "." || part === "..");
  } catch { return false; }
}

/** Explicitly absent configuration retains existing local behavior.
 * Once configured, every tenant must have a closed forge/local entry. Unknown
 * tenants refuse rather than silently publishing a local-only catalog.
 */
export function createTenantForgeAuthorityFromEnv(
  localRepoRef: (tenantId: string) => string | Promise<string>,
  env: NodeJS.ProcessEnv = process.env,
  readJson: (path: string, maximum: number) => unknown = protectedJson,
): TenantForgeComposition {
  const path = env.LEAF_TENANT_FORGE_CONFIG;
  const credentialDir = env.LEAF_TENANT_FORGE_CREDENTIAL_DIR;
  if (path === undefined && credentialDir === undefined) {
    return { repoRef: async tenant => localRepoRef(tenant), remoteRef: async () => refused(), remoteAuthority: () => undefined };
  }
  if (!path || !credentialDir || !isAbsolute(path) || !isAbsolute(credentialDir)) refused();
  let document: unknown;
  try { document = readJson(path, 128 * 1024); } catch { return refused(); }
  if (!keys(document, ["version", "tenants"]) || document.version !== 1
    || !document.tenants || typeof document.tenants !== "object" || Array.isArray(document.tenants)) refused();
  const entries = new Map<string, Entry>();
  const origins = new Set<string>();
  const refs = new Set<string>();
  for (const [tenant, value] of Object.entries(document.tenants)) {
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$/.test(tenant)) refused();
    if (keys(value, ["mode"]) && value.mode === "local") {
      entries.set(tenant, { mode: "local" });
    } else if (keys(value, ["mode", "remoteUrl", "credential_ref"]) && value.mode === "forge"
      && origin(value.remoteUrl) && typeof value.credential_ref === "string"
      && /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}\.json$/.test(value.credential_ref)) {
      if (origins.has(value.remoteUrl) || refs.has(value.credential_ref)) refused();
      origins.add(value.remoteUrl); refs.add(value.credential_ref);
      entries.set(tenant, { mode: "forge", remoteUrl: value.remoteUrl, credential_ref: value.credential_ref });
    } else refused();
  }
  if (entries.size < 1 || entries.size > 1000) refused();
  const entry = (tenant: string): Entry => entries.get(tenant) ?? refused();
  const credentials = async (tenantId: string, remoteUrl: string): Promise<ForgeCredential | null> => {
    const selected = entry(tenantId);
    if (selected.mode !== "forge" || selected.remoteUrl !== remoteUrl) return null;
    try {
      // Read on every operation: removal/null means revoked, not cached authorization.
      const value = readJson(join(credentialDir, selected.credential_ref), 16 * 1024);
      if (!keys(value, ["tenantId", "remoteUrl", "username", "token"])
        || value.tenantId !== tenantId || value.remoteUrl !== remoteUrl
        || typeof value.username !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(value.username)
        || typeof value.token !== "string" || !/^[A-Za-z0-9_-]+$/.test(value.token)) return null;
      return { tenantId, remoteUrl, username: value.username, token: value.token };
    } catch { return null; }
  };
  const authority = new ForgeRemoteAuthority({
    locate: async tenant => {
      const selected = entry(tenant);
      if (selected.mode !== "forge") refused();
      return selected.remoteUrl;
    },
    credentials,
  });
  return {
    repoRef: async tenant => {
      entry(tenant);
      return localRepoRef(tenant);
    },
    remoteRef: async tenant => {
      const selected = entry(tenant);
      if (selected.mode !== "forge") refused();
      return selected.remoteUrl;
    },
    remoteAuthority: tenant => entry(tenant).mode === "forge" ? authority : undefined,
  };
}
