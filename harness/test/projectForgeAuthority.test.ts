import { execFileSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { AddressInfo } from "node:net";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProjectForgeAuthority, ProjectForgePublicationError,
  createProjectForgeAuthorityFromEnv, createProjectRepositoryEditsForService,
  type ProjectForgeAuthorityOptions } from "../src/ports/impl/projectForgeAuthority.js";
import { TenantChangeRepo, type TenantChangeSet } from "../src/vendor/mushy-author/ports/impl/tenantChangeRepo.js";
import { TenantRepoProviderImpl, type PgTenantRepoLeaseCoordinator } from "../src/ports/impl/tenantRepoProvider.js";
import { ProjectRepositoryEditCoordinator } from "../src/agent/projectRepositoryEditCoordinator.js";
import { PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT as contract,
  type ProjectRepositoryAuthority, type ProjectRepositoryEditCoordination,
  type TenantMutationFence, type WriterLeaseWitness } from "../src/ports/index.js";
import { createHarness } from "../src/server.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";

const authority: ProjectRepositoryAuthority = {
  tenantId: "11111111-1111-4111-8111-111111111111", organizationId: "22222222-2222-4222-8222-222222222222",
  projectId: "33333333-3333-4333-8333-333333333333", repoKey: "44444444-4444-4444-8444-444444444444",
};
const actor = "66666666-6666-4666-8666-666666666666";
const confirmation = "77777777-7777-4777-8777-777777777777";
const leaseId = "88888888-8888-4888-8888-888888888888";
const roots: string[] = [];
const hash = (s: string) => createHash("sha256").update(s).digest("hex");
function temporary(): string {
  const dir = mkdtempSync(join(tmpdir(), "project-forge-test-"));
  roots.push(dir);
  return dir;
}
function git(dir: string, args: string[]): string {
  return execFileSync("git", args, { cwd: dir, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
}
function fixture(a = authority) {
  const root = temporary();
  const work = join(root, "seed");
  const bareBase = join(root, "bare");
  mkdirSync(bareBase);
  const bare = join(bareBase, `${a.repoKey}.git`);
  const remote = join(root, "remote.git");
  git(root, ["init", "-q", "-b", "main", work]);
  writeFileSync(join(work, "existing.txt"), "before\n");
  git(work, ["add", "."]);
  git(work, ["-c", "user.name=test", "-c", "user.email=test@leaf.invalid", "commit", "-qm", "base"]);
  git(root, ["clone", "-q", "--bare", work, bare]);
  git(root, ["clone", "-q", "--bare", work, remote]);
  writeFileSync(join(bare, "config"), "[core]\nrepositoryformatversion = 0\nbare = true\n");
  const owner = { contract: "leaf.project-repository-source-initializer.v1", tenant_id: a.tenantId,
    organization_id: a.organizationId, project_id: a.projectId, repo_key: a.repoKey };
  writeFileSync(join(bare, ".leaf-source-owner.json"), JSON.stringify(owner, Object.keys(owner).sort()));
  const base = git(bare, ["rev-parse", "main"]);
  const repo = new TenantChangeRepo({ repoDir: bare, workBase: join(root, "changes"),
    identity: { name: "test", email: "test@leaf.invalid" } });
  function stage(text = "after\n"): TenantChangeSet {
    const change = repo.createOrResume(randomUUID(), base);
    writeFileSync(join(change.dir, "existing.txt"), text);
    repo.stageCommitWitness(change, "change");
    repo.cleanupWorktree(change);
    return change;
  }
  const credentials = vi.fn(async (bound: ProjectRepositoryAuthority, remoteUrl: string) => ({ authority: bound, remoteUrl }));
  const options: ProjectForgeAuthorityOptions = { allowLocalPathsForTests: true,
    locate: async () => remote, credentials };
  return { root, bare, bareBase, remote, base, repo, stage, credentials, options };
}
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true });
});

