import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createHarness } from "../src/server.js";
import { PROJECT_SOURCE_SCAFFOLD } from "../src/agent/projectSourceAuthoring.js";
import { ProjectRepositoryEditCoordinator, type StagedProjectRepositoryEdit } from "../src/agent/projectRepositoryEditCoordinator.js";
import { TenantRepoProviderImpl, type PgTenantRepoLeaseCoordinator } from "../src/ports/impl/tenantRepoProvider.js";
import { TenantChangeRepo } from "../src/vendor/mushy-author/ports/impl/tenantChangeRepo.js";
import type { RepoEditInput, RepoEditResult } from "../src/vendor/mushy-author/ports/impl/repoEditRunner.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT as contract,
  type HarnessPorts, type OAuthGrantProvider, type ProjectRepositoryAuthority, type ProjectRepositoryEditCoordination,
  type ProjectRepositoryStagedReceipt, type TenantMutationFence, type WriterLeaseWitness } from "../src/ports/index.js";

const authority: ProjectRepositoryAuthority = {
  tenantId: "11111111-1111-4111-8111-111111111111", organizationId: "22222222-2222-4222-8222-222222222222",
  projectId: "33333333-3333-4333-8333-333333333333", repoKey: "44444444-4444-4444-8444-444444444444",
};
const editId = "55555555-5555-4555-8555-555555555555", actor = "66666666-6666-4666-8666-666666666666";
const secret = "source-author-test-secret";
const common = { tenant_id: authority.tenantId, organization_id: authority.organizationId,
  project_id: authority.projectId, repo_key: authority.repoKey, edit_id: editId, actor_binding_id: actor };
