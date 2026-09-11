import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { ForgePublicationError, ForgeRemoteAuthority } from "../src/vendor/mushy-author/ports/impl/forgeRemoteAuthority.js";
import type { ForgeRemoteAuthorityOptions } from "../src/vendor/mushy-author/ports/impl/forgeRemoteAuthority.js";
import { TenantChangeRepo } from "../src/vendor/mushy-author/ports/impl/tenantChangeRepo.js";
import { TenantRepoProviderImpl } from "../src/vendor/mushy-author/ports/impl/tenantRepoProvider.js";
import type { PgTenantRepoLeaseCoordinator } from "../src/vendor/mushy-author/ports/impl/tenantRepoProvider.js";
import type { AuthoritativePublishRequest } from "../src/vendor/mushy-author/ports/index.js";

const TENANT = "tenant-a";
const A = "11111111-1111-4111-8111-111111111111";
const B = "22222222-2222-4222-8222-222222222222";
const identity = { name: "Leaf test", email: "test@example.invalid" };
const roots: string[] = [];
afterEach(() => { for (const dir of roots.splice(0)) rmSync(dir, { recursive: true, force: true }); });

function git(dir: string, args: string[]) {
  return execFileSync("git", args, { cwd: dir, encoding: "utf8" }).trim();
}

function fixture() {
  const root = mkdtempSync(join(tmpdir(), "forge-publication-test-"));
  roots.push(root);
  const seed = join(root, "seed");
  git(root, ["init", "-b", "main", seed]);
  writeFileSync(join(seed, "registry.json"), '{"tools":[]}\n');
  git(seed, ["add", "."]);
  git(seed, ["-c", `user.name=${identity.name}`, "-c", `user.email=${identity.email}`, "commit", "-m", "seed"]);
  const remote = join(root, "remote.git");
  git(root, ["clone", "--bare", seed, remote]);
  git(remote, ["config", "receive.denyNonFastForwards", "true"]);
  const base = git(remote, ["rev-parse", "main"]);
  const cache = join(root, "cache.git");
  git(root, ["clone", "--bare", remote, cache]);
  let revoked = false;
  const options: ForgeRemoteAuthorityOptions = {
    locate: async () => remote,
    credentials: async (tenantId, remoteUrl) => revoked ? null : { tenantId, remoteUrl },
    allowLocalPathsForTests: true,
  };
  const authority = new ForgeRemoteAuthority(options);
  function stage(id = A, dir = cache): AuthoritativePublishRequest {
    const changes = new TenantChangeRepo({ repoDir: dir, identity, workBase: root });
    const change = changes.create(id, base);
    writeFileSync(join(change.dir, "change.txt"), id);
    const commit = changes.stageCommit(change, "validated artifact");
    changes.cleanupWorktree(change);
    return {
      tenantId: TENANT, expectedMainSha: base, changeRef: change.ref, stagedCommit: commit,
      receipt: {
        contract: "leaf.customization.v1", tenant_id: TENANT, state: "staged",
        change_set_id: id, base_commit: base, staged_commit: commit,
        catalog_digest: "a".repeat(64), platform_release: "test",
        workspace_contract_digest: "b".repeat(64), idempotency_key: id,
      },
    };
  }
  return { root, remote, cache, seed, base, stage, options, authority, revoke: () => { revoked = true; } };
}

/** Real Git still executes; faults emulate loss of its reply at the boundary. */
class InterruptedAuthority extends ForgeRemoteAuthority {
  mode: "after-push" | "before-push" | "readback" = "after-push";
  private pushed = false;
  protected override async execute(args: string[], cwd: string, env: NodeJS.ProcessEnv) {
    const mainPush = args.includes("push") && args.some((arg) => arg.startsWith("--force-with-lease=refs/heads/main:"));
    if (mainPush && this.mode === "before-push") {
      throw new ForgePublicationError("unknown", "simulated timeout");
    }
    if (this.pushed && this.mode === "readback" && args.includes("ls-remote") && args.includes("refs/heads/main")) {
      throw new ForgePublicationError("unknown", "simulated failed readback");
    }
    const result = await super.execute(args, cwd, env);
    if (mainPush) {
      this.pushed = true;
      if (this.mode === "after-push") throw new ForgePublicationError("unknown", "simulated lost reply");
    }
    return result;
  }
}