describe("project Forge authority with real bare repositories", () => {
  it("proves publication before cache advance, preserves private refs and cache metadata", async () => {
    const f = fixture();
    const change = f.stage();
    const config = readFileSync(join(f.bare, "config"), "utf8");
    const marker = readFileSync(join(f.bare, ".leaf-source-owner.json"), "utf8");
    const bound = new ProjectForgeAuthority(f.options).bind(authority, f.bare);
    expect(await bound.publishAuthoritatively(change, f.base)).toEqual({ commit: change.stagedSha });
    expect(git(f.remote, ["rev-parse", "main"])).toBe(change.stagedSha);
    expect(f.repo.readRef("refs/heads/main")).toBe(f.base);
    await bound.refreshMain();
    expect(f.repo.readRef("refs/heads/main")).toBe(change.stagedSha);
    expect(f.repo.readRef(change.ref)).toBe(change.stagedSha);
    expect(readFileSync(join(f.bare, "config"), "utf8")).toBe(config);
    expect(readFileSync(join(f.bare, ".leaf-source-owner.json"), "utf8")).toBe(marker);
  });

  it("allows exactly one expected-head race winner", async () => {
    const f = fixture();
    const changes = [f.stage("one\n"), f.stage("two\n")];
    const bound = new ProjectForgeAuthority(f.options).bind(authority, f.bare);
    const results = await Promise.allSettled(changes.map(c => bound.publishAuthoritatively(c, f.base)));
    expect(results.filter(r => r.status === "fulfilled")).toHaveLength(1);
    expect(results.filter(r => r.status === "rejected")).toHaveLength(1);
    expect(f.repo.readRef("refs/heads/main")).toBe(f.base);
  });

  it("rechecks revoked credentials on an accepted retry and refresh", async () => {
    const f = fixture();
    let revoked = false;
    const credentials = vi.fn(async (a: ProjectRepositoryAuthority, remoteUrl: string) =>
      revoked ? null : { authority: a, remoteUrl });
    const bound = new ProjectForgeAuthority({ ...f.options, credentials }).bind(authority, f.bare);
    const change = f.stage();
    await bound.publishAuthoritatively(change, f.base);
    revoked = true;
    await expect(bound.publishAuthoritatively(change, f.base)).rejects.toMatchObject({ state: "refused" });
    await expect(bound.refreshMain()).rejects.toMatchObject({ state: "refused" });
    expect(credentials).toHaveBeenCalledTimes(3);
    expect(f.repo.readRef("refs/heads/main")).toBe(f.base);
    revoked = false;
    await expect(bound.publishAuthoritatively(change, f.base)).resolves.toEqual({ commit: change.stagedSha });
  });

  it("keeps same-tenant projects on distinct origins and rejects cross-project credentials", async () => {
    const other = { ...authority, projectId: randomUUID(), repoKey: randomUUID() };
    const first = fixture();
    const second = fixture(other);
    const forge = new ProjectForgeAuthority({ allowLocalPathsForTests: true,
      locate: async a => a.repoKey === authority.repoKey ? first.remote : second.remote,
      credentials: async (a, remoteUrl) => ({ authority: a, remoteUrl }) });
    const one = first.stage("one\n");
    const two = second.stage("two\n");
    await forge.bind(authority, first.bare).publishAuthoritatively(one, first.base);
    await forge.bind(other, second.bare).publishAuthoritatively(two, second.base);
    expect(git(first.remote, ["rev-parse", "main"])).toBe(one.stagedSha);
    expect(git(second.remote, ["rev-parse", "main"])).toBe(two.stagedSha);
    const wrong = new ProjectForgeAuthority({ ...second.options,
      credentials: async (_a, remoteUrl) => ({ authority, remoteUrl }) });
    await expect(wrong.bind(other, second.bare).publishAuthoritatively(two, second.base))
      .rejects.toMatchObject({ state: "refused" });
  });

  it("keeps lost readback unknown without advancing local main, then recovers exact acceptance", async () => {
    const f = fixture();
    class LostReadback extends ProjectForgeAuthority {
      lost = false;
      override async execute(args: string[], cwd: string, env: NodeJS.ProcessEnv): Promise<string> {
        if (this.lost && args.includes("ls-remote") && args.at(-1) === "refs/heads/main") {
          throw new Error("secret-provider-detail");
        }
        const result = await super.execute(args, cwd, env);
        if (args.includes("push") && args.at(-1)?.endsWith(":refs/heads/main")) this.lost = true;
        return result;
      }
    }
    const forge = new LostReadback(f.options);
    const bound = forge.bind(authority, f.bare);
    const change = f.stage();
    await expect(bound.publishAuthoritatively(change, f.base)).rejects.toMatchObject({
      state: "unknown", message: "project Forge publication unknown" });
    expect(f.repo.readRef("refs/heads/main")).toBe(f.base);
    expect(git(f.remote, ["rev-parse", "main"])).toBe(change.stagedSha);
    forge.lost = false;
    await expect(bound.publishAuthoritatively(change, f.base)).resolves.toEqual({ commit: change.stagedSha });
    expect(f.credentials).toHaveBeenCalledTimes(2);
  });

  it("refuses wrong staged identities and does not rewind a divergent cache", async () => {
    const f = fixture();
    const bound = new ProjectForgeAuthority(f.options).bind(authority, f.bare);
    const change = f.stage();
    for (const bad of [{ ...change, id: "bad" }, { ...change, ref: "refs/heads/main" },
      { ...change, stagedSha: f.base }, { ...change, expectedBaseSha: "f".repeat(40) }]) {
      await expect(bound.publishAuthoritatively(bad, f.base)).rejects.toBeInstanceOf(ProjectForgePublicationError);
    }
    f.repo.publishToMainObserved(change, f.base);
    await expect(bound.refreshMain()).rejects.toBeInstanceOf(ProjectForgePublicationError);
    expect(f.repo.readRef("refs/heads/main")).toBe(change.stagedSha);
  });

  it("copies closed authority and discards supplier errors", async () => {
    const f = fixture();
    const mutable = { ...authority };
    const bound = new ProjectForgeAuthority(f.options).bind(mutable, f.bare);
    mutable.projectId = randomUUID();
    await bound.refreshMain();
    expect(f.credentials.mock.calls[0]![0]).toEqual(authority);
    expect(() => new ProjectForgeAuthority(f.options).bind({ ...authority, extra: true } as ProjectRepositoryAuthority, f.bare))
      .toThrow("project Forge publication refused");
    const refused = new ProjectForgeAuthority({ ...f.options, locate: async () => { throw new Error("secret"); } });
    await expect(refused.bind(authority, f.bare).refreshMain()).rejects.toMatchObject({ message: "project Forge publication refused" });
  });

  it("rechecks the exact origin on an accepted retry", async () => {
    const f = fixture();
    let remote = f.remote;
    const forge = new ProjectForgeAuthority({ ...f.options, locate: async () => remote });
    const bound = forge.bind(authority, f.bare);
    const change = f.stage();
    await bound.publishAuthoritatively(change, f.base);
    remote = join(f.root, "other.git");
    await expect(bound.publishAuthoritatively(change, f.base)).rejects.toMatchObject({ state: "refused" });
    expect(f.repo.readRef("refs/heads/main")).toBe(f.base);
  });

  it("keeps scoped auth out of argv and isolates ambient Git and proxy settings", async () => {
    const f = fixture();
    const token = "fixture_scoped_token";
    let networkCalls = 0;
    class InspectTransport extends ProjectForgeAuthority {
      override async execute(args: string[], cwd: string, env: NodeJS.ProcessEnv): Promise<string> {
        expect(args.join(" ")).not.toContain(token);
        expect(args.join(" ")).not.toContain(Buffer.from(`oauth2:${token}`).toString("base64"));
        expect(env).not.toHaveProperty("HTTPS_PROXY");
        expect(env).not.toHaveProperty("GIT_TRACE");
        expect(env).not.toHaveProperty("GIT_CONFIG_PARAMETERS");
        expect(env.GIT_CONFIG_NOSYSTEM).toBe("1");
        expect(env.GIT_TERMINAL_PROMPT).toBe("0");
        if (env.GIT_OBJECT_DIRECTORY) {
          networkCalls++;
          expect(cwd).not.toBe(f.bare);
          expect(env.GIT_CONFIG_VALUE_1).toBe("false");
          expect(env.GIT_CONFIG_VALUE_2).toBe("true");
          expect(env.GIT_CONFIG_VALUE_4).toBe(`Authorization: Basic ${Buffer.from(`oauth2:${token}`).toString("base64")}`);
        }
        return super.execute(args, cwd, env);
      }
    }
    const forge = new InspectTransport({ ...f.options, credentials: async (a, remoteUrl) => ({ authority: a, remoteUrl, token }) });
    await forge.bind(authority, f.bare).publishAuthoritatively(f.stage(), f.base);
    expect(networkCalls).toBeGreaterThan(0);
  });
});

