import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { AddressInfo } from "node:net";
import type { Server } from "node:http";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createHarness } from "../src/server.js";
import { ProjectRepositoryEditCoordinator, type StagedProjectRepositoryEdit } from "../src/agent/projectRepositoryEditCoordinator.js";
import { TenantRepoProviderImpl, type PgTenantRepoLeaseCoordinator } from "../src/ports/impl/tenantRepoProvider.js";
import { TenantChangeRepo } from "../src/vendor/mushy-author/ports/impl/tenantChangeRepo.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT as contract,
  type HarnessPorts, type ProjectRepositoryAuthority, type ProjectRepositoryEditCoordination,
  type ProjectRepositoryStagedReceipt, type TenantMutationFence, type WriterLeaseWitness } from "../src/ports/index.js";

const authority: ProjectRepositoryAuthority = {
  tenantId: "11111111-1111-4111-8111-111111111111", organizationId: "22222222-2222-4222-8222-222222222222",
  projectId: "33333333-3333-4333-8333-333333333333", repoKey: "44444444-4444-4444-8444-444444444444",
};
const editId = "55555555-5555-4555-8555-555555555555";
const actor = "66666666-6666-4666-8666-666666666666";
const confirmation = "77777777-7777-4777-8777-777777777777";
const secret = "campaign-source-test-secret";
const hash = (value: Buffer | string) => createHash("sha256").update(value).digest("hex");
const common = { tenant_id: authority.tenantId, organization_id: authority.organizationId,
  project_id: authority.projectId, repo_key: authority.repoKey, edit_id: editId, actor_binding_id: actor };