describe("Forge remote artifact authority", () => {
  it("publishes exact staged bytes with protected fast-forward CAS and leaves local main unchanged", async () => {
    const f = fixture();
    const request = f.stage();
    const repo = f.authority.bind(TENANT, f.cache, f.remote);
    await expect(repo.publishAuthoritatively!(request)).resolves.toEqual({ commit: request.stagedCommit });
    expect(git(f.remote, ["rev-parse", "main"])).toBe(request.stagedCommit);
    expect(git(f.remote, ["rev-parse", request.changeRef])).toBe(request.stagedCommit);
    expect(git(f.remote, ["show", `${request.stagedCommit}:change.txt`])).toBe(A);
    expect(git(f.cache, ["rev-parse", "main"])).toBe(f.base);
    await expect(repo.publishAuthoritatively!(request)).resolves.toEqual({ commit: request.stagedCommit });
    f.revoke();
    await expect(repo.publishAuthoritatively!(request)).rejects.toMatchObject({ state: "refused" });
  });

  it("has one winner across two caches at the same expected remote head", async () => {
    const f = fixture();
    const cacheB = join(f.root, "cache-b.git");
    git(f.root, ["clone", "--bare", f.remote, cacheB]);
    const a = f.stage();
    const b = f.stage(B, cacheB);
    const results = await Promise.allSettled([
      f.authority.bind(TENANT, f.cache, f.remote).publishAuthoritatively!(a),
      f.authority.bind(TENANT, cacheB, f.remote).publishAuthoritatively!(b),
    ]);
    expect(results.filter((result) => result.status === "fulfilled")).toHaveLength(1);
    const winner = git(f.remote, ["rev-parse", "main"]);
    expect([a.stagedCommit, b.stagedCommit]).toContain(winner);
    const loser = winner === a.stagedCommit ? b : a;
    const loserDir = winner === a.stagedCommit ? cacheB : f.cache;
    await expect(f.authority.bind(TENANT, loserDir, f.remote).publishAuthoritatively!(loser)).rejects.toThrow("Git ref conflict");
    expect(git(f.remote, ["rev-parse", "main"])).toBe(winner);
    expect(git(f.cache, ["rev-parse", "main"])).toBe(f.base);
  });

  it("refuses wrong tenant, origin, private ref and credential binding before remote mutation", async () => {
    const f = fixture();
    const request = f.stage();
    const repo = f.authority.bind(TENANT, f.cache, f.remote);
    await expect(repo.publishAuthoritatively!({ ...request, tenantId: "tenant-b" })).rejects.toThrow("refused");
    await expect(repo.publishAuthoritatively!({ ...request, changeRef: "refs/heads/main" })).rejects.toThrow("refused");
    const wrongCredential = new ForgeRemoteAuthority({ ...f.options,
      credentials: async () => ({ tenantId: "tenant-b", remoteUrl: f.remote }),
    });
    await expect(wrongCredential.bind(TENANT, f.cache, f.remote).publishAuthoritatively!(request)).rejects.toThrow("refused");
    git(f.cache, ["config", "remote.origin.url", f.seed]);
    await expect(repo.publishAuthoritatively!(request)).rejects.toThrow("refused");
    expect(git(f.remote, ["rev-parse", "main"])).toBe(f.base);
    expect(git(f.seed, ["rev-parse", "main"])).toBe(f.base);
  });

  it("rejects unsafe production URLs and implicit local paths", async () => {
    for (const remote of ["http://forge.invalid/team/repo.git", "https://token@forge.invalid/team/repo.git",
      "https://forge.invalid/team/repo.git?q=token", "https://forge.invalid/team/repo.git#token",
      "https://forge.invalid/team/../repo.git", "/tmp/remote.git"]) {
      const authority = new ForgeRemoteAuthority({ locate: async () => remote, credentials: async () => null });
      await expect(authority.canonicalRemote(TENANT, remote)).rejects.toThrow("refused");
    }
  });

  it("reconciles a lost push reply only from exact authoritative readback", async () => {
    const f = fixture();
    const request = f.stage();
    const authority = new InterruptedAuthority(f.options);
    await expect(authority.bind(TENANT, f.cache, f.remote).publishAuthoritatively!(request))
      .resolves.toEqual({ commit: request.stagedCommit });
    expect(git(f.cache, ["rev-parse", "main"])).toBe(f.base);
  });

  it("reports expected old head as not published and preserves its staged ref", async () => {
    const f = fixture();
    const request = f.stage();
    const authority = new InterruptedAuthority(f.options);
    authority.mode = "before-push";
    await expect(authority.bind(TENANT, f.cache, f.remote).publishAuthoritatively!(request))
      .rejects.toMatchObject({ state: "not-published" });
    expect(git(f.remote, ["rev-parse", "main"])).toBe(f.base);
    expect(git(f.cache, ["rev-parse", request.changeRef])).toBe(request.stagedCommit);
  });

  it("keeps failed readback unknown after successful remote acceptance, then retries proof", async () => {
    const f = fixture();
    const request = f.stage();
    const authority = new InterruptedAuthority(f.options);
    authority.mode = "readback";
    await expect(authority.bind(TENANT, f.cache, f.remote).publishAuthoritatively!(request))
      .rejects.toMatchObject({ state: "unknown" });
    expect(git(f.remote, ["rev-parse", "main"])).toBe(request.stagedCommit);
    expect(git(f.cache, ["rev-parse", "main"])).toBe(f.base);
    await expect(f.authority.bind(TENANT, f.cache, f.remote).publishAuthoritatively!(request))
      .resolves.toEqual({ commit: request.stagedCommit });
  });

  it("refuses a conflicting remote private ref without overwriting it", async () => {
    const f = fixture();
    const request = f.stage();
    git(f.remote, ["update-ref", request.changeRef, f.base]);
    await expect(f.authority.bind(TENANT, f.cache, f.remote).publishAuthoritatively!(request)).rejects.toThrow("Git ref conflict");
    expect(git(f.remote, ["rev-parse", request.changeRef])).toBe(f.base);
    expect(git(f.remote, ["rev-parse", "main"])).toBe(f.base);
  });

  it("never rewinds accepted history even with an expected-head lease", async () => {
    const f = fixture();
    const request = f.stage();
    await f.authority.bind(TENANT, f.cache, f.remote).publishAuthoritatively!(request);
    git(f.cache, ["update-ref", request.changeRef, f.base]);
    const rewind = { ...request, expectedMainSha: request.stagedCommit, stagedCommit: f.base,
      receipt: { ...request.receipt, base_commit: request.stagedCommit, staged_commit: f.base } };
    await expect(f.authority.bind(TENANT, f.cache, f.remote).publishAuthoritatively!(rewind)).rejects.toThrow("Git ref conflict");
    expect(git(f.remote, ["rev-parse", "main"])).toBe(request.stagedCommit);
  });

  it("fast-forwards a fresh-stage cache, keeps private refs and refuses divergent cache history", async () => {
    const f = fixture();
    const a = f.stage();
    const b = f.stage(B);
    const repo = f.authority.bind(TENANT, f.cache, f.remote);
    await repo.publishAuthoritatively!(a);
    await repo.refreshMain!();
    expect(git(f.cache, ["rev-parse", "main"])).toBe(a.stagedCommit);
    expect(git(f.cache, ["rev-parse", b.changeRef])).toBe(b.stagedCommit);
    git(f.cache, ["update-ref", "refs/heads/main", b.stagedCommit]);
    await expect(repo.refreshMain!()).rejects.toThrow("Git ref conflict");
    expect(git(f.cache, ["rev-parse", "main"])).toBe(b.stagedCommit);
  });

  it("fails closed for a missing remote", async () => {
    const f = fixture();
    const request = f.stage();
    rmSync(f.remote, { recursive: true, force: true });
    await expect(f.authority.bind(TENANT, f.cache, f.remote).publishAuthoritatively!(request))
      .rejects.toMatchObject({ state: "unknown" });
    expect(git(f.cache, ["rev-parse", "main"])).toBe(f.base);
  });

  it("ignores cached URL rewrites and inherited Git config without exposing credentials in argv", async () => {
    const f = fixture();
    const request = f.stage();
    git(f.cache, ["config", `url.${f.seed}.insteadOf`, f.remote]);
    const token = "fixture_token_not_a_real_credential";
    class InspectAuthority extends ForgeRemoteAuthority {
      sawHeader = false;
      protected override async execute(args: string[], cwd: string, env: NodeJS.ProcessEnv) {
        expect(args.join(" ")).not.toContain(token);
        expect(env.GIT_TRACE).toBeUndefined();
        expect(env.GIT_ASKPASS).toBeUndefined();
        if (args.includes("push")) {
          const values = Object.entries(env).filter(([key]) => key.startsWith("GIT_CONFIG_VALUE_"));
          expect(values.some(([, value]) => value === `Authorization: Basic ${Buffer.from(`oauth2:${token}`).toString("base64")}`)).toBe(true);
          this.sawHeader = true;
        }
        return super.execute(args, cwd, env);
      }
    }
    const authority = new InspectAuthority({ ...f.options,
      credentials: async (tenantId, remoteUrl) => ({ tenantId, remoteUrl, token }),
    });
    const keys = ["GIT_TRACE", "GIT_ASKPASS", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"];
    const previous = keys.map((key) => process.env[key]);
    Object.assign(process.env, { GIT_TRACE: "1", GIT_ASKPASS: "invalid-helper",
      GIT_CONFIG_COUNT: "1", GIT_CONFIG_KEY_0: "http.followRedirects", GIT_CONFIG_VALUE_0: "true" });
    try {
      await authority.bind(TENANT, f.cache, f.remote).publishAuthoritatively!(request);
    } finally {
      keys.forEach((key, i) => {
        if (previous[i] === undefined) delete process.env[key];
        else process.env[key] = previous[i];
      });
    }
    expect(authority.sawHeader).toBe(true);
    expect(git(f.remote, ["rev-parse", "main"])).toBe(request.stagedCommit);
    expect(git(f.seed, ["rev-parse", "main"])).toBe(f.base);
    expect(git(f.cache, ["config", "--list"])).not.toContain(token);
  });

  it("bootstraps and refreshes the provider cache only within its existing lease", async () => {
    const f = fixture();
    const lease = {
      withLease: async <T>(tenantId: string, action: (lease: unknown) => Promise<T>) =>
        action({ tenantId, lost: false, repoDirs: new Set<string>() }),
      runFenced: async <T>(_lease: unknown, action: () => Promise<T>) => action(),
    } as unknown as PgTenantRepoLeaseCoordinator;
    const provider = new TenantRepoProviderImpl({
      locator: { repoRef: async () => f.seed }, remoteRef: async () => f.remote, remoteAuthority: f.authority,
      bareBase: join(f.root, "provider"), lease, authoringMode: "singleton",
    });
    await expect(provider.bare(TENANT)).rejects.toThrow("lease was lost");
    await provider.withTenantLease(TENANT, async (fence) => {
      const cache = await fence(() => provider.bare(TENANT));
      expect(git(cache.dir, ["rev-parse", "main"])).toBe(f.base);
      const staged = f.stage();
      await f.authority.bind(TENANT, f.cache, f.remote).publishAuthoritatively!(staged);
      await fence(() => cache.refreshMain!());
      expect(git(cache.dir, ["rev-parse", "main"])).toBe(staged.stagedCommit);
      git(cache.dir, ["config", "remote.origin.url", f.seed]);
      await expect(fence(() => cache.refreshMain!())).rejects.toThrow("refused");
      expect(git(cache.dir, ["rev-parse", "main"])).toBe(staged.stagedCommit);
    });
  });
});