describe("trusted project Forge environment mapping", () => {
  function config(entries: unknown[] = [{ authority, remoteUrl: "https://forge.leafdesign.ai/team/project.git" }]) {
    const dir = temporary();
    const map = join(dir, "origins.json");
    writeFileSync(map, JSON.stringify({ version: 1, repositories: entries }));
    return { dir, map, env: { LEAF_FORGE_ORIGIN_MAP: map, LEAF_FORGE_CREDENTIAL_DIR: dir } };
  }
  it("is absent only with neither setting, and rejects partial or invalid topology", () => {
    expect(createProjectForgeAuthorityFromEnv({})).toBeUndefined();
    expect(() => createProjectForgeAuthorityFromEnv({ LEAF_FORGE_ORIGIN_MAP: "" })).toThrow();
    const c = config();
    expect(createProjectForgeAuthorityFromEnv(c.env)).toBeInstanceOf(ProjectForgeAuthority);
    expect(() => createProjectForgeAuthorityFromEnv({ ...c.env, LEAF_FORGE_CREDENTIAL_DIR: "relative" })).toThrow();
    for (const remoteUrl of ["http://forge.leafdesign.ai/a/b.git", "https://elsewhere.test/a/b.git",
      "https://forge.leafdesign.ai/a/../b.git", "https://forge.leafdesign.ai/a/b.git?q=1",
      "https://user:token@forge.leafdesign.ai/a/b.git", c.dir]) {
      expect(() => createProjectForgeAuthorityFromEnv(config([{ authority, remoteUrl }]).env)).toThrow();
    }
    const first = { authority, remoteUrl: "https://forge.leafdesign.ai/a/b.git" };
    for (const second of [first, { ...first, authority: { ...authority, projectId: randomUUID() } },
      { ...first, authority: { ...authority, projectId: randomUUID(), repoKey: randomUUID() } }]) {
      expect(() => createProjectForgeAuthorityFromEnv(config([first, second]).env)).toThrow();
    }
  });

  it("loads mapping once and reads revoked, malformed and misbound credentials anew", async () => {
    const c = config();
    const forge = createProjectForgeAuthorityFromEnv(c.env)!;
    const bound = forge.bind(authority, c.dir);
    const credential = join(c.dir, `${authority.repoKey}.json`);
    writeFileSync(c.map, "invalid after startup");
    await expect(bound.refreshMain()).rejects.toMatchObject({ state: "refused" });
    for (const value of ["invalid", JSON.stringify({ authority: { ...authority, projectId: randomUUID() },
      remoteUrl: "https://forge.leafdesign.ai/team/project.git", token: "scoped" }),
      JSON.stringify({ authority, remoteUrl: "https://forge.leafdesign.ai/team/other.git", token: "scoped" })]) {
      writeFileSync(credential, value);
      await expect(bound.refreshMain()).rejects.toMatchObject({ state: "refused" });
    }
    // A valid credential gets past supplier validation to the absent local cache,
    // without contacting HTTPS. This also proves that the map was loaded once.
    writeFileSync(credential, JSON.stringify({ authority, remoteUrl: "https://forge.leafdesign.ai/team/project.git", token: "scoped" }));
    await expect(bound.refreshMain()).rejects.toMatchObject({ state: "unknown" });
    rmSync(credential);
    await expect(bound.refreshMain()).rejects.toMatchObject({ state: "refused" });
  });
});

