/** Trusted Forgejo topology for tenant capability publication in the workspace. */
import { readFileSync, statSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { isAbsolute, join } from "node:path";
import { ForgeRemoteAuthority, ForgePublicationError } from "../../vendor/mushy-author/ports/impl/forgeRemoteAuthority.js";
import type { ForgeRemoteAuthorityOptions } from "../../vendor/mushy-author/ports/impl/forgeRemoteAuthority.js";

const TENANT = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
function refuse(): never { throw new ForgePublicationError("refused", "tenant Forge configuration refused"); }
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return refuse();
  return value as Record<string, unknown>;
}
function remote(value: unknown): string {
  if (typeof value !== "string") return refuse();
  try {
    const url = new URL(value);
    if (url.origin !== "https://forge.leafdesign.ai" || url.username || url.password || url.search || url.hash ||
        url.href !== value || !/^\/[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+\.git$/.test(url.pathname) ||
        url.pathname.split("/").some(part => part === "." || part === "..")) return refuse();
    return value;
  } catch { return refuse(); }
}

export interface TenantForgeConfiguration {
  remoteAuthority: ForgeRemoteAuthority;
  locator: { repoRef(tenantId: string): Promise<string> };
}

/** Required mode cannot fall back to a local repository when topology is absent. */
export function createTenantForgeConfigurationFromEnv(env: NodeJS.ProcessEnv = process.env): TenantForgeConfiguration | undefined {
  const mode = env.LEAF_TENANT_REPOSITORY_AUTHORITY;
  const mapPath = env.LEAF_TENANT_FORGE_ORIGIN_MAP;
  const credentialDir = env.LEAF_TENANT_FORGE_CREDENTIAL_DIR;
  if (mode === undefined && mapPath === undefined && credentialDir === undefined) return undefined;
  try {
    if (mode !== undefined && mode !== "forgejo") return refuse();
    if (!mapPath || !credentialDir || !isAbsolute(mapPath) || !isAbsolute(credentialDir) ||
        !statSync(credentialDir).isDirectory()) return refuse();
    const map = object(JSON.parse(readFileSync(mapPath, "utf8")));
    if (Object.keys(map).sort().join(",") !== "tenants,version" || map.version !== 1 || !Array.isArray(map.tenants)) return refuse();
    const origins = new Map<string, string>();
    const remotes = new Set<string>();
    for (const value of map.tenants) {
      const entry = object(value);
      if (Object.keys(entry).sort().join(",") !== "remoteUrl,tenantId" ||
          typeof entry.tenantId !== "string" || !TENANT.test(entry.tenantId)) return refuse();
      const url = remote(entry.remoteUrl);
      if (origins.has(entry.tenantId) || remotes.has(url)) return refuse();
      origins.set(entry.tenantId, url); remotes.add(url);
    }
    const options: ForgeRemoteAuthorityOptions = {
      async locate(tenantId) { return origins.get(tenantId) ?? refuse(); },
      async credentials(tenantId, remoteUrl) {
        if (!TENANT.test(tenantId) || origins.get(tenantId) !== remoteUrl) return refuse();
        let contents: string;
        try { contents = await readFile(join(credentialDir, `${tenantId}.json`), "utf8"); }
        catch (error) {
          if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
          return refuse();
        }
        try {
          const record = object(JSON.parse(contents));
          if (Object.keys(record).sort().join(",") !== (record.username === undefined
            ? "remoteUrl,tenantId,token" : "remoteUrl,tenantId,token,username") ||
              record.tenantId !== tenantId || record.remoteUrl !== remoteUrl ||
              typeof record.token !== "string" || !/^[A-Za-z0-9_-]+$/.test(record.token) ||
              (record.username !== undefined && (typeof record.username !== "string" || !TENANT.test(record.username)))) return refuse();
          return { tenantId, remoteUrl, token: record.token,
            ...(typeof record.username === "string" ? { username: record.username } : {}) };
        } catch { return refuse(); }
      },
    };
    return { remoteAuthority: new ForgeRemoteAuthority(options), locator: { repoRef: options.locate } };
  } catch { return refuse(); }
}
