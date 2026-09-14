import { afterEach, describe, expect, it, vi } from "vitest";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync, symlinkSync } from "node:fs";
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
import { createTenantForgeConfigurationFromEnv, protectedJson } from "../src/ports/impl/tenantForgeAuthority.js";
const roots: string[] = [];
// Inject only for fixtures: Linux /tmp is intentionally rejected in production.
const fixtureJson = (path: string, limit: number): unknown => {
  const raw = readFileSync(path); if (raw.length > limit) throw new Error("too large");
  return JSON.parse(raw.toString("utf8"));
};
const create = (env: NodeJS.ProcessEnv, local?: (tenant: string) => string | Promise<string>) =>
  createTenantForgeConfigurationFromEnv(env, local, fixtureJson);
afterEach(() => { for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true }); capture.options = undefined; });
function fixture() {
  const root = mkdtempSync(join(tmpdir(), "tenant-forge-config-")); roots.push(root);
  const dir = join(root, "credentials"); mkdirSync(dir);
  const map = join(root, "origins.json"); const remoteUrl = "https://forge.leafdesign.ai/team/tenant-a.git";
  writeFileSync(map, JSON.stringify({ version: 1, tenants: { "tenant-a": { mode: "forge", remoteUrl, credential_ref: "current-a.json" }, "local-a": { mode: "local" } } }));
  const credential = join(dir, "current-a.json");
  const env = { LEAF_TENANT_REPOSITORY_AUTHORITY: "forgejo", LEAF_TENANT_FORGE_CONFIG: map, LEAF_TENANT_FORGE_CREDENTIAL_DIR: dir };
  return { root, dir, map, remoteUrl, credential, env };
}
describe("trusted tenant Forge composition", () => {
  it("keeps unconfigured development optional but required mode cannot fall back", () => {
    expect(createTenantForgeConfigurationFromEnv({})).toBeUndefined();
    expect(() => createTenantForgeConfigurationFromEnv({ LEAF_TENANT_REPOSITORY_AUTHORITY: "forgejo" })).toThrow("refused");
  });
  it("resolves only mapped tenants and rereads rotated or revoked credentials", async () => {
    const f = fixture(); const config = create(f.env)!;
    expect(await config.locator.repoRef("tenant-a")).toBe(f.remoteUrl);
    await expect(config.locator.repoRef("tenant-b")).rejects.toThrow("refused");
    const credentials = capture.options!.credentials;
    expect(await credentials("tenant-a", f.remoteUrl)).toBeNull();
    const write = (token: string) => writeFileSync(f.credential, JSON.stringify({ tenantId: "tenant-a", remoteUrl: f.remoteUrl, username: "oauth2", token }));
    write("fixture_one"); expect((await credentials("tenant-a", f.remoteUrl))?.token).toBe("fixture_one");
    write("fixture_two"); expect((await credentials("tenant-a", f.remoteUrl))?.token).toBe("fixture_two");
    rmSync(f.credential); expect(await credentials("tenant-a", f.remoteUrl)).toBeNull();
  });
  it.each(["tenant", "remote", "malformed", "username", "extra"])("rejects substituted %s credentials without exposing contents", async fault => {
    const f = fixture(); create(f.env);
    const row: Record<string, unknown> = { tenantId: "tenant-a", remoteUrl: f.remoteUrl, username: "oauth2", token: "private_fixture" };
    if (fault === "tenant") row.tenantId = "tenant-b";
    if (fault === "remote") row.remoteUrl = "https://forge.leafdesign.ai/team/other.git";
    if (fault === "username") row.username = "unsafe\nuser";
    if (fault === "extra") row.unexpected = true;
    writeFileSync(f.credential, fault === "malformed" ? "private_fixture{broken" : JSON.stringify(row));
    expect(await capture.options!.credentials("tenant-a", f.remoteUrl)).toBeNull();
  });
  it.each(["origin", "shared", "traversal", "ref-traversal", "shared-ref", "array", "extra-local"])("rejects invalid %s topology", fault => {
    const f = fixture();
    const row = { mode: "forge", remoteUrl: f.remoteUrl, credential_ref: "current-a.json" };
    const rows: Record<string, unknown> = { "tenant-a": row };
    if (fault === "origin") row.remoteUrl = "https://elsewhere.example/team/tenant-a.git";
    if (fault === "traversal") rows["../tenant-a"] = row;
    if (fault === "ref-traversal") row.credential_ref = "../token.json";
    if (fault === "shared") rows["tenant-b"] = { ...row, credential_ref: "b.json" };
    if (fault === "shared-ref") rows["tenant-b"] = { ...row, remoteUrl: "https://forge.leafdesign.ai/team/b.git" };
    if (fault === "extra-local") rows["local-a"] = { mode: "local", remoteUrl: f.remoteUrl };
    writeFileSync(f.map, JSON.stringify({ version: 1, tenants: fault === "array" ? [row] : rows }));
    expect(() => create(f.env)).toThrow("refused");
  });
  it("selects only explicit local tenants and supports optional forgejo mode", async () => {
    const f = fixture(); const local = vi.fn(async (tenant: string) => "/local/" + tenant);
    const config = create({ ...f.env, LEAF_TENANT_REPOSITORY_AUTHORITY: undefined }, local)!;
    expect(config.isRemoteTenant("tenant-a")).toBe(true);
    expect(config.isRemoteTenant("local-a")).toBe(false);
    expect(() => config.isRemoteTenant("unknown")).toThrow("refused");
    expect(await config.locator.repoRef("tenant-a")).toBe(f.remoteUrl);
    expect(local).not.toHaveBeenCalled();
    expect(await config.locator.repoRef("local-a")).toBe("/local/local-a");
    await expect(config.locator.repoRef("unknown")).rejects.toThrow("refused");
    expect(local).toHaveBeenCalledTimes(1);
    await expect(create(f.env)!.locator.repoRef("local-a")).rejects.toThrow("refused");
    await expect(capture.options!.locate("local-a")).rejects.toThrow("refused");
    expect(() => create({ ...f.env, LEAF_TENANT_REPOSITORY_AUTHORITY: "other" })).toThrow("refused");
  });
  it("bounds config and credential reads and refuses oversized credentials", async () => {
    const f = fixture(); const reads: Array<[string, number]> = [];
    createTenantForgeConfigurationFromEnv(f.env, undefined, (path, limit) => {
      reads.push([path, limit]); return fixtureJson(path, limit);
    });
    writeFileSync(f.credential, JSON.stringify({ tenantId: "tenant-a", remoteUrl: f.remoteUrl, username: "oauth2", token: "x".repeat(17000) }));
    expect(await capture.options!.credentials("tenant-a", f.remoteUrl)).toBeNull();
    expect(reads).toEqual([[f.map, 128 * 1024], [f.credential, 16 * 1024]]);
  });
  it("protected reader refuses relative paths, directories, oversize and symlink parents", () => {
    const f = fixture();
    expect(() => protectedJson("relative.json", 100)).toThrow();
    expect(() => protectedJson(f.dir, 100)).toThrow();
    expect(() => protectedJson(f.map, 1)).toThrow();
    const linked = join(f.root, "linked");
    symlinkSync(f.dir, linked, process.platform === "win32" ? "junction" : "dir");
    writeFileSync(join(f.dir, "value.json"), "{}");
    expect(() => protectedJson(join(linked, "value.json"), 100)).toThrow();
  });
});