function coordinatedFixture() {
  const f = fixture();
  let fenceNumber = 0;
  let activeFence = 0;
  let loseLocalFence = false;
  let remoteFence = 0;
  let localFence = 0;
  const lease = { async withProjectLease<T>(_a: ProjectRepositoryAuthority,
    action: (w: WriterLeaseWitness, fence: TenantMutationFence) => Promise<T>): Promise<T> {
    return action({ writerLeaseId: leaseId, writerLeaseGeneration: "8" }, async operation => {
      activeFence = ++fenceNumber;
      if (loseLocalFence && remoteFence) throw new Error("lease lost");
      try { return await operation(); } finally { activeFence = 0; }
    });
  } } as unknown as PgTenantRepoLeaseCoordinator;
  const provider = new TenantRepoProviderImpl({ locator: { async repoRef() { throw new Error("tenant fallback forbidden"); } },
    bareBase: f.bareBase, workBase: join(f.root, "changes"), lease, authoringMode: "singleton",
    projectRemoteAuthority: new ProjectForgeAuthority(f.options) });
  const change = f.stage();
  const tree = f.repo.resolveCommitTree(change.stagedSha!);
  const digest = "a".repeat(64);
  const coordination: ProjectRepositoryEditCoordination = {
    recordStaged: vi.fn(async () => ({ contract, action: "record_staged", edit_id: change.id, state: "staged", version: 1 })),
    authorizePublish: vi.fn(async () => ({ contract, action: "authorize_publish", edit_id: change.id, state: "publishing", version: 2,
      receipt_digest: digest, expected_main_commit: f.base, staged_head_commit: change.stagedSha!, staged_tree: tree,
      private_ref: change.ref, publish_lease_id: leaseId, publish_lease_generation: 8 })),
    settlePublish: vi.fn(async () => ({ contract, action: "settle_publish", edit_id: change.id, state: "published", version: 3 })),
    recoverPublish: vi.fn(async () => ({ contract, action: "recover_publish", edit_id: change.id, state: "published", version: 3 })),
  };
  const service = new ProjectRepositoryEditCoordinator({ leases: provider, coordination, changeRepo: a => {
    const repo = provider.projectChangeRepo(a);
    const remote = repo.publishAuthoritatively!;
    repo.publishAuthoritatively = async (c, expected) => { remoteFence = activeFence; return remote(c, expected); };
    const local = repo.publishToMainObserved.bind(repo);
    repo.publishToMainObserved = (c, expected) => { localFence = activeFence; return local(c, expected); };
    return repo;
  } });
  const publish = { authority, editId: change.id, actorBindingId: actor, confirmationId: confirmation,
    receiptDigest: digest, expectedVersion: 1, transitionKey: "publish" };
  const recover = { authority, editId: change.id, actorBindingId: actor, expectedMainCommit: f.base,
    stagedHeadCommit: change.stagedSha!, stagedTree: tree, expectedVersion: 2, transitionKey: "recover", reasonCode: "interrupted" };
  return { ...f, provider, change, service, coordination, publish, recover,
    fences: () => ({ remoteFence, localFence }), loseFence: () => { loseLocalFence = true; } };
}

