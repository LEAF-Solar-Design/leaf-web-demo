import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { afterEach, expect, it, vi } from "vitest";
import { createTenantForgeAuthorityFromEnv } from "../src/ports/impl/tenantForgeAuthority.js";
import { TenantRepoProviderImpl } from "../src/ports/impl/tenantRepoProvider.js";
import { ForgeRemoteAuthority } from "../src/vendor/mushy-author/ports/impl/forgeRemoteAuthority.js";
import { TenantChangeRepo } from "../src/vendor/mushy-author/ports/impl/tenantChangeRepo.js";

const roots: string[] = [];
afterEach(() => { vi.restoreAllMocks(); for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true }); });
function git(dir: string, args: string[]) {
  return execFileSync("git", args, { cwd: dir, encoding: "utf8", timeout: 10000, stdio: ["ignore", "pipe", "pipe"] }).trim();
}

it("composes configured Forge with inPlace checkout and preserves exact accepted catalog materialization", async () => {
  const root = mkdtempSync(join(tmpdir(), "tenant-forge-composition-")); roots.push(root);
  const local = join(root, "local");
  git(root, ["init", "-b", "main", local]);
  writeFileSync(join(local, "registry.json"), '{"tools":[]}\n');
  git(local, ["add", "."]);
  git(local, ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "seed"]);
  const remoteDisk = join(root, "remote.git");
  git(root, ["clone", "--bare", local, remoteDisk]);
  const url = "https://forge.leafdesign.ai/fixture/composition.git";
  const path = resolve(root, "config.json");
  const c = createTenantForgeAuthorityFromEnv(() => local, {
    LEAF_TENANT_FORGE_CONFIG: path, LEAF_TENANT_FORGE_CREDENTIAL_DIR: root,
  }, input => input === path ? { version: 1, tenants: {
    a: { mode: "forge", remoteUrl: url, credential_ref: "a.json" }, b: { mode: "local" },
  } } : { tenantId: "a", remoteUrl: url, username: "fixture", token: "synthetic_only" });

  // Only the network boundary is redirected. Actual factory, downstream provider,
  // lease composition, authority authentication/CAS and local Git execute.
  const original = (ForgeRemoteAuthority.prototype as any).execute;
  const observed: string[][] = [];
  vi.spyOn(ForgeRemoteAuthority.prototype as any, "execute").mockImplementation(function (this: ForgeRemoteAuthority, ...values: any[]) {
    const [args, cwd, env] = values;
    observed.push(args);
    const network = args.some((arg: string) => ["fetch", "push", "ls-remote"].includes(arg));
    return original.call(this, args.map((arg: string) => network && arg === url ? remoteDisk : arg), cwd, network ? { ...env, GIT_ALLOW_PROTOCOL: "file" } : env);
  });
  const lease = {
    withLease: async (tenantId: string, action: any) => action({ tenantId, lost: false, repoDirs: new Set() }),
    runFenced: async (_lease: unknown, action: any) => action(),
  };
  const provider = new TenantRepoProviderImpl({
    locator: { repoRef: c.repoRef }, remoteRef: c.remoteRef, remoteAuthority: c.remoteAuthority,
    inPlace: true, bareBase: join(root, "bare"), workBase: root,
    lease: lease as any, authoringMode: "singleton",
  });
  await expect(provider.checkout("unknown")).rejects.toThrow("unavailable");
  await provider.withTenantLease("a", async fence => {
    const checkout = await fence(() => provider.checkout("a"));
    expect(checkout.dir).toBe(local);
    expect(observed).toHaveLength(0);
    const bare = await fence(() => provider.bare("a"));
    expect(git(bare.dir, ["remote", "get-url", "origin"])).toBe(url);
    expect(observed.some(args => args.includes(url))).toBe(true);
    const base = git(bare.dir, ["rev-parse", "main"]);
    const changes = new TenantChangeRepo({ repoDir: bare.dir, workBase: root,
      identity: { name: "Fixture", email: "fixture@example.invalid" } });
    const id = "11111111-1111-4111-8111-111111111111";
    const change = changes.create(id, base);
    writeFileSync(join(change.dir, "registry.json"), '{"tools":[],"accepted":true}\n');
    const commit = changes.stageCommit(change, "accepted catalog");
    changes.cleanupWorktree(change);
    await bare.publishAuthoritatively!({ tenantId: "a", expectedMainSha: base,
      changeRef: change.ref, stagedCommit: commit, receipt: {
        contract: "leaf.customization.v1", tenant_id: "a", state: "staged", change_set_id: id,
        base_commit: base, staged_commit: commit, catalog_digest: "a".repeat(64),
        platform_release: "fixture", workspace_contract_digest: "b".repeat(64), idempotency_key: id,
      } });
    expect(git(remoteDisk, ["rev-parse", "main"])).toBe(commit);
    expect(git(bare.dir, ["rev-parse", "main"])).toBe(base);
    await fence(async () => changes.publishToMain(change, base));
    // Existing server effective_catalog_dir materializes a pinned commit from
    // this shared bare repository; it does not consume the mutable author dir.
    const effective = join(root, "effective");
    git(bare.dir, ["worktree", "add", "--detach", effective, commit]);
    expect(git(effective, ["rev-parse", "HEAD"])).toBe(commit);
    expect(readFileSync(join(effective, "registry.json"), "utf8")).toContain('"accepted":true');
    expect((await fence(() => provider.checkout("a"))).dir).toBe(local);
    expect(git(local, ["rev-parse", "HEAD"])).toBe(base);
  });
}, 120000);