function git(dir: string, args: string[]): string {
  return execFileSync("git", args, { cwd: dir, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
}
function file(path: string, bytes: string, before: string | null = null) {
  return { path, key: `products/${path}`, sha256: hash(bytes), size_bytes: Buffer.byteLength(bytes),
    product_b64: Buffer.from(bytes).toString("base64"), before_sha256: before === null ? null : hash(before) };
}

describe("mounted campaign product source transaction (real bare Git, component coordination transport)", () => {
  let root: string;
  let bare: string;
  let base: string;
  let url: string;
  let server: Server;
  let ports: HarnessPorts;
  let provider: TenantRepoProviderImpl;
  let receipt: ProjectRepositoryStagedReceipt;
  let receiptDigest: string;
  let loseSettlement: boolean;
  let coordination: ProjectRepositoryEditCoordination;
  let generation: number;

  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), "campaign-source-"));
    const work = join(root, "work");
    const bareBase = join(root, "bare");
    mkdirSync(bareBase);
    bare = join(bareBase, `${authority.repoKey}.git`);
    execFileSync("git", ["init", "-q", "-b", "main", work]);
    writeFileSync(join(work, "existing.txt"), "before\n");
    git(work, ["add", "."]);
    git(work, ["-c", "user.name=test", "-c", "user.email=test@leaf.invalid", "commit", "-qm", "base"]);
    base = git(work, ["rev-parse", "HEAD"]);
    execFileSync("git", ["clone", "-q", "--bare", work, bare]);
    const owner = { contract: "leaf.project-repository-source-initializer.v1", tenant_id: authority.tenantId,
      organization_id: authority.organizationId, project_id: authority.projectId, repo_key: authority.repoKey };
    writeFileSync(join(bare, ".leaf-source-owner.json"), JSON.stringify(owner, Object.keys(owner).sort()));
    generation = 0;
    const lease = { async withProjectLease<T>(_authority: ProjectRepositoryAuthority,
      action: (w: WriterLeaseWitness, fence: TenantMutationFence) => Promise<T>): Promise<T> {
      return action({ writerLeaseId: "88888888-8888-4888-8888-888888888888", writerLeaseGeneration: String(++generation) },
        async operation => operation());
    } } as unknown as PgTenantRepoLeaseCoordinator;
    provider = new TenantRepoProviderImpl({ locator: { async repoRef() { throw new Error("tenant checkout forbidden"); } },
      bareBase, workBase: join(root, "changes"), lease, authoringMode: "singleton" });
    loseSettlement = false;
    coordination = {
      recordStaged: vi.fn(async (request: Parameters<ProjectRepositoryEditCoordination["recordStaged"]>[0]) => {
        receipt = request.receipt;
        receiptDigest = request.receipt_digest;
        return { contract, action: "record_staged", edit_id: editId, state: "staged", version: 1 } as const;
      }),
      authorizePublish: vi.fn(async (request: Parameters<ProjectRepositoryEditCoordination["authorizePublish"]>[0]) => {
        if (request.actor_binding_id !== actor || request.receipt_digest !== receiptDigest) throw new Error("authority denied");
        return { contract, action: "authorize_publish", edit_id: editId, state: "publishing", version: 2,
          receipt_digest: receiptDigest, expected_main_commit: receipt.base_commit,
          staged_head_commit: receipt.staged_head_commit, staged_tree: receipt.staged_tree,
          private_ref: `refs/leaf/changes/${editId}`, publish_lease_id: request.publish_lease_id,
          publish_lease_generation: request.publish_lease_generation } as const;
      }),
      settlePublish: vi.fn(async () => {
        if (loseSettlement) throw new Error("lost settlement");
        return { contract, action: "settle_publish", edit_id: editId, state: "published", version: 3 } as const;
      }),
      recoverPublish: vi.fn(async () => ({ contract, action: "recover_publish", edit_id: editId, state: "published", version: 3 } as const)),
    };
    ports = { tenantRepo: provider, agentRunner: new FakeAgentRunner(), broker: new FakeBrokerApsClient(), oauth: new FakeOAuthGrantProvider() };
    const service = new ProjectRepositoryEditCoordinator({ leases: provider, changeRepo: a => provider.projectChangeRepo(a), coordination });
    server = createHarness(ports, { auth: { enabled: false, secret }, projectRepositoryEdits: service }).listen(0);
    url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  });
  afterEach(async () => {
    await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
    vi.restoreAllMocks();
    rmSync(root, { recursive: true, force: true });
  });
  const stageBody = (base: string) => ({ ...common, expected_base_commit: base, instruction_digest: "a".repeat(64),
    idempotency_key: "stage-product", commit_message: "verified product", files: [file("existing.txt", "after\n", "before\n"), file("new.bin", "\0new\0")] });
  const publishBody = (digest: string) => ({ ...common, confirmation_id: confirmation,
    receipt_digest: digest, expected_version: 1, transition_key: "publish-product" });
  async function post(action: string, body: unknown, headers: Record<string, string> = {}) {
    return fetch(`${url}/internal/project-repository-source/${action}`, { method: "POST",
      headers: { "content-type": "application/json", "x-harness-secret": secret, "x-tenant-id": authority.tenantId, ...headers },
      body: JSON.stringify(body) });
  }

  it("stages only verified bytes and binds the actual binary diff, then publishes once", async () => {
    const publish = vi.spyOn(TenantChangeRepo.prototype, "publishToMainObserved");
    const response = await post("stage", stageBody(base));
    expect(response.status).toBe(200);
    const staged = await response.json() as StagedProjectRepositoryEdit;
    expect(staged.receipt.changed_paths).toEqual(["existing.txt", "new.bin"]);
    expect(git(bare, ["rev-parse", "main"])).toBe(base);
    const actualDiff = execFileSync("git", ["-c", "diff.algorithm=myers", "-c", "diff.indentHeuristic=false",
      "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--binary", "--full-index", "--no-color",
      "--src-prefix=a/", "--dst-prefix=b/", "--unified=3", base, staged.receipt.staged_head_commit, "--"], { cwd: bare });
    expect(staged.receipt.diff_digest).toBe(hash(actualDiff));
    expect(git(bare, ["show", `${staged.receipt.staged_head_commit}:existing.txt`])).toBe("after");
    expect((await post("publish", publishBody(staged.receiptDigest))).status).toBe(200);
    expect(git(bare, ["rev-parse", "main"])).toBe(staged.receipt.staged_head_commit);
    expect(publish).toHaveBeenCalledTimes(1);
  });

  it("returns landed Git truth on lost settlement and recovers without another publish", async () => {
    const publish = vi.spyOn(TenantChangeRepo.prototype, "publishToMainObserved");
    const staged = await (await post("stage", stageBody(base))).json() as StagedProjectRepositoryEdit;
    loseSettlement = true;
    const failed = await post("publish", publishBody(staged.receiptDigest));
    expect(failed.status).toBe(503);
    const failure = await failed.json() as { observation: { after_main_commit: string }; settleRequest: { expected_version: number } };
    expect(failure.observation.after_main_commit).toBe(staged.receipt.staged_head_commit);
    const recovered = await post("recover", { ...common, expected_main_commit: base,
      staged_head_commit: staged.receipt.staged_head_commit, staged_tree: staged.receipt.staged_tree,
      expected_version: failure.settleRequest.expected_version, transition_key: "recover-product", reason_code: "settlement_unavailable" });
    expect(recovered.status).toBe(200);
    expect(await recovered.json()).toMatchObject({ compareAndSwap: false, settlement: { state: "published" } });
    expect(publish).toHaveBeenCalledTimes(1);
    expect(coordination.authorizePublish).toHaveBeenCalledTimes(1);
  });

  it("forces authentication and tenant equality and fails closed when the service is absent", async () => {
    expect((await post("stage", stageBody(base), { "x-harness-secret": "wrong" })).status).toBe(401);
    expect((await post("stage", stageBody(base), { "x-tenant-id": actor })).status).toBe(409);
    const absent = createHarness(ports, { auth: { enabled: false, secret } }).listen(0);
    try {
      const response = await fetch(`http://127.0.0.1:${(absent.address() as AddressInfo).port}/internal/project-repository-source/stage`,
        { method: "POST", headers: { "x-harness-secret": secret }, body: "{}" });
      expect(response.status).toBe(503);
    } finally { await new Promise<void>(resolve => absent.close(() => resolve())); }
    expect(coordination.recordStaged).not.toHaveBeenCalled();
  });

  it("rejects duplicate nested and escaped keys before staging", async () => {
    for (const body of [
      JSON.stringify(stageBody(base)).replace('"path":"existing.txt"', '"path":"existing.txt","path":"other.txt"'),
      JSON.stringify(stageBody(base)).replace('"tenant_id":', '"tenant_id":"bad","tenant_\\u0069d":'),
    ]) {
      const response = await fetch(`${url}/internal/project-repository-source/stage`, { method: "POST",
        headers: { "content-type": "application/json", "x-harness-secret": secret, "x-tenant-id": authority.tenantId }, body });
      expect(response.status).toBe(400);
    }
    expect(coordination.recordStaged).not.toHaveBeenCalled();
  });

  it("preserves existing executable modes and leaves capability paths available", async () => {
    const blob = git(bare, ["rev-parse", `${base}:existing.txt`]);
    const tree = execFileSync("git", ["mktree"], { cwd: bare, input: `100755 blob ${blob}\texisting.txt\n`, encoding: "utf8" }).trim();
    const commit = git(bare, ["-c", "user.name=test", "-c", "user.email=test@leaf.invalid", "commit-tree", tree, "-p", base, "-m", "executable"]);
    git(bare, ["update-ref", "refs/heads/main", commit, base]);
    const response = await post("stage", { ...stageBody(commit),
      files: [file("existing.txt", "after\n", "before\n"), file(".leaf/capabilities/example.json", "{}\n")] });
    expect(response.status).toBe(200);
    const staged = await response.json() as StagedProjectRepositoryEdit;
    expect(git(bare, ["ls-tree", staged.receipt.staged_head_commit, "--", "existing.txt"])).toMatch(/^100755 blob /);
    expect(git(bare, ["ls-tree", staged.receipt.staged_head_commit, "--", ".leaf/capabilities/example.json"])).toMatch(/^100644 blob /);
  });

  it.each(["PROMPT.md", ".leaf/source-seed.json", ".git/config", "findings/pair/edit.json", ".leaf/campaign-plan.json", ".env", "../escape"])("rejects reserved path %s", async path => {
    expect((await post("stage", { ...stageBody(base), files: [file(path, "bad")] })).status).toBe(409);
    expect(git(bare, ["rev-parse", "main"])).toBe(base);
    expect(coordination.recordStaged).not.toHaveBeenCalled();
  });

  it("rejects hash, before hash, mode, count, aggregate, authority and stale base errors", async () => {
    const original = stageBody(base);
    const invalidBodies = [
      { ...original, files: [{ ...original.files[0], sha256: "b".repeat(64) }] },
      { ...original, files: [{ ...original.files[0], before_sha256: "b".repeat(64) }] },
      { ...original, files: [{ ...original.files[0], mode: "100755" }] },
      { ...original, files: [{ ...original.files[0], product_b64: original.files[0].product_b64 + "\n" }] },
      { ...original, files: Array.from({ length: 65 }, (_, i) => file(`f${i}`, "")) },
      { ...original, files: Array.from({ length: 5 }, (_, i) => file(`f${i}`, "x".repeat(1048576))) },
      { ...original, organization_id: actor },
      { ...original, expected_base_commit: "f".repeat(40) },
      { ...original, changed_paths: ["worker-lie"], diff_digest: "b".repeat(64) },
    ];
    for (const body of invalidBodies) expect([400, 409]).toContain((await post("stage", body)).status);
    expect(git(bare, ["rev-parse", "main"])).toBe(base);
    expect(coordination.recordStaged).not.toHaveBeenCalled();
  });

  it("rejects a missing owner marker and changed-byte replay before advancing the private ref", async () => {
    const staged = await (await post("stage", stageBody(base))).json() as StagedProjectRepositoryEdit;
    const replay = { ...stageBody(base), files: [file("existing.txt", "replayed lie\n", "before\n")] };
    expect((await post("stage", replay)).status).toBe(409);
    expect(git(bare, ["rev-parse", `refs/leaf/changes/${editId}`])).toBe(staged.receipt.staged_head_commit);
    rmSync(join(bare, ".leaf-source-owner.json"));
    expect(() => provider.projectChangeRepo(authority)).toThrow();
    expect(git(bare, ["rev-parse", "main"])).toBe(base);
  });

  it("rejects a tracked symlink even on hosts that check it out as an ordinary file", async () => {
    const blob = git(bare, ["rev-parse", `${base}:existing.txt`]);
    const tree = execFileSync("git", ["mktree"], { cwd: bare, input: `120000 blob ${blob}\tlink\n`, encoding: "utf8" }).trim();
    const commit = git(bare, ["-c", "user.name=test", "-c", "user.email=test@leaf.invalid", "commit-tree", tree, "-p", base, "-m", "link"]);
    git(bare, ["update-ref", "refs/heads/main", commit, base]);
    expect((await post("stage", { ...stageBody(commit), files: [file("link", "bad", "before\n")] })).status).toBe(409);
    expect(git(bare, ["rev-parse", "main"])).toBe(commit);
  });
});