const hash = (value: Buffer | string) => createHash("sha256").update(value).digest("hex");
const raw = '{"nested":{"zero":0,"nothing":null,"items":[{"name":"Pear"},{"name":"Apple"}]}}\n';
const page = 'import data from "../public/data/nested/items.json";\nexport default function Page() { return <main>{data.nested.items.map(x => <p key={x.name}>{x.name}</p>)}</main>; }\n';
function git(dir: string, args: string[]): string {
  return execFileSync("git", args, { cwd: dir, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
}
function material(path: string, bytes: string) {
  return { path, key: `metadata/${path}`, sha256: hash(bytes), size_bytes: Buffer.byteLength(bytes), product_b64: Buffer.from(bytes).toString("base64") };
}

describe("mounted first source author transaction with real private Git", () => {
  let root: string, bare: string, base: string, url: string;
  let server: Server, ports: HarnessPorts, service: ProjectRepositoryEditCoordinator;
  let receipt: ProjectRepositoryStagedReceipt, receiptDigest: string;
  let coordination: ProjectRepositoryEditCoordination;
  let edit: ReturnType<typeof vi.fn<(input: RepoEditInput) => Promise<RepoEditResult>>>;
  let oauth: OAuthGrantProvider;
  let generation: number, confirmed: boolean;
  beforeEach(() => {
    vi.stubEnv("LEAF_AUTHORED_EXECUTION", "1");
    root = mkdtempSync(join(tmpdir(), "source-author-"));
    const work = join(root, "work"), bareBase = join(root, "bare");
    mkdirSync(bareBase);
    bare = join(bareBase, `${authority.repoKey}.git`);
    execFileSync("git", ["init", "-q", "-b", "main", work]);
    mkdirSync(join(work, ".leaf"));
    writeFileSync(join(work, ".leaf/source-seed.json"), '{"seed":true}\n');
    writeFileSync(join(work, "PROMPT.md"), "Protected seed\n");
    git(work, ["add", "."]);
    git(work, ["-c", "user.name=test", "-c", "user.email=test@leaf.invalid", "commit", "-qm", "seed"]);
    base = git(work, ["rev-parse", "HEAD"]);
    execFileSync("git", ["clone", "-q", "--bare", work, bare]);
    const owner = { contract: "leaf.project-repository-source-initializer.v1", tenant_id: authority.tenantId,
      organization_id: authority.organizationId, project_id: authority.projectId, repo_key: authority.repoKey };
    writeFileSync(join(bare, ".leaf-source-owner.json"), JSON.stringify(owner, Object.keys(owner).sort()));
    generation = 0;
    confirmed = false;
    const lease = { async withProjectLease<T>(_authority: ProjectRepositoryAuthority,
      action: (w: WriterLeaseWitness, fence: TenantMutationFence) => Promise<T>): Promise<T> {
      return action({ writerLeaseId: "88888888-8888-4888-8888-888888888888", writerLeaseGeneration: String(++generation) },
        async operation => operation());
    } } as unknown as PgTenantRepoLeaseCoordinator;
    const provider = new TenantRepoProviderImpl({ locator: { async repoRef() { throw new Error("no tenant checkout"); } },
      bareBase, workBase: join(root, "changes"), lease, authoringMode: "singleton" });
    coordination = {
      recordStaged: vi.fn(async (request: Parameters<ProjectRepositoryEditCoordination["recordStaged"]>[0]) => {
        receipt = request.receipt; receiptDigest = request.receipt_digest;
        return { contract, action: "record_staged", edit_id: editId, state: "staged", version: 1 } as const;
      }),
      authorizePublish: vi.fn(async (request: Parameters<ProjectRepositoryEditCoordination["authorizePublish"]>[0]) => {
        if (confirmed || request.confirmation_id !== actor || request.actor_binding_id !== actor || request.receipt_digest !== receiptDigest) throw new Error("confirmation denied");
        confirmed = true;
        return { contract, action: "authorize_publish", edit_id: editId, state: "publishing", version: 2,
          receipt_digest: receiptDigest, expected_main_commit: receipt.base_commit, staged_head_commit: receipt.staged_head_commit,
          staged_tree: receipt.staged_tree, private_ref: `refs/leaf/changes/${editId}`,
          publish_lease_id: request.publish_lease_id, publish_lease_generation: request.publish_lease_generation } as const;
      }),
      settlePublish: vi.fn(async () => ({ contract, action: "settle_publish", edit_id: editId, state: "published", version: 3 } as const)),
      recoverPublish: vi.fn(async () => ({ contract, action: "recover_publish", edit_id: editId, state: "published", version: 3 } as const)),
    };
    oauth = { getGrant: vi.fn(async () => ({ kind: "api_key", apiKey: "test-only" } as const)),
      acquireGrant: vi.fn(async () => ({ grant: { kind: "api_key", apiKey: "test-only" } as const, account_id: "account", lease_id: "lease" })),
      settleGrant: vi.fn(async () => {}) };
    edit = vi.fn(async input => {
      expect(input.grant).toEqual({ kind: "api_key", apiKey: "test-only" });
      expect(input.signal).toBeDefined();
      expect(readFileSync(join(input.repoDir, "public/data/nested/items.json"), "utf8")).toBe(raw);
      writeFileSync(join(input.repoDir, "app/page.tsx"), page);
      return { summary: "private", changedFiles: ["deliberate-lie"] };
    });
    ports = { tenantRepo: provider, oauth, agentRunner: new FakeAgentRunner(), broker: new FakeBrokerApsClient() };
    service = new ProjectRepositoryEditCoordinator({ leases: provider, changeRepo: a => provider.projectChangeRepo(a), coordination });
    server = createHarness(ports, { auth: { enabled: false, secret }, projectRepositoryEdits: service,
      projectSourceAuthoring: { oauth, editor: { edit } } }).listen(0);
    url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  });
  afterEach(async () => {
    await new Promise<void>(resolve => server.close(() => resolve()));
    vi.restoreAllMocks(); vi.unstubAllEnvs();
    rmSync(root, { recursive: true, force: true });
  });
  function body() {
    const requirements = "Make a searchable list.\nPreserve zero, null and arrays from the nested input.";
    return { ...common, expected_base_commit: base, instruction_digest: hash(requirements), requirements,
      idempotency_key: "first-author", commit_message: "Author editable project", material: [material("nested/items.json", raw)] };
  }
  async function post(value: unknown, action = "author", headers: Record<string, string> = {}, target = url) {
    return fetch(`${target}/internal/project-repository-source/${action}`, { method: "POST",
      headers: { "content-type": "application/json", "x-harness-secret": secret, "x-tenant-id": authority.tenantId, ...headers }, body: JSON.stringify(value) });
  }
  function unchanged() {
    expect(git(bare, ["rev-parse", "main"])).toBe(base);
    expect(coordination.recordStaged).not.toHaveBeenCalled();
    expect(coordination.authorizePublish).not.toHaveBeenCalled();
  }

  it("commits exact nested material and scaffold, binds actual binary evidence, and publishes only once with confirmation", async () => {
    const publish = vi.spyOn(TenantChangeRepo.prototype, "publishToMainObserved");
    const response = await post(body());
    expect(response.status).toBe(200);
    const staged = await response.json() as StagedProjectRepositoryEdit;
    const head = staged.receipt.staged_head_commit;
    expect(git(bare, ["rev-parse", `refs/leaf/changes/${editId}`])).toBe(head);
    expect(git(bare, ["rev-parse", "main"])).toBe(base);
    const bytes = (path: string) => execFileSync("git", ["show", `${head}:${path}`], { cwd: bare });
    expect(bytes("public/data/nested/items.json")).toEqual(Buffer.from(raw));
    expect(hash(bytes("public/data/nested/items.json"))).toBe(body().material[0].sha256);
    expect(bytes("app/page.tsx")).toEqual(Buffer.from(page));
    for (const [path, content] of Object.entries(PROJECT_SOURCE_SCAFFOLD)) expect(bytes(path)).toEqual(Buffer.from(content));
    expect(bytes(".leaf/source-seed.json").toString()).toBe('{"seed":true}\n');
    expect(bytes("PROMPT.md").toString()).toBe("Protected seed\n");
    expect(JSON.parse(bytes("package.json").toString()).dependencies).toEqual({ next: "16.4.3", react: "18.3.1", "react-dom": "18.3.1" });
    expect(JSON.parse(bytes("package.json").toString()).scripts).toEqual({ build: "next build", dev: "next dev", start: "next start" });
    expect(bytes("next.config.mjs").toString()).toBe('export default { output: "export", images: { unoptimized: true } };\n');
    expect(staged.receipt.changed_paths).toEqual([...Object.keys(PROJECT_SOURCE_SCAFFOLD), "app/page.tsx", "public/data/nested/items.json"].sort());
    const diff = execFileSync("git", ["-c", "diff.algorithm=myers", "-c", "diff.indentHeuristic=false", "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
      "--binary", "--full-index", "--no-color", "--src-prefix=a/", "--dst-prefix=b/", "--unified=3", base, head, "--"], { cwd: bare });
    expect(staged.receipt.diff_digest).toBe(hash(diff));
    expect(staged.receipt.instruction_digest).toBe(body().instruction_digest);
    expect(oauth.acquireGrant).toHaveBeenCalledWith(authority.tenantId);
    expect(oauth.getGrant).not.toHaveBeenCalled();
    expect(oauth.settleGrant).toHaveBeenCalledWith(authority.tenantId, "lease", { usage: { cost_tokens: 0 }, stop_reason: "end_turn" });
    expect(publish).not.toHaveBeenCalled();
    const request = { ...common, confirmation_id: actor, receipt_digest: staged.receiptDigest, expected_version: 1, transition_key: "publish" };
    expect((await post(request, "publish")).status).toBe(200);
    expect((await post(request, "publish")).status).toBe(409);
    expect(git(bare, ["rev-parse", "main"])).toBe(head);
    expect(publish).toHaveBeenCalledTimes(1);
  });

  it("passes a different calculator requirement and CSV through the same generic editor", async () => {
    const requirements = "Make a client-side price calculator with quantity input.";
    const csv = "name,price\nWidget,3.5\n";
    const calculator = '"use client";\nimport {useState} from "react";\nexport default function Page(){const [q,setQ]=useState(0);return <main><input type="number" value={q} onChange={e=>setQ(Number(e.target.value))}/><output>{q*3.5}</output></main>;}\n';
    edit.mockImplementation(async input => {
      expect(input.instruction).toBe(requirements);
      expect(readFileSync(join(input.repoDir, "public/data/prices.csv"), "utf8")).toBe(csv);
      writeFileSync(join(input.repoDir, "app/page.tsx"), calculator);
      return { summary: "done", changedFiles: [] };
    });
    const response = await post({ ...body(), requirements, instruction_digest: hash(requirements), material: [material("prices.csv", csv)] });
    expect(response.status).toBe(200);
    const staged = await response.json() as StagedProjectRepositoryEdit;
    expect(execFileSync("git", ["show", `${staged.receipt.staged_head_commit}:app/page.tsx`], { cwd: bare }).toString()).toBe(calculator);
    expect(git(bare, ["rev-parse", "main"])).toBe(base);
  });

  it("requires authentication, matching tenant, runtime enablement and both dependencies before model work", async () => {
    expect((await post(body(), "author", { "x-harness-secret": "" })).status).toBe(401);
    expect((await post(body(), "author", { "x-tenant-id": actor })).status).toBe(409);
    vi.stubEnv("LEAF_AUTHORED_EXECUTION", "0");
    expect((await post(body())).status).toBe(503);
    vi.stubEnv("LEAF_AUTHORED_EXECUTION", "1");
    for (const options of [{}, { projectRepositoryEdits: service }, { projectSourceAuthoring: { oauth, editor: { edit } } }]) {
      const absent = createHarness(ports, { auth: { enabled: false, secret }, ...options }).listen(0);
      try { expect((await post(body(), "author", {}, `http://127.0.0.1:${(absent.address() as AddressInfo).port}`)).status).toBe(503); }
      finally { await new Promise<void>(resolve => absent.close(() => resolve())); }
    }
    expect(edit).not.toHaveBeenCalled(); expect(oauth.acquireGrant).not.toHaveBeenCalled(); unchanged();
  });

  it("validates the complete closed request before acquiring a grant", async () => {
    const original = body(), first = original.material[0];
    const invalid = [
      { ...original, instruction_digest: "a".repeat(64) }, { ...original, requirements: "" },
      { ...original, requirements: "bad\0", instruction_digest: hash("bad\0") },
      { ...original, requirements: "a".repeat(16385), instruction_digest: hash("a".repeat(16385)) },
      { ...original, extra: true }, { ...original, tenant_id: "bad" }, { ...original, material: [] },
      { ...original, material: [{ ...first, product_b64: first.product_b64 + "\n" }] },
      { ...original, material: [{ ...first, sha256: "b".repeat(64) }] },
      { ...original, material: [{ ...first, size_bytes: first.size_bytes + 1 }] },
      { ...original, material: [first, { ...first, path: "NESTED/items.json" }] },
      { ...original, material: [material("folder.json", "{}"), material("folder.json/item.txt", "data")] },
      { ...original, material: [material("large.txt", "x".repeat(1048577))] },
      { ...original, material: Array.from({ length: 9 }, (_, i) => material(`${i}.txt`, "x")) },
      { ...original, material: Array.from({ length: 5 }, (_, i) => material(`${i}.txt`, "x".repeat(900000))) },
      ...["../x.json", "https://example/x.json", "CON.json", ".env.txt", "app.tsx", ".git/config.txt", "credentials.json"].map(path => ({ ...original, material: [{ ...first, path }] })),
    ];
    for (const request of invalid) expect((await post(request)).status).toBe(409);
    expect(edit).not.toHaveBeenCalled(); expect(oauth.acquireGrant).not.toHaveBeenCalled(); unchanged();
  });

  it.each(["package.json", "app/old.tsx", "components/old.tsx", "public/data/nested/items.json"])("refuses stale bases and collision at %s before the model or grant", async collision => {
    expect((await post({ ...body(), expected_base_commit: "f".repeat(40) })).status).toBe(409);
    const work = join(root, "work");
    const parts = collision.split("/");
    if (parts.length > 1) mkdirSync(join(work, ...parts.slice(0, -1)), { recursive: true });
    writeFileSync(join(work, collision), "{}\n");
    git(work, ["add", "."]);
    git(work, ["-c", "user.name=test", "-c", "user.email=test@leaf.invalid", "commit", "-qm", "existing app"]);
    git(bare, ["fetch", work, "main:main"]);
    base = git(bare, ["rev-parse", "main"]);
    expect((await post(body())).status).toBe(409);
    expect(edit).not.toHaveBeenCalled(); expect(oauth.acquireGrant).not.toHaveBeenCalled(); unchanged();
  });

  it("uses getGrant when lease settlement is unavailable", async () => {
    delete oauth.settleGrant;
    expect((await post(body())).status).toBe(200);
    expect(oauth.acquireGrant).not.toHaveBeenCalled();
    expect(oauth.getGrant).toHaveBeenCalledWith(authority.tenantId);
    expect(git(bare, ["rev-parse", "main"])).toBe(base);
  });

  it("settles the editor's actual usage without forwarding its summary", async () => {
    edit.mockImplementation(async input => {
      writeFileSync(join(input.repoDir, "app/page.tsx"), page);
      return { summary: "MODEL PRIVATE SUMMARY", changedFiles: [], telemetry: { input_tokens: 12, output_tokens: 7 } };
    });
    const response = await post(body());
    expect(response.status).toBe(200);
    expect(await response.text()).not.toContain("MODEL PRIVATE SUMMARY");
    expect(oauth.settleGrant).toHaveBeenCalledWith(authority.tenantId, "lease", { usage: { cost_tokens: 19 }, stop_reason: "end_turn" });
  });

  it.each(["noop", "failure", "control", "untracked", "ignored", "scaffold", "material", "delete", "symlink", "wrong-path", "empty", "executable"])("rejects %s model output and settles the grant", async mode => {
    edit.mockImplementation(async input => {
      const dir = input.repoDir;
      if (mode === "failure") throw new Error("MODEL PRIVATE ERROR test-only");
      if (mode === "noop") return { summary: "done", changedFiles: ["app/page.tsx"] };
      writeFileSync(join(dir, "app/page.tsx"), mode === "empty" ? " \n" : page);
      if (mode === "control") writeFileSync(join(dir, "PROMPT.md"), "changed");
      if (mode === "untracked") writeFileSync(join(dir, "evil.js"), "bad");
      if (mode === "ignored") { writeFileSync(join(dir, ".gitignore"), "ignored.txt\n"); writeFileSync(join(dir, "ignored.txt"), "bad"); }
      if (mode === "scaffold") writeFileSync(join(dir, "package.json"), "{}");
      if (mode === "material") writeFileSync(join(dir, "public/data/nested/items.json"), "{}");
      if (mode === "delete") rmSync(join(dir, ".leaf/source-seed.json"));
      if (mode === "symlink") symlinkSync(join(dir, "public/data/nested/items.json"), join(dir, "app/link.ts"));
      if (mode === "wrong-path") { rmSync(join(dir, "app/page.tsx")); writeFileSync(join(dir, "app/page.jsx"), page); }
      if (mode === "executable") {
        if (process.platform !== "win32") chmodSync(join(dir, "app/page.tsx"), 0o755);
        else { git(dir, ["add", "app/page.tsx"]); git(dir, ["update-index", "--chmod=+x", "app/page.tsx"]); }
      }
      return { summary: "done", changedFiles: [] };
    });
    const response = await post(body());
    expect(response.status).toBe(409);
    expect(await response.text()).not.toContain("MODEL PRIVATE");
    expect(oauth.settleGrant).toHaveBeenCalledWith(authority.tenantId, "lease", { usage: { cost_tokens: 0 }, stop_reason: "error" });
    unchanged();
  });
});
