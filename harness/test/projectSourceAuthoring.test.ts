import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createHarness } from "../src/server.js";
import { authorProjectSource, PROJECT_SOURCE_SCAFFOLD } from "../src/agent/projectSourceAuthoring.js";
import { nativeProjectSourceProfile } from "../src/agent/nativeProjectSourceProfile.js";
import { ProjectRepositoryEditCoordinator, ProjectRepositoryEditError, type StagedProjectRepositoryEdit } from "../src/agent/projectRepositoryEditCoordinator.js";
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
const nativeProfile = nativeProjectSourceProfile(authority.projectId);
const swift = 'import SwiftUI\nstruct ContentView: View { var body: some View { List(["Pear", "Apple"], id: \\.self) { Text($0) } } }\n';
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
        return { contract, action: "record_staged", edit_id: request.receipt.edit_id, state: "staged", version: 1 } as const;
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
  function seed(files: Record<string, string>) {
    const work = join(root, "work");
    for (const [path, content] of Object.entries(files)) {
      mkdirSync(dirname(join(work, path)), { recursive: true });
      writeFileSync(join(work, path), content);
    }
    git(work, ["add", "."]);
    git(work, ["-c", "user.name=test", "-c", "user.email=test@leaf.invalid", "commit", "-qm", "baseline"]);
    git(bare, ["fetch", work, "main:main"]);
    base = git(bare, ["rev-parse", "main"]);
  }
  function nativeEditor() {
    edit.mockImplementation(async input => {
      expect(input.context).toContain(nativeProfile.context);
      expect(input.context).toContain("Resources/Input/nested/items.json");
      expect(readFileSync(join(input.repoDir, "Resources/Input/nested/items.json"), "utf8")).toBe(raw);
      writeFileSync(join(input.repoDir, "Sources/ContentView.swift"), swift);
      return { summary: "private native", changedFiles: ["deliberate-lie"] };
    });
  }

  function existingApp(extra: Record<string, string> = {}) {
    const files = { ...PROJECT_SOURCE_SCAFFOLD, "app/page.tsx": page, "public/data/nested/items.json": raw,
      ...nativeProfile.scaffold, "Sources/ContentView.swift": swift, "Resources/Input/old.txt": "native input\n", ...extra };
    seed(files);
    return files;
  }

  const nativeEditId = "77777777-7777-4777-8777-777777777777";
  function nativeEditRequest() {
    return { ...body(), edit_id: nativeEditId, source_profile: nativeProfile.id, source_operation: "edit", material: [] as ReturnType<typeof material>[] };
  }
  async function publishedNative(observeStageFailure = false) {
    // Keep unrelated Web/source files, including paths that Web edit preflight
    // would reject. They are immutable baseline data for a Native edit.
    seed({ ...PROJECT_SOURCE_SCAFFOLD, "app/page.tsx": page, "app/legacy.jsx": "legacy\n",
      "components/legacy.jsx": "legacy\n", "src/legacy.ts": "source\n", "pages/old.tsx": "old\n",
      "public/data/nested/items.json": raw });
    nativeEditor();
    const create = edit.getMockImplementation()!;
    edit.mockImplementation(async input => {
      const result = await create(input);
      writeFileSync(join(input.repoDir, "Sources/Old.swift"), "struct Old {}\n");
      return result;
    });
    const diagnostic = { stage: "stageEdit", code: "not-observed" };
    const codes = new Set(["invalid_source_request", "source_base_conflict", "main_moved_during_stage",
      "invalid_changed_paths", "invalid_diff_digest", "stage_record_mismatch", "invalid_lease_witness",
      "invalid_authority", "invalid_edit_id", "invalid_actor", "invalid_operation", "invalid_source_edit",
      "invalid_base_commit", "invalid_instruction_digest", "invalid_idempotency_key", "invalid_commit_message",
      "invalid_apply", "invalid_change_evidence", "project_writer_lease_required"]);
    const stageEdit = service.stageEdit.bind(service);
    const observation = observeStageFailure ? vi.spyOn(service, "stageEdit").mockImplementation(async request => {
      try { return await stageEdit(request); }
      catch (error) {
        diagnostic.code = error instanceof ProjectRepositoryEditError && codes.has(error.code) ? error.code : "unclassified";
        throw error;
      }
    }) : undefined;
    let response: Response;
    try { response = await post({ ...body(), source_profile: nativeProfile.id }); }
    finally { observation?.mockRestore(); }
    expect(response.status, observeStageFailure ? JSON.stringify(diagnostic) : undefined).toBe(200);
    const initial = await response.json() as StagedProjectRepositoryEdit;
    expect((await post({ ...common, confirmation_id: actor, receipt_digest: initial.receiptDigest,
      expected_version: 1, transition_key: "publish-native" }, "publish")).status).toBe(200);
    base = initial.receipt.staged_head_commit;
    expect(git(bare, ["rev-parse", "main"])).toBe(base);
    const work = join(root, "work");
    git(work, ["fetch", bare, "main"]);
    git(work, ["reset", "--hard", "FETCH_HEAD"]);
    vi.clearAllMocks();
    confirmed = false;
  }

  function nativeEditUnchanged() {
    unchanged();
    // The coordinator can reserve a private ref at the base before preflight.
    // Failure must never advance it to an authored candidate.
    expect(["", base]).toContain(git(bare, ["for-each-ref", "--format=%(objectname)", `refs/leaf/changes/${nativeEditId}`]));
  }

  it.each([undefined, "create"])("creates a plain Browser brief without material with operation %s", async source_operation => {
    const requirements = "Create a landing page for a neighborhood garden.";
    edit.mockImplementation(async input => {
      expect(input.instruction).toBe(requirements);
      writeFileSync(join(input.repoDir, "app/page.tsx"), "export default function Page(){return <main>Garden</main>;}\n");
      return { summary: "done", changedFiles: [] };
    });
    const request = { ...body(), material: [], requirements, instruction_digest: hash(requirements),
      ...(source_operation === undefined ? {} : { source_operation }) };
    expect((await post(request)).status).toBe(200);
    expect(git(bare, ["rev-parse", "main"])).toBe(base);
    expect(oauth.settleGrant).toHaveBeenCalledWith(authority.tenantId, "lease", { usage: { cost_tokens: 0 }, stop_reason: "end_turn" });
  });

  it.each([null, 1, false, {}, [], "", "update", "EDIT"])("rejects invalid operation %j before grants", async source_operation => {
    expect((await post({ ...body(), source_operation })).status).toBe(409);
    expect((await post({ ...body(), source_profile: nativeProfile.id, source_operation })).status).toBe(409);
    expect(edit).not.toHaveBeenCalled(); expect(oauth.acquireGrant).not.toHaveBeenCalled(); unchanged();
  });

  it("rejects native edits without existing source and native create without input before grants", async () => {
    expect((await post({ ...body(), source_profile: nativeProfile.id, source_operation: "edit" })).status).toBe(409);
    expect((await post({ ...body(), source_profile: nativeProfile.id, material: [] })).status).toBe(409);
    expect(edit).not.toHaveBeenCalled(); expect(oauth.acquireGrant).not.toHaveBeenCalled(); unchanged();
  });

  it.each([false, true])("edits a published Native candidate with real Git evidence, new material=%s", async supplied => {
    await publishedNative();
    const updated = swift + "// updated\n";
    edit.mockImplementation(async input => {
      expect(input.instruction).toBe(body().requirements);
      expect(input.context).toContain("Edit the existing SwiftUI source in Sources/ContentView.swift");
      expect(input.context).toContain("Resources/Input/nested/items.json");
      expect(input.context).toContain("Do not execute shell, builds, tools, network");
      expect(input.context).toContain("Do not invoke Xcode, XcodeGen or a simulator");
      expect(input.context).not.toContain("Next");
      expect(readFileSync(join(input.repoDir, "Sources/ContentView.swift"), "utf8")).toBe(swift);
      expect(readFileSync(join(input.repoDir, "Resources/Input/nested/items.json"), "utf8")).toBe(raw);
      if (supplied) expect(readFileSync(join(input.repoDir, "Resources/Input/new.csv"), "utf8")).toBe("value\n2\n");
      writeFileSync(join(input.repoDir, "Sources/ContentView.swift"), updated);
      mkdirSync(join(input.repoDir, "Sources/Views"));
      writeFileSync(join(input.repoDir, "Sources/Views/Helper.swift"), "struct Helper {}\n");
      rmSync(join(input.repoDir, "Sources/Old.swift"));
      return { summary: "private native edit", changedFiles: ["lie"], telemetry: { input_tokens: 11, output_tokens: 4 } };
    });
    const stage = vi.spyOn(service, "stageEdit");
    const response = await post({ ...nativeEditRequest(), material: supplied ? [material("nested/items.json", raw), material("new.csv", "value\n2\n")] : [] });
    expect(response.status).toBe(200);
    const staged = await response.json() as StagedProjectRepositoryEdit;
    expect(stage).toHaveBeenCalledWith(expect.objectContaining({ authority, operation: "edit", expectedBaseCommit: base }));
    expect(staged.receipt).toMatchObject({ operation: "edit", base_commit: base, tenant_id: authority.tenantId,
      organization_id: authority.organizationId, project_id: authority.projectId, repo_key: authority.repoKey });
    expect(staged.receipt).not.toHaveProperty("source_profile");
    const head = staged.receipt.staged_head_commit;
    expect(git(bare, ["rev-parse", `refs/leaf/changes/${nativeEditId}`])).toBe(head);
    expect(git(bare, ["rev-parse", `${head}^{tree}`])).toBe(staged.receipt.staged_tree);
    expect(staged.receipt.changed_paths).toEqual([...(supplied ? ["Resources/Input/new.csv"] : []),
      "Sources/ContentView.swift", "Sources/Old.swift", "Sources/Views/Helper.swift"]);
    expect(execFileSync("git", ["show", `${head}:Sources/ContentView.swift`], { cwd: bare }).toString()).toBe(updated);
    expect(execFileSync("git", ["show", `${head}:Sources/Views/Helper.swift`], { cwd: bare }).toString()).toBe("struct Helper {}\n");
    expect(git(bare, ["ls-tree", head, "--", "Sources/Old.swift"])).toBe("");
    if (supplied) expect(execFileSync("git", ["show", `${head}:Resources/Input/new.csv`], { cwd: bare }).toString()).toBe("value\n2\n");
    const changed = new Set(staged.receipt.changed_paths);
    for (const path of git(bare, ["ls-tree", "-r", "--name-only", base]).split("\n")) if (!changed.has(path)) {
      expect(execFileSync("git", ["show", `${head}:${path}`], { cwd: bare })).toEqual(execFileSync("git", ["show", `${base}:${path}`], { cwd: bare }));
    }
    const diff = execFileSync("git", ["-c", "diff.algorithm=myers", "-c", "diff.indentHeuristic=false", "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
      "--binary", "--full-index", "--no-color", "--src-prefix=a/", "--dst-prefix=b/", "--unified=3", base, head, "--"], { cwd: bare });
    expect(staged.receipt.diff_digest).toBe(hash(diff));
    expect(edit).toHaveBeenCalledTimes(1); expect(oauth.acquireGrant).toHaveBeenCalledTimes(1);
    expect(oauth.getGrant).not.toHaveBeenCalled();
    expect(oauth.settleGrant).toHaveBeenCalledWith(authority.tenantId, "lease", { usage: { cost_tokens: 15 }, stop_reason: "end_turn" });
    expect(coordination.recordStaged).toHaveBeenCalledTimes(1);
    expect(coordination.authorizePublish).not.toHaveBeenCalled();
    expect(git(bare, ["rev-parse", "main"])).toBe(base);
  });

  it.each(["wrong-project", "app-entry", "missing-scaffold", "missing-product", "empty-product", "unsupported-source", "source-case",
    "input-bytes", "input-case", "directory-case", "stale", "wrong-base", "wrong-authority", "symlink", "executable"])("rejects Native edit %s before a grant", async mode => {
    await publishedNative();
    const work = join(root, "work");
    const changes: Record<string, string> = {};
    if (mode === "wrong-project") changes["project.yml"] = nativeProjectSourceProfile(actor).scaffold["project.yml"];
    if (mode === "app-entry") changes["Sources/LeafProjectApp.swift"] = "changed\n";
    if (mode === "empty-product") changes["Sources/ContentView.swift"] = " \n";
    if (mode === "unsupported-source") changes["Sources/unknown.txt"] = "unknown\n";
    if (Object.keys(changes).length) seed(changes);
    if (["missing-scaffold", "missing-product", "source-case", "symlink", "executable"].includes(mode)) {
      if (mode.startsWith("missing")) rmSync(join(work, mode === "missing-scaffold" ? "project.yml" : "Sources/ContentView.swift"));
      git(work, ["add", "-A"]);
      if (mode === "symlink") {
        const blob = git(work, ["rev-parse", "HEAD:Sources/ContentView.swift"]);
        git(work, ["update-index", "--add", "--cacheinfo", "120000", blob, "Sources/Link.swift"]);
      }
      if (mode === "source-case") {
        const blob = git(work, ["rev-parse", "HEAD:Sources/ContentView.swift"]);
        git(work, ["update-index", "--add", "--cacheinfo", "100644", blob, "Sources/contentview.swift"]);
      }
      if (mode === "executable") git(work, ["update-index", "--chmod=+x", "Sources/ContentView.swift"]);
      git(work, ["-c", "user.name=test", "-c", "user.email=test@leaf.invalid", "commit", "-qm", "invalid native baseline"]);
      git(bare, ["fetch", work, "main:main"]);
      base = git(bare, ["rev-parse", "main"]);
    }
    const request = nativeEditRequest();
    if (mode === "input-bytes") request.material = [material("nested/items.json", "{}")];
    if (mode === "input-case") request.material = [material("nested/ITEMS.json", raw)];
    if (mode === "directory-case") request.material = [material("NESTED/new.json", "{}")];
    if (mode === "stale") request.expected_base_commit = git(bare, ["rev-parse", `${base}^`]);
    if (mode === "wrong-base") request.expected_base_commit = "f".repeat(40);
    if (mode === "wrong-authority") request.project_id = actor;
    expect((await post(request)).status).toBe(409);
    expect(edit).not.toHaveBeenCalled(); expect(oauth.acquireGrant).not.toHaveBeenCalled(); expect(oauth.getGrant).not.toHaveBeenCalled();
    nativeEditUnchanged();
  });

  it("rejects a resumed Native candidate whose HEAD has advanced beyond the requested base before grants", async () => {
    await publishedNative();
    edit.mockImplementation(async input => {
      writeFileSync(join(input.repoDir, "Sources/ContentView.swift"), swift + "// edited\n");
      return { summary: "done", changedFiles: [] };
    });
    expect((await post(nativeEditRequest())).status).toBe(200);
    const head = git(bare, ["rev-parse", `refs/leaf/changes/${nativeEditId}`]);
    vi.clearAllMocks();
    expect((await post(nativeEditRequest())).status).toBe(409);
    expect(edit).not.toHaveBeenCalled(); expect(oauth.acquireGrant).not.toHaveBeenCalled();
    unchanged();
    expect(git(bare, ["rev-parse", `refs/leaf/changes/${nativeEditId}`])).toBe(head);
  });

  it("accepts eight new Native originals at the exact per-file and total byte limits", async () => {
    await publishedNative();
    const originals = Array.from({ length: 8 }, (_, i) => material(`reference${i}.txt`, i < 4 ? "x".repeat(1048576) : ""));
    edit.mockImplementation(async input => {
      for (const original of originals) {
        const bytes = readFileSync(join(input.repoDir, "Resources/Input", original.path));
        expect(bytes.length).toBe(original.size_bytes); expect(hash(bytes)).toBe(original.sha256);
      }
      writeFileSync(join(input.repoDir, "Sources/ContentView.swift"), swift + "// referenced\n");
      return { summary: "done", changedFiles: [] };
    });
    const response = await post({ ...nativeEditRequest(), material: originals });
    expect(response.status).toBe(200);
    const staged = await response.json() as StagedProjectRepositoryEdit;
    for (const original of originals) expect(hash(execFileSync("git", ["show", `${staged.receipt.staged_head_commit}:Resources/Input/${original.path}`], { cwd: bare }))).toBe(original.sha256);
    expect(edit).toHaveBeenCalledTimes(1); expect(oauth.acquireGrant).toHaveBeenCalledTimes(1);
    expect(oauth.settleGrant).toHaveBeenCalledWith(authority.tenantId, "lease", { usage: { cost_tokens: 0 }, stop_reason: "end_turn" });
    expect(git(bare, ["rev-parse", "main"])).toBe(base);
    expect(coordination.authorizePublish).not.toHaveBeenCalled();
  });

  it("enforces Native edit original count, byte, hash and path limits before grants", async () => {
    await publishedNative();
    const first = material("new.txt", "new");
    const invalid = [
      [material("large.txt", "x".repeat(1048577))],
      Array.from({ length: 5 }, (_, i) => material(`${i}.txt`, "x".repeat(900000))),
      Array.from({ length: 9 }, (_, i) => material(`${i}.txt`, "x")),
      [{ ...first, sha256: "a".repeat(64) }], [{ ...first, size_bytes: 1 }], [{ ...first, product_b64: first.product_b64 + "\n" }],
      [first, first], [material("../new.txt", "new")], [material("new.swift", "new")],
    ];
    for (const originals of invalid) expect((await post({ ...nativeEditRequest(), material: originals })).status).toBe(409);
    expect(edit).not.toHaveBeenCalled(); expect(oauth.acquireGrant).not.toHaveBeenCalled(); nativeEditUnchanged();
  });

  it.each(["noop", "failure", "empty", "delete-product", "scaffold", "app-entry", "material", "web", "component", "source", "seed", "control",
    "delete-material", "unknown", "entitlements", "dependencies", "scripts", "output", "alias", "symlink", "executable",
    "file-limit", "count-limit", "total-limit"])("rejects Native edit %s output atomically and settles its grant", async mode => {
    await publishedNative(mode === "count-limit");
    edit.mockImplementation(async input => {
      const dir = input.repoDir, product = "Sources/ContentView.swift";
      if (mode === "failure") throw new Error("private editor failure");
      if (mode !== "noop") writeFileSync(join(dir, product), mode === "empty" ? " \n" : swift + "// edited\n");
      const paths: Record<string, string> = { scaffold: "project.yml", "app-entry": "Sources/LeafProjectApp.swift", material: "Resources/Input/nested/items.json",
        web: "app/page.tsx", component: "components/legacy.jsx", source: "src/legacy.ts", seed: ".leaf/source-seed.json", control: "PROMPT.md",
        unknown: "Sources/extra.txt", entitlements: "Sources/entitlements.swift", dependencies: "Sources/Package.swift", scripts: "Sources/scripts/run.swift", output: "output.txt" };
      if (paths[mode]) { mkdirSync(dirname(join(dir, paths[mode])), { recursive: true }); writeFileSync(join(dir, paths[mode]), "changed"); }
      if (mode === "delete-product") rmSync(join(dir, product));
      if (mode === "delete-material") rmSync(join(dir, "Resources/Input/nested/items.json"));
      if (mode === "alias") {
        const blob = git(dir, ["rev-parse", `HEAD:${product}`]);
        git(dir, ["update-index", "--add", "--cacheinfo", "100644", blob, "Sources/CONTENTVIEW.swift"]);
      }
      if (mode === "symlink") symlinkSync(join(dir, "Resources/Input/nested/items.json"), join(dir, "Sources/Link.swift"));
      if (mode === "executable") {
        if (process.platform !== "win32") chmodSync(join(dir, product), 0o755);
        else { git(dir, ["add", product]); git(dir, ["update-index", "--chmod=+x", product]); }
      }
      if (mode === "file-limit") writeFileSync(join(dir, product), "x".repeat(1048577));
      if (mode === "count-limit" || mode === "total-limit") for (let i = 0; i < (mode === "count-limit" ? 64 : 5); i++) writeFileSync(join(dir, `Sources/File${i}.swift`), "x".repeat(mode === "count-limit" ? 1 : 900000));
      return { summary: "private", changedFiles: [] };
    });
    expect((await post(nativeEditRequest())).status).toBe(409);
    expect(edit).toHaveBeenCalledTimes(1); expect(oauth.acquireGrant).toHaveBeenCalledTimes(1);
    expect(oauth.settleGrant).toHaveBeenCalledWith(authority.tenantId, "lease", { usage: { cost_tokens: 0 }, stop_reason: "error" });
    nativeEditUnchanged();
  });

  it("rejects duplicate nested and escaped author keys before staging or grants", async () => {
    const stage = vi.spyOn(service, "stageEdit");
    const original = JSON.stringify(body());
    for (const wire of [
      original.replace('"path":"nested/items.json"', '"path":"nested/items.json","path":"nested/items.json"'),
      original.replace('"path":"nested/items.json"', '"path":"nested/items.json","p\\u0061th":"nested/items.json"'),
      original.replace('"tenant_id":', `"tenant_\\u0069d":"${authority.tenantId}","tenant_id":`),
    ]) {
      expect(wire).not.toBe(original);
      const response = await fetch(`${url}/internal/project-repository-source/author`, { method: "POST",
        headers: { "content-type": "application/json", "x-harness-secret": secret, "x-tenant-id": authority.tenantId }, body: wire });
      expect(response.status).toBe(409);
    }
    expect(stage).not.toHaveBeenCalled(); expect(edit).not.toHaveBeenCalled();
    expect(oauth.acquireGrant).not.toHaveBeenCalled(); expect(oauth.settleGrant).not.toHaveBeenCalled(); unchanged();
  });

  it("accepts escaped quotes and structural characters in string values", async () => {
    const requirements = 'Show literal text: "path":"one", "path":"two", [{ }] and a backslash \\.';
    edit.mockImplementation(async input => {
      expect(input.instruction).toBe(requirements);
      writeFileSync(join(input.repoDir, "app/page.tsx"), page);
      return { summary: "done", changedFiles: [] };
    });
    const request = { ...body(), requirements, instruction_digest: hash(requirements) };
    // Escaped property names and whitespace before the colon remain valid.
    const wire = JSON.stringify(request).replace('"requirements":', '"requ\\u0069rements" \n :');
    const response = await fetch(`${url}/internal/project-repository-source/author`, { method: "POST",
      headers: { "content-type": "application/json", "x-harness-secret": secret, "x-tenant-id": authority.tenantId }, body: wire });
    expect(response.status).toBe(200);
    expect(edit).toHaveBeenCalledTimes(1); expect(oauth.acquireGrant).toHaveBeenCalledTimes(1);
  });

  it.each(["input", "http", "boundary"])("preserves a real large valid JSON %s fixture through authenticated HTTP and Git", async fixture => {
    const stage = vi.spyOn(service, "stageEdit");
    // Synthetic transport fixture, never a recreated user result. Cover both exact input and wire sizes.
    const length = fixture === "input" ? 6915737 : fixture === "boundary" ? 8388608 : 5185500;
    const json = JSON.stringify({ payload: "x".repeat(length - 14) });
    expect(Buffer.byteLength(json)).toBe(length);
    JSON.parse(json);
    const request = { ...body(), material: [material("large.json", json)] };
    if (fixture === "http") {
      const padding = 6915737 - Buffer.byteLength(JSON.stringify(request));
      expect(padding).toBeGreaterThan(0);
      request.material[0].key += "x".repeat(padding);
      expect(request.material[0].key.length).toBeLessThanOrEqual(1024);
      expect(Buffer.byteLength(JSON.stringify(request))).toBe(6915737);
    }
    edit.mockImplementation(async input => {
      const bytes = readFileSync(join(input.repoDir, "public/data/large.json"));
      expect(bytes.equals(Buffer.from(json))).toBe(true);
      expect(hash(bytes)).toBe(request.material[0].sha256);
      writeFileSync(join(input.repoDir, "app/page.tsx"), "export default function Page(){return <main>Large input</main>;}\n");
      return { summary: "done", changedFiles: [] };
    });
    const response = await post(request);
    // Call counts identify parser/preflight refusal separately from failures
    // inside the model or Git staging, without returning private model errors.
    expect(stage).toHaveBeenCalledTimes(1);
    expect(oauth.acquireGrant).toHaveBeenCalledTimes(1);
    expect(edit).toHaveBeenCalledTimes(1);
    expect(oauth.settleGrant).toHaveBeenCalledWith(authority.tenantId, "lease", { usage: { cost_tokens: 0 }, stop_reason: "end_turn" });
    expect(response.status).toBe(200);
    const staged = await response.json() as StagedProjectRepositoryEdit;
    const bytes = execFileSync("git", ["show", `${staged.receipt.staged_head_commit}:public/data/large.json`], { cwd: bare, maxBuffer: 12 * 1024 * 1024 });
    expect(bytes.length).toBe(length);
    expect(bytes.equals(Buffer.from(json))).toBe(true);
    expect(hash(bytes)).toBe(request.material[0].sha256);
    expect(git(bare, ["rev-parse", "main"])).toBe(base);
  }, 30000);

  it.each([false, true])("edits an existing protected app and publishes its exact tree once, deleting component=%s", async deleting => {
    const old = existingApp({ "components/Old.tsx": "export default function Old(){return <p>Old</p>;}\n" });
    const requirements = "Add a favorite toggle and a comparison message for each fruit.";
    const updated = '"use client";\nimport {useState} from "react";\nexport default function Page(){const [favorite,setFavorite]=useState(false);return <main><button onClick={()=>setFavorite(!favorite)}>{favorite?"Favorite pear":"Compare fruits"}</button></main>;}\n';
    const component = "export const Label = () => <span>Compare</span>;\n";
    edit.mockImplementation(async input => {
      expect(input.instruction).toBe(requirements);
      expect(input.context).toContain("Edit the existing");
      expect(input.context).toContain("public/data/nested/items.json");
      expect(readFileSync(join(input.repoDir, "app/page.tsx"), "utf8")).toBe(page);
      expect(readFileSync(join(input.repoDir, "public/data/new.txt"), "utf8")).toBe("additional input\n");
      writeFileSync(join(input.repoDir, "app/page.tsx"), updated);
      writeFileSync(join(input.repoDir, "components/Label.tsx"), component);
      if (deleting) rmSync(join(input.repoDir, "components/Old.tsx"));
      return { summary: "private", changedFiles: ["incorrect"] };
    });
    const response = await post({ ...body(), source_operation: "edit", requirements, instruction_digest: hash(requirements),
      material: [material("nested/items.json", raw), material("new.txt", "additional input\n")] });
    expect(response.status).toBe(200);
    const staged = await response.json() as StagedProjectRepositoryEdit;
    const head = staged.receipt.staged_head_commit;
    expect(staged.receipt.changed_paths).toEqual(["app/page.tsx", "components/Label.tsx", ...(deleting ? ["components/Old.tsx"] : []), "public/data/new.txt"]);
    expect(staged.receipt).toMatchObject({ tenant_id: authority.tenantId, organization_id: authority.organizationId,
      project_id: authority.projectId, repo_key: authority.repoKey, actor_binding_id: actor });
    const bytes = (path: string) => execFileSync("git", ["show", `${head}:${path}`], { cwd: bare });
    expect(bytes("app/page.tsx").toString()).toBe(updated);
    expect(bytes("components/Label.tsx").toString()).toBe(component);
    for (const [path, content] of Object.entries(old)) if (path !== "app/page.tsx" && !(deleting && path === "components/Old.tsx")) expect(bytes(path)).toEqual(Buffer.from(content));
    expect(bytes(".leaf/source-seed.json").toString()).toBe('{"seed":true}\n');
    expect(bytes("PROMPT.md").toString()).toBe("Protected seed\n");
    if (deleting) expect(git(bare, ["ls-tree", head, "components/Old.tsx"])).toBe("");
    expect(git(bare, ["rev-parse", "main"])).toBe(base);
    expect(oauth.settleGrant).toHaveBeenCalledWith(authority.tenantId, "lease", { usage: { cost_tokens: 0 }, stop_reason: "end_turn" });
    const request = { ...common, confirmation_id: actor, receipt_digest: staged.receiptDigest, expected_version: 1, transition_key: "publish" };
    expect((await post(request, "publish")).status).toBe(200);
    expect((await post(request, "publish")).status).toBe(409);
    expect(git(bare, ["rev-parse", "main"])).toBe(head);
    expect(git(bare, ["rev-parse", "main^{tree}"])).toBe(staged.receipt.staged_tree);
    expect(coordination.authorizePublish).toHaveBeenCalledTimes(2);
  });

  it("edits without new material and refuses replay before acquiring another grant", async () => {
    existingApp();
    edit.mockImplementation(async input => {
      expect(input.context).toContain("public/data/nested/items.json");
      writeFileSync(join(input.repoDir, "app/page.tsx"), page + "// New content\n");
      return { summary: "done", changedFiles: [] };
    });
    const request = { ...body(), source_operation: "edit", material: [] };
    expect((await post(request)).status).toBe(200);
    const head = git(bare, ["rev-parse", `refs/leaf/changes/${editId}`]);
    expect((await post(request)).status).toBe(409);
    expect((await post({ ...request, source_operation: "create" })).status).toBe(409);
    expect(edit).toHaveBeenCalledTimes(1); expect(oauth.acquireGrant).toHaveBeenCalledTimes(1);
    expect(git(bare, ["rev-parse", `refs/leaf/changes/${editId}`])).toBe(head);
    expect(git(bare, ["rev-parse", "main"])).toBe(base);
  });

  it.each(["missing-app", "scaffold", "missing-scaffold", "empty-page", "src", "pages", "unknown", "input-bytes", "input-case", "directory-case", "stale"])("rejects edit %s preflight before model and grants", async mode => {
    if (mode !== "missing-app") {
      const extra: Record<string, string> = {};
      if (mode === "scaffold") extra["package.json"] = "{}\n";
      if (mode === "empty-page") extra["app/page.tsx"] = " \n";
      if (mode === "src") extra["src/app/page.tsx"] = page;
      if (mode === "pages") extra["pages/index.tsx"] = page;
      if (mode === "unknown") extra["app/unsupported.jsx"] = page;
      existingApp(extra);
      if (mode === "missing-scaffold") {
        const work = join(root, "work");
        rmSync(join(work, "next.config.mjs"));
        git(work, ["add", "-A"]);
        git(work, ["-c", "user.name=test", "-c", "user.email=test@leaf.invalid", "commit", "-qm", "incomplete scaffold"]);
        git(bare, ["fetch", work, "main:main"]);
        base = git(bare, ["rev-parse", "main"]);
      }
    }
    const request = { ...body(), source_operation: "edit" };
    if (mode === "input-bytes") request.material = [material("nested/items.json", "{}")];
    if (mode === "input-case") request.material = [material("nested/ITEMS.json", raw)];
    if (mode === "directory-case") request.material = [material("NESTED/new.json", "{}")];
    if (mode === "stale") request.expected_base_commit = "f".repeat(40);
    expect((await post(request)).status).toBe(409);
    expect(edit).not.toHaveBeenCalled(); expect(oauth.acquireGrant).not.toHaveBeenCalled(); expect(oauth.getGrant).not.toHaveBeenCalled(); unchanged();
  });

  it.each(["symlink", "executable"])("rejects %s baseline before grants", async mode => {
    existingApp();
    const work = join(root, "work");
    // The source boundary rejects Git symlink mode even when Windows checks it
    // out as a regular file. Do not require the host's symlink privilege.
    if (mode === "symlink") writeFileSync(join(work, "app/link.ts"), "../public/data/nested/items.json");
    else if (process.platform !== "win32") chmodSync(join(work, "app/page.tsx"), 0o755);
    git(work, ["add", "-A"]);
    if (mode === "symlink") {
      const blob = git(work, ["hash-object", "-w", "app/link.ts"]);
      git(work, ["update-index", "--add", "--cacheinfo", "120000", blob, "app/link.ts"]);
    }
    if (mode === "executable") git(work, ["update-index", "--chmod=+x", "app/page.tsx"]);
    git(work, ["-c", "user.name=test", "-c", "user.email=test@leaf.invalid", "commit", "-qm", "invalid baseline mode"]);
    git(bare, ["fetch", work, "main:main"]);
    base = git(bare, ["rev-parse", "main"]);
    expect((await post({ ...body(), source_operation: "edit", material: [] })).status).toBe(409);
    expect(edit).not.toHaveBeenCalled(); expect(oauth.acquireGrant).not.toHaveBeenCalled(); unchanged();
  });

  it.each([false, true])("rejects aborted edits and settles only acquired grants, during model=%s", async duringModel => {
    existingApp();
    const controller = new AbortController();
    if (duringModel) edit.mockImplementation(async input => {
      writeFileSync(join(input.repoDir, "app/page.tsx"), page + "// changed\n");
      controller.abort();
      return { summary: "done", changedFiles: [] };
    });
    else controller.abort();
    await expect(authorProjectSource(service, { oauth, editor: { edit } },
      { ...body(), source_operation: "edit", material: [] }, authority.tenantId, controller.signal)).rejects.toThrow();
    expect(oauth.acquireGrant).toHaveBeenCalledTimes(duringModel ? 1 : 0);
    expect(edit).toHaveBeenCalledTimes(duringModel ? 1 : 0);
    if (duringModel) expect(oauth.settleGrant).toHaveBeenCalledWith(authority.tenantId, "lease", { usage: { cost_tokens: 0 }, stop_reason: "error" });
    else expect(oauth.settleGrant).not.toHaveBeenCalled();
    unchanged();
  });

  it.each(["noop", "data", "scaffold", "layout", "native", "seed", "control", "delete-page", "delete-scaffold", "unknown", "alias", "symlink", "executable", "file-limit", "count-limit", "total-limit"])("rejects edit model %s mutation after acquiring and settling grant", async mode => {
    existingApp();
    edit.mockImplementation(async input => {
      const dir = input.repoDir;
      if (mode !== "noop") writeFileSync(join(dir, "app/page.tsx"), page + "// updated\n");
      const paths: Record<string, string> = { data: "public/data/nested/items.json", scaffold: "package.json", layout: "app/layout.tsx",
        native: "Sources/ContentView.swift", seed: ".leaf/source-seed.json", control: "PROMPT.md", unknown: "app/extra.jsx" };
      if (paths[mode]) writeFileSync(join(dir, paths[mode]), "changed");
      if (mode === "alias") {
        if (process.platform !== "win32") writeFileSync(join(dir, "app/PAGE.tsx"), "changed");
        // Insert an actual alias into Git's index on every host. A write to
        // PAGE.tsx alone can overwrite page.tsx on a case-insensitive volume.
        const blob = git(dir, ["rev-parse", "HEAD:app/page.tsx"]);
        git(dir, ["update-index", "--add", "--cacheinfo", "100644", blob, "app/PAGE.tsx"]);
        const paths = git(dir, ["ls-files", "-z"]).split("\0");
        expect(paths).toContain("app/page.tsx"); expect(paths).toContain("app/PAGE.tsx");
      }
      if (mode === "delete-page") rmSync(join(dir, "app/page.tsx"));
      if (mode === "delete-scaffold") rmSync(join(dir, "tsconfig.json"));
      if (mode === "symlink") symlinkSync(join(dir, "public/data/nested/items.json"), join(dir, "app/link.ts"));
      if (mode === "executable") {
        if (process.platform !== "win32") chmodSync(join(dir, "app/page.tsx"), 0o755);
        else { git(dir, ["add", "app/page.tsx"]); git(dir, ["update-index", "--chmod=+x", "app/page.tsx"]); }
      }
      if (mode === "file-limit") writeFileSync(join(dir, "app/page.tsx"), "x".repeat(1048577));
      if (mode === "count-limit" || mode === "total-limit") for (let i = 0; i < (mode === "count-limit" ? 64 : 5); i++) writeFileSync(join(dir, `app/File${i}.ts`), "x".repeat(mode === "count-limit" ? 1 : 900000));
      return { summary: "done", changedFiles: [] };
    });
    expect((await post({ ...body(), source_operation: "edit", material: [] })).status).toBe(409);
    expect(edit).toHaveBeenCalledTimes(1); expect(oauth.acquireGrant).toHaveBeenCalledTimes(1);
    expect(oauth.settleGrant).toHaveBeenCalledWith(authority.tenantId, "lease", { usage: { cost_tokens: 0 }, stop_reason: "error" });
    unchanged();
  });

  it("stages exact native scaffold, resource bytes and new Swift, then requires explicit publication", async () => {
    nativeEditor();
    const publish = vi.spyOn(TenantChangeRepo.prototype, "publishToMainObserved");
    const response = await post({ ...body(), source_profile: nativeProfile.id });
    expect(response.status).toBe(200);
    const staged = await response.json() as StagedProjectRepositoryEdit;
    const head = staged.receipt.staged_head_commit;
    const bytes = (path: string) => execFileSync("git", ["show", `${head}:${path}`], { cwd: bare });
    expect(staged.receipt.contract).toBe("leaf.project-repository-edit.v2");
    expect(git(bare, ["rev-parse", `refs/leaf/changes/${editId}`])).toBe(head);
    expect(git(bare, ["rev-parse", "main"])).toBe(base);
    expect(bytes("Sources/ContentView.swift")).toEqual(Buffer.from(swift));
    expect(bytes("Resources/Input/nested/items.json")).toEqual(Buffer.from(raw));
    for (const [path, content] of Object.entries(nativeProfile.scaffold)) expect(bytes(path)).toEqual(Buffer.from(content));
    expect(bytes("project.yml").toString()).toContain(`private.leaf.project.p${authority.projectId.replace(/-/g, "")}`);
    expect(bytes("PROMPT.md").toString()).toBe("Protected seed\n");
    expect(bytes(".leaf/source-seed.json").toString()).toBe('{"seed":true}\n');
    expect(staged.receipt.changed_paths).toEqual([...Object.keys(nativeProfile.scaffold), "Sources/ContentView.swift", "Resources/Input/nested/items.json"].sort());
    const diff = execFileSync("git", ["-c", "diff.algorithm=myers", "-c", "diff.indentHeuristic=false", "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
      "--binary", "--full-index", "--no-color", "--src-prefix=a/", "--dst-prefix=b/", "--unified=3", base, head, "--"], { cwd: bare });
    expect(staged.receipt.diff_digest).toBe(hash(diff));
    expect(publish).not.toHaveBeenCalled();
    const request = { ...common, confirmation_id: actor, receipt_digest: staged.receiptDigest, expected_version: 1, transition_key: "publish" };
    expect((await post(request, "publish")).status).toBe(200);
    expect((await post(request, "publish")).status).toBe(409);
    expect(git(bare, ["rev-parse", "main"])).toBe(head);
    expect(publish).toHaveBeenCalledTimes(1);
  });

  it("passes different native requirements and CSV data to the generic editor", async () => {
    const requirements = "Create a quantity calculator using the supplied prices.";
    const csv = "name,price\nBolt,2.75\n";
    const calculator = 'import SwiftUI\nstruct ContentView: View { @State private var quantity = 0; var body: some View { VStack { Stepper("Quantity", value: $quantity); Text(String(Double(quantity) * 2.75)) } } }\n';
    edit.mockImplementation(async input => {
      expect(input.instruction).toBe(requirements);
      expect(readFileSync(join(input.repoDir, "Resources/Input/prices.csv"), "utf8")).toBe(csv);
      writeFileSync(join(input.repoDir, "Sources/ContentView.swift"), calculator);
      return { summary: "done", changedFiles: [] };
    });
    const response = await post({ ...body(), source_profile: nativeProfile.id, requirements, instruction_digest: hash(requirements), material: [material("prices.csv", csv)] });
    expect(response.status).toBe(200);
    const staged = await response.json() as StagedProjectRepositoryEdit;
    expect(execFileSync("git", ["show", `${staged.receipt.staged_head_commit}:Sources/ContentView.swift`], { cwd: bare }).toString()).toBe(calculator);
    expect(execFileSync("git", ["show", `${staged.receipt.staged_head_commit}:Resources/Input/prices.csv`], { cwd: bare }).toString()).toBe(csv);
    expect(git(bare, ["rev-parse", "main"])).toBe(base);
  });

  it.each([null, 1, false, {}, [], "", "unknown", "IOS-SWIFTUI-XCODEGEN-V1"])("rejects unsupported profile %j before grants", async source_profile => {
    expect((await post({ ...body(), source_profile })).status).toBe(409);
    expect(edit).not.toHaveBeenCalled(); expect(oauth.acquireGrant).not.toHaveBeenCalled(); expect(oauth.getGrant).not.toHaveBeenCalled(); unchanged();
  });

  it("explicit web choice has the same tree and evidence as the default", async () => {
    const implicit = await post(body());
    expect(implicit.status).toBe(200);
    const first = await implicit.json() as StagedProjectRepositoryEdit;
    const explicit = await post({ ...body(), source_profile: "web-next-static-v1", edit_id: "77777777-7777-4777-8777-777777777777" });
    expect(explicit.status).toBe(200);
    const second = await explicit.json() as StagedProjectRepositoryEdit;
    expect(second.receipt.staged_tree).toBe(first.receipt.staged_tree);
    expect(second.receipt.diff_digest).toBe(first.receipt.diff_digest);
    expect(edit).toHaveBeenCalledTimes(2);
  });

  it.each([true, false])("preserves the other platform baseline when native=%s", async native => {
    const baseline = native
      ? { ...PROJECT_SOURCE_SCAFFOLD, "app/page.tsx": page, "public/data/nested/items.json": raw }
      : { ...nativeProfile.scaffold, "Sources/ContentView.swift": swift, "Resources/Input/nested/items.json": raw };
    seed(baseline);
    if (native) nativeEditor();
    const response = await post({ ...body(), source_profile: native ? nativeProfile.id : "web-next-static-v1" });
    expect(response.status).toBe(200);
    const staged = await response.json() as StagedProjectRepositoryEdit;
    for (const [path, content] of Object.entries(baseline)) {
      expect(execFileSync("git", ["show", `${staged.receipt.staged_head_commit}:${path}`], { cwd: bare })).toEqual(Buffer.from(content));
    }
    expect(git(bare, ["rev-parse", "main"])).toBe(base);
  });

  it.each([true, false])("rejects changed-profile replay of an already staged edit when native=%s", async native => {
    if (native) nativeEditor();
    expect((await post({ ...body(), source_profile: native ? nativeProfile.id : "web-next-static-v1" })).status).toBe(200);
    const head = git(bare, ["rev-parse", `refs/leaf/changes/${editId}`]);
    expect((await post({ ...body(), source_profile: native ? "web-next-static-v1" : nativeProfile.id })).status).toBe(409);
    expect(edit).toHaveBeenCalledTimes(1); expect(oauth.acquireGrant).toHaveBeenCalledTimes(1);
    expect(coordination.recordStaged).toHaveBeenCalledTimes(1);
    expect(git(bare, ["rev-parse", `refs/leaf/changes/${editId}`])).toBe(head);
    expect(git(bare, ["rev-parse", "main"])).toBe(base);
  });

  it.each([true, false])("rejects changes to the other platform baseline when native=%s", async native => {
    const otherPath = native ? "app/page.tsx" : "Sources/ContentView.swift";
    seed({ [otherPath]: native ? page : swift });
    edit.mockImplementation(async input => {
      writeFileSync(join(input.repoDir, native ? "Sources/ContentView.swift" : "app/page.tsx"), native ? swift : page);
      writeFileSync(join(input.repoDir, otherPath), "changed");
      return { summary: "done", changedFiles: [] };
    });
    expect((await post({ ...body(), source_profile: native ? nativeProfile.id : "web-next-static-v1" })).status).toBe(409);
    unchanged();
  });

  it.each(["Sources/old.swift", "sources/old.swift", "PROJECT.YML", "resources/input/NESTED/items.json", "Resources/Input"])("rejects native collision %s before grants", async path => {
    seed({ [path]: "existing\n" });
    expect((await post({ ...body(), source_profile: nativeProfile.id })).status).toBe(409);
    expect(edit).not.toHaveBeenCalled(); expect(oauth.acquireGrant).not.toHaveBeenCalled(); unchanged();
  });

  it.each(["missing", "empty", "web", "scaffold", "app", "material", "baseline", "delete", "entitlements", "scripts", "dependencies", "extra", "symlink", "executable", "file-limit", "count-limit", "total-limit"])("rejects native %s output using actual Git evidence", async mode => {
    edit.mockImplementation(async input => {
      const dir = input.repoDir, product = "Sources/ContentView.swift";
      if (mode !== "missing") writeFileSync(join(dir, product), mode === "empty" ? " \n" : swift);
      const extra: Record<string, string> = { web: "app/page.tsx", scaffold: "project.yml", app: "Sources/LeafProjectApp.swift",
        material: "Resources/Input/nested/items.json", baseline: "PROMPT.md", entitlements: "Sources/entitlements.swift",
        scripts: "Sources/scripts/run.swift", dependencies: "Sources/Package.swift", extra: "output.txt" };
      if (extra[mode]) { mkdirSync(dirname(join(dir, extra[mode])), { recursive: true }); writeFileSync(join(dir, extra[mode]), "changed"); }
      if (mode === "delete") rmSync(join(dir, ".leaf/source-seed.json"));
      if (mode === "symlink") symlinkSync(join(dir, "Resources/Input/nested/items.json"), join(dir, "Sources/Link.swift"));
      if (mode === "executable") {
        if (process.platform !== "win32") chmodSync(join(dir, product), 0o755);
        else { git(dir, ["add", product]); git(dir, ["update-index", "--chmod=+x", product]); }
      }
      if (mode === "file-limit") writeFileSync(join(dir, product), "x".repeat(1048577));
      if (mode === "count-limit" || mode === "total-limit") {
        for (let i = 0; i < (mode === "count-limit" ? 64 : 5); i++) writeFileSync(join(dir, `Sources/File${i}.swift`), "x".repeat(mode === "count-limit" ? 1 : 900000));
      }
      return { summary: "done", changedFiles: [] };
    });
    expect((await post({ ...body(), source_profile: nativeProfile.id })).status).toBe(409);
    expect(oauth.settleGrant).toHaveBeenCalledWith(authority.tenantId, "lease", { usage: { cost_tokens: 0 }, stop_reason: "error" });
    unchanged();
  });

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
      { ...original, extra: true }, { ...original, tenant_id: "bad" },
      { ...original, material: [{ ...first, product_b64: first.product_b64 + "\n" }] },
      { ...original, material: [{ ...first, sha256: "b".repeat(64) }] },
      { ...original, material: [{ ...first, size_bytes: first.size_bytes + 1 }] },
      { ...original, material: [first, { ...first, path: "NESTED/items.json" }] },
      { ...original, material: [material("folder.json", "{}"), material("folder.json/item.txt", "data")] },
      { ...original, material: [material("large.txt", "x".repeat(8388609))] },
      { ...original, material: Array.from({ length: 9 }, (_, i) => material(`${i}.txt`, "x")) },
      { ...original, material: Array.from({ length: 2 }, (_, i) => material(`${i}.txt`, "x".repeat(4194305))) },
      ...["../x.json", "https://example/x.json", "CON.json", ".env.txt", "app.tsx", ".git/config.txt", "credentials.json"].map(path => ({ ...original, material: [{ ...first, path }] })),
    ];
    for (const request of invalid) {
      expect((await post(request)).status).toBe(409);
      expect((await post({ ...request, source_profile: nativeProfile.id })).status).toBe(409);
    }
    expect((await post({ ...original, source_profile: nativeProfile.id, material: [] })).status).toBe(409);
    expect((await post({ ...original, source_profile: nativeProfile.id, material: [material("large.txt", "x".repeat(1048577))] })).status).toBe(409);
    expect((await post({ ...original, source_profile: nativeProfile.id,
      material: Array.from({ length: 5 }, (_, i) => material(`${i}.txt`, "x".repeat(900000))) })).status).toBe(409);
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

  it.each(["noop", "failure", "control", "untracked", "ignored", "scaffold", "material", "delete", "symlink", "wrong-path", "native", "empty", "executable"])("rejects %s model output and settles the grant", async mode => {
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
      if (mode === "native") { mkdirSync(join(dir, "Sources")); writeFileSync(join(dir, "Sources/ContentView.swift"), swift); }
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
