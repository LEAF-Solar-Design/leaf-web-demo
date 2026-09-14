import { afterEach, describe, expect, it, vi } from "vitest";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import type { ForgeRemoteAuthorityOptions } from "../src/vendor/mushy-author/ports/impl/forgeRemoteAuthority.js";
const capture = vi.hoisted(() => ({ options: undefined as ForgeRemoteAuthorityOptions | undefined }));
vi.mock("../src/vendor/mushy-author/ports/impl/forgeRemoteAuthority.js", async importOriginal => {
  const actual = await importOriginal<typeof import("../src/vendor/mushy-author/ports/impl/forgeRemoteAuthority.js")>();
  return { ...actual, ForgeRemoteAuthority: class extends actual.ForgeRemoteAuthority {
    constructor(options: ForgeRemoteAuthorityOptions) { super(options); capture.options = options; }
  } };
});
import { createTenantForgeConfigurationFromEnv } from "../src/ports/impl/tenantForgeAuthority.js";
const roots: string[] = [];
afterEach(() => { for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true }); capture.options = undefined; });
function fixture() {
  const root = mkdtempSync(join(tmpdir(), "tenant-forge-config-")); roots.push(root);
  const dir = join(root, "credentials"); mkdirSync(dir);
  const map = join(root, "origins.json"); const remoteUrl = "https://forge.leafdesign.ai/team/tenant-a.git";
  writeFileSync(map, JSON.stringify({ version: 1, tenants: [{ tenantId: "tenant-a", remoteUrl }] }));
  const credential = join(dir, "tenant-a.json");
  const env = { LEAF_TENANT_REPOSITORY_AUTHORITY: "forgejo", LEAF_TENANT_FORGE_ORIGIN_MAP: map, LEAF_TENANT_FORGE_CREDENTIAL_DIR: dir };
  return { root, dir, map, remoteUrl, credential, env };
}
describe("trusted tenant Forge composition", () => {
  it("keeps unconfigured development optional but required mode cannot fall back", () => {
    expect(createTenantForgeConfigurationFromEnv({})).toBeUndefined();
    expect(() => createTenantForgeConfigurationFromEnv({ LEAF_TENANT_REPOSITORY_AUTHORITY: "forgejo" })).toThrow("refused");
  });
  it("resolves only mapped tenants and rereads rotated or revoked credentials", async () => {
    const f = fixture(); const config = createTenantForgeConfigurationFromEnv(f.env)!;
    expect(await config.locator.repoRef("tenant-a")).toBe(f.remoteUrl);
    await expect(config.locator.repoRef("tenant-b")).rejects.toThrow("refused");
    const credentials = capture.options!.credentials;
    expect(await credentials("tenant-a", f.remoteUrl)).toBeNull();
    const write = (token: string) => writeFileSync(f.credential, JSON.stringify({ tenantId: "tenant-a", remoteUrl: f.remoteUrl, token }));
    write("fixture_one"); expect((await credentials("tenant-a", f.remoteUrl))?.token).toBe("fixture_one");
    write("fixture_two"); expect((await credentials("tenant-a", f.remoteUrl))?.token).toBe("fixture_two");
    rmSync(f.credential); expect(await credentials("tenant-a", f.remoteUrl)).toBeNull();
  });
  it.each(["tenant", "remote", "malformed", "username", "extra"])("rejects substituted %s credentials without exposing contents", async fault => {
    const f = fixture(); createTenantForgeConfigurationFromEnv(f.env);
    const row: Record<string, unknown> = { tenantId: "tenant-a", remoteUrl: f.remoteUrl, token: "private_fixture" };
    if (fault === "tenant") row.tenantId = "tenant-b";
    if (fault === "remote") row.remoteUrl = "https://forge.leafdesign.ai/team/other.git";
    if (fault === "username") row.username = "unsafe\nuser";
    if (fault === "extra") row.unexpected = true;
    writeFileSync(f.credential, fault === "malformed" ? "private_fixture{broken" : JSON.stringify(row));
    await expect(capture.options!.credentials("tenant-a", f.remoteUrl)).rejects.toThrow("tenant Forge configuration refused");
  });
  it.each(["origin", "duplicate", "shared", "traversal"])("rejects invalid %s topology", fault => {
    const f = fixture(); const rows = [{ tenantId: "tenant-a", remoteUrl: f.remoteUrl }];
    if (fault === "origin") rows[0].remoteUrl = "https://elsewhere.example/team/tenant-a.git";
    if (fault === "traversal") rows[0].tenantId = "../tenant-a";
    if (fault === "duplicate") rows.push({ ...rows[0] });
    if (fault === "shared") rows.push({ ...rows[0], tenantId: "tenant-b" });
    writeFileSync(f.map, JSON.stringify({ version: 1, tenants: rows }));
    expect(() => createTenantForgeConfigurationFromEnv(f.env)).toThrow("refused");
  });
});