describe("project coordinator remote publication fences", () => {
  it("uses separate remote and local fences", async () => {
    const f = coordinatedFixture();
    await f.service.publishEdit(f.publish);
    expect(f.fences().remoteFence).toBeGreaterThan(0);
    expect(f.fences().localFence).toBeGreaterThan(f.fences().remoteFence);
    expect(f.repo.readRef("refs/heads/main")).toBe(f.change.stagedSha);
  });
  it("preserves remote acceptance when the local fence is lost", async () => {
    const f = coordinatedFixture();
    f.loseFence();
    await expect(f.service.publishEdit(f.publish)).rejects.toThrow("lease lost");
    expect(git(f.remote, ["rev-parse", "main"])).toBe(f.change.stagedSha);
    expect(f.repo.readRef("refs/heads/main")).toBe(f.base);
    expect(f.coordination.settlePublish).not.toHaveBeenCalled();
  });
  it("rechecks remote proof for alreadyPublished recovery and resumes an accepted remote", async () => {
    const f = coordinatedFixture();
    await new ProjectForgeAuthority(f.options).bind(authority, f.bare).publishAuthoritatively(f.change, f.base);
    await f.service.recoverEdit(f.recover);
    expect(f.fences().localFence).toBeGreaterThan(f.fences().remoteFence);
    const calls = f.credentials.mock.calls.length;
    await f.service.recoverEdit(f.recover);
    expect(f.credentials.mock.calls.length).toBe(calls + 1);
    f.credentials.mockImplementation(async () => { throw new Error("revoked"); });
    await expect(f.service.recoverEdit(f.recover)).rejects.toMatchObject({ state: "refused" });
    expect(f.coordination.recoverPublish).toHaveBeenCalledTimes(2);
  });
  it("does not publish an unrelated recovery matrix", async () => {
    const f = coordinatedFixture();
    const other = f.stage("unrelated\n");
    f.repo.publishToMainObserved(other, f.base);
    await f.service.recoverEdit(f.recover);
    expect(f.credentials).not.toHaveBeenCalled();
    expect(git(f.remote, ["rev-parse", "main"])).toBe(f.base);
  });
});

