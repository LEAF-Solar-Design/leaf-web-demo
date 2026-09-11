import { describe, expect, it, vi } from "vitest";
import { resolve, join } from "node:path";

// Configuration tests stop at the real authority's constructor boundary.
// tenantForgeRemoteAuthority tests exercise that authority with real local Git.
vi.mock("../src/vendor/mushy-author/ports/impl/forgeRemoteAuthority.js", () => ({
  ForgeRemoteAuthority: class { constructor(readonly opts: any) {} },
}));
import { createTenantForgeAuthorityFromEnv } from "../src/ports/impl/tenantForgeAuthority.js";

const remote = "https://forge.leafdesign.ai/fixture/tenant-a.git";
const config = resolve("fixture-config.json");
const directory = resolve("fixture-credentials");
const env = { LEAF_TENANT_FORGE_CONFIG: config, LEAF_TENANT_FORGE_CREDENTIAL_DIR: directory };
const local = vi.fn((tenant: string) => `/local/${tenant}`);
function fixture() {
  const document: any = { version: 1, tenants: {
    a: { mode: "forge", remoteUrl: remote, credential_ref: "a.json" },
    b: { mode: "local" },
  } };
  let credential: any = { tenantId: "a", remoteUrl: remote, username: "fixture", token: "synthetic_test_only" };
  const reads: string[] = [];
  const read = (path: string) => {
    reads.push(path);
    if (path === config) return document;
    if (path !== join(directory, "a.json")) throw new Error("unexpected path");
    return credential;
  };
  return { document, read, reads, setCredential: (value: any) => { credential = value; } };
}

describe("server-owned tenant Forge composition", () => {
  it("keeps disabled local tenants explicit and rejects unknown configured tenants", async () => {
    const f = fixture();
    const c = createTenantForgeAuthorityFromEnv(local, env, f.read);
    expect(await c.repoRef("a")).toBe("/local/a");
    expect(await c.remoteRef("a")).toBe(remote);
    await expect(c.remoteRef("b")).rejects.toThrow("unavailable");
    expect(await c.repoRef("b")).toBe("/local/b");
    expect(c.remoteAuthority("a")).toBeDefined();
    expect(c.remoteAuthority("b")).toBeUndefined();
    expect(() => c.remoteAuthority("unknown")).toThrow("unavailable");
    await expect(c.repoRef("unknown")).rejects.toThrow("unavailable");
    expect(f.reads).toEqual([config]);
  });

  it("checks exact tenant and origin and reads revocation on every operation", async () => {
    const f = fixture();
    const c = createTenantForgeAuthorityFromEnv(local, env, f.read);
    const opts = (c.remoteAuthority("a") as any).opts;
    expect(await opts.locate("a")).toBe(remote);
    expect(await opts.credentials("a", remote)).toMatchObject({ tenantId: "a", remoteUrl: remote });
    expect(await opts.credentials("a", remote + "-other")).toBeNull();
    expect(await opts.credentials("b", remote)).toBeNull();
    f.setCredential({ tenantId: "b", remoteUrl: remote, username: "fixture", token: "synthetic" });
    expect(await opts.credentials("a", remote)).toBeNull();
    f.setCredential({ tenantId: "a", remoteUrl: remote + "-other", username: "fixture", token: "synthetic" });
    expect(await opts.credentials("a", remote)).toBeNull();
    f.setCredential(null);
    expect(await opts.credentials("a", remote)).toBeNull();
    expect(f.reads.filter(p => p === join(directory, "a.json"))).toHaveLength(4);
  });

  it("has no credential read when the integration is absent", async () => {
    const read = vi.fn(() => { throw new Error("must not read"); });
    const c = createTenantForgeAuthorityFromEnv(local, {}, read);
    expect(await c.repoRef("any")).toBe("/local/any");
    expect(c.remoteAuthority("any")).toBeUndefined();
    expect(read).not.toHaveBeenCalled();
  });

  it("refuses partial configuration, unsafe origins, extra fields and shared identities", () => {
    expect(() => createTenantForgeAuthorityFromEnv(local, { LEAF_TENANT_FORGE_CONFIG: config })).toThrow();
    for (const mutate of [
      (d: any) => { d.tenants.a.remoteUrl = "https://other.invalid/fixture/a.git"; },
      (d: any) => { d.tenants.a.remoteUrl = "https://secret@forge.leafdesign.ai/fixture/a.git"; },
      (d: any) => { d.tenants.a.credential_ref = "../a.json"; },
      (d: any) => { d.tenants.a.token = "synthetic"; },
      (d: any) => { d.tenants.b = { ...d.tenants.a }; },
      (d: any) => { d.version = 2; },
    ]) {
      const f = fixture(); mutate(f.document);
      expect(() => createTenantForgeAuthorityFromEnv(local, env, f.read)).toThrow("unavailable");
    }
  });
});