describe("live service composition helper", () => {
  it("mounts the actual provider, project lease and coordination client in a working HTTP stage route", async () => {
    const f = coordinatedFixture();
    expect(createProjectRepositoryEditsForService(f.provider, {})).toBeUndefined();
    const nativeFetch = globalThis.fetch;
    const editId = randomUUID();
    const dispatch = vi.fn(async () => new Response(JSON.stringify({ contract, action: "record_staged",
      edit_id: editId, state: "staged", version: 1 }), { status: 200, headers: { "content-type": "application/json" } }));
    vi.stubGlobal("fetch", ((input: Parameters<typeof fetch>[0], init?: Parameters<typeof fetch>[1]) =>
      String(input).startsWith("https://coordination.invalid/") ? dispatch() : nativeFetch(input, init)) as typeof fetch);
    const projectRepositoryEdits = createProjectRepositoryEditsForService(f.provider,
      { LEAF_APP_URL: "https://coordination.invalid", LEAF_APP_DISPATCH_SECRET: "test-dispatch" });
    const server = createHarness({ tenantRepo: f.provider, agentRunner: new FakeAgentRunner(),
      broker: new FakeBrokerApsClient(), oauth: new FakeOAuthGrantProvider() },
      { auth: { enabled: true, secret: "test-harness" }, projectRepositoryEdits }).listen(0);
    try {
      const bytes = "new product\n";
      const response = await nativeFetch(`http://127.0.0.1:${(server.address() as AddressInfo).port}/internal/project-repository-source/stage`,
        { method: "POST", headers: { "content-type": "application/json", "x-harness-secret": "test-harness", "x-tenant-id": authority.tenantId },
          body: JSON.stringify({ tenant_id: authority.tenantId, organization_id: authority.organizationId,
            project_id: authority.projectId, repo_key: authority.repoKey, edit_id: editId, actor_binding_id: actor,
            expected_base_commit: f.base, instruction_digest: "a".repeat(64), idempotency_key: "runtime-stage",
            commit_message: "product", files: [{ path: "new.txt", key: "products/new.txt", sha256: hash(bytes),
              size_bytes: Buffer.byteLength(bytes), product_b64: Buffer.from(bytes).toString("base64"), before_sha256: null }] }) });
      expect(response.status).toBe(200);
      expect(await response.json()).toMatchObject({ receipt: { project_id: authority.projectId, state: "staged" } });
      expect(dispatch).toHaveBeenCalledTimes(1);
      expect(f.credentials).toHaveBeenCalledTimes(1);
      expect(f.repo.readRef("refs/heads/main")).toBe(f.base);
    } finally { await new Promise<void>(resolve => server.close(() => resolve())); }
  });
});
