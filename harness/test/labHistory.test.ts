import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createHarness } from "../src/server.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { FakeTenantRepoProvider } from "../src/ports/fakes/fakeTenantRepo.js";
import { LAB_GENERATION_CAPABILITY, LabHistoryError, TenantDocumentRepo, type LabDocumentPort, type TenantDocumentRepoOptions } from "../src/vendor/mushy-author/ports/labDocumentPort.js";
import { ForgePublicationError, ForgeRemoteAuthority } from "../src/vendor/mushy-author/ports/impl/forgeRemoteAuthority.js";
import { HARNESS_IDENTITY } from "../src/vendor/mushy-author/registry/registerTool.js";

const TENANT = "lab-history-tenant";
const SECRET = "lab-history-test-secret";
const SHA = "a".repeat(40);
const OP = "a".repeat(32);
const MISSING = "b".repeat(32);
const DIGEST = "c".repeat(64);
const PATH = "lab/graphs/one.json";
const trailers = { "Lab-Op": OP, "Lab-Doc": "one", "Lab-Revision": "1", "Lab-Sha256": DIGEST };
const message = "Save\n\n" + Object.entries(trailers).map(([key, value]) => `${key}: ${value}`).join("\n");

describe("Lab operation lookup and history routes", () => {
  const directories: string[] = [];
  const servers: Server[] = [];
  afterEach(async () => {
    await Promise.all(servers.splice(0).map(server => new Promise<void>((resolve, reject) => {
      server.close(error => error ? reject(error) : resolve());
      server.closeAllConnections();
    })));
    vi.restoreAllMocks();
    for (const directory of directories.splice(0)) rmSync(directory, { recursive: true, force: true });
  });

  function git(dir: string, args: string[], input?: string): string {
    return execFileSync("git", ["-c", "user.name=Lab Test", "-c", "user.email=lab@example.test",
      "-c", "commit.gpgSign=false", "-c", "core.hooksPath=/dev/null", ...args],
    { cwd: dir, input, encoding: "utf8", windowsHide: true }).trim();
  }

  function fixture(maxHistory?: number) {
    const root = mkdtempSync(join(tmpdir(), "harness-lab-history-"));
    directories.push(root);
    const remote = join(root, "remote.git"), cache = join(root, "cache.git");
    git(root, ["init", "--bare", remote]);
    const tree = git(remote, ["mktree"], "");
    const main = git(remote, ["commit-tree", tree], "Main\n");
    git(remote, ["update-ref", "refs/heads/main", main]);
    git(root, ["clone", "--bare", remote, cache]);
    const authority = new ForgeRemoteAuthority({ locate: async () => remote,
      credentials: async (tenantId, remoteUrl) => ({ tenantId, remoteUrl }), allowLocalPathsForTests: true });
    const lease: TenantDocumentRepoOptions["lease"] = {
      async withLease(tenantId, action) {
        return action({ tenantId, ownerToken: "history-test-owner", generation: "1", lost: false, repoDirs: new Set<string>() });
      },
      async runFenced(_lease, action) { return action(); },
    };
    const port = new TenantDocumentRepo({ locate: async () => ({ dir: cache, sourceRef: remote }), lease, authority, identity: HARNESS_IDENTITY, maxHistory });
    const save = (expectedSha: string | null, text = message, content = "{}") => port.commit({ tenantId: TENANT,
      expectedSha, documents: [{ path: PATH, content }], message: text });
    return { port, cache, tree, main, save };
  }

  function listen(labDocuments?: LabDocumentPort): string {
    const server = createHarness({ oauth: new FakeOAuthGrantProvider(), tenantRepo: new FakeTenantRepoProvider(process.cwd()),
      broker: new FakeBrokerApsClient(), agentRunner: new FakeAgentRunner() },
    { auth: { enabled: true, secret: SECRET }, ...(labDocuments ? { labDocuments } : {}) }).listen(0);
    servers.push(server);
    return `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  }

  async function post(base: string, operation: string, body: unknown, tenant: string | null = TENANT, secret: string | null = SECRET, raw = false) {
    const response = await fetch(`${base}/lab/${operation}`, { method: "POST", headers: {
      "content-type": "application/json", ...(tenant === null ? {} : { "x-tenant-id": tenant }),
      ...(secret === null ? {} : { "x-harness-secret": secret }),
    }, body: raw ? body as string : JSON.stringify(body) });
    expect(response.headers.get("x-leaf-lab-capability")).toBe(LAB_GENERATION_CAPABILITY);
    expect(response.headers.get("cache-control")).toBe("no-store");
    return { status: response.status, body: await response.json() };
  }

  it("returns operation identity in snake case and null for missing operations at the pinned head", async () => {
    const { port, save } = fixture();
    const first = await save(null);
    const second = await save(first.commit, "Second", '{"version":2}');
    const call = vi.spyOn(port, "lookupOps");
    expect(await post(listen(port), "ops", { op_ids: [OP, MISSING], head: second.commit })).toEqual({ status: 200, body: { ops: {
      [OP]: { commit: first.commit, doc_id: "one", revision: 1, sha256: DIGEST }, [MISSING]: null,
    } } });
    expect(call).toHaveBeenCalledExactlyOnceWith(TENANT, { opIds: [OP, MISSING], head: second.commit });
  });

  it("accepts 64 distinct operation IDs at the request boundary", async () => {
    const { port, save } = fixture();
    const first = await save(null, "No operation trailers");
    const ids = Array.from({ length: 64 }, (_, index) => index.toString(16).padStart(32, "0"));
    expect(await post(listen(port), "ops", { op_ids: ids, head: first.commit })).toEqual({ status: 200, body: { ops: Object.fromEntries(ids.map(id => [id, null])) } });
  });

  it("returns ordered commits with paths, trailers, an exclusive since and pagination", async () => {
    const { port, save } = fixture();
    const first = await save(null);
    const second = await save(first.commit, "Second", '{"version":2}');
    const base = listen(port);
    const call = vi.spyOn(port, "changes");
    const firstEntry = { commit: first.commit, parent: null, changed_paths: [PATH], trailers };
    const secondEntry = { commit: second.commit, parent: first.commit, changed_paths: [PATH], trailers: {} };
    expect(await post(base, "changes", { since: null, head: second.commit, limit: 1 })).toEqual({ status: 200, body: { commits: [firstEntry], next: first.commit } });
    expect(call).toHaveBeenLastCalledWith(TENANT, { since: null, head: second.commit, limit: 1 });
    expect(await post(base, "changes", { since: null, head: second.commit, limit: 1, after: first.commit })).toEqual({ status: 200, body: { commits: [secondEntry], next: null } });
    expect(call).toHaveBeenLastCalledWith(TENANT, { since: null, head: second.commit, limit: 1, after: first.commit });
    expect(await post(base, "changes", { since: first.commit, head: second.commit })).toEqual({ status: 200, body: { commits: [secondEntry], next: null } });
    expect(await post(base, "changes", { since: second.commit, head: second.commit, limit: 1000 })).toEqual({ status: 200, body: { commits: [], next: null } });
    expect(await post(base, "changes", { since: null, head: second.commit })).toEqual({ status: 200, body: { commits: [firstEntry, secondEntry], next: null } });
  });

  it("maps duplicate operation trailers to ambiguity", async () => {
    const { port, save } = fixture();
    const first = await save(null);
    const second = await save(first.commit, message, '{"version":2}');
    expect(await post(listen(port), "ops", { op_ids: [OP], head: second.commit })).toEqual({ status: 409, body: { error: { code: "lab_ops_ambiguous" } } });
  });

  it("maps merge history, non-ancestor since, invalid heads and invalid cursors", async () => {
    const { port, cache, tree, main, save } = fixture();
    const first = await save(null);
    const side = git(cache, ["commit-tree", tree], "Side\n");
    const merge = git(cache, ["commit-tree", tree, "-p", first.commit, "-p", side], "Merge\n");
    const base = listen(port);
    for (const operation of ["ops", "changes"]) {
      const fields = operation === "ops" ? { op_ids: [OP] } : { since: null };
      expect(await post(base, operation, { ...fields, head: merge })).toEqual({ status: 409, body: { error: { code: "lab_history_nonlinear" } } });
      expect(await post(base, operation, { ...fields, head: tree })).toEqual({ status: 422, body: { error: { code: "lab_history_invalid" } } });
    }
    expect(await post(base, "changes", { since: main, head: first.commit })).toEqual({ status: 409, body: { error: { code: "lab_history_not_ancestor" } } });
    expect(await post(base, "changes", { since: null, head: first.commit, after: main })).toEqual({ status: 422, body: { error: { code: "lab_history_invalid" } } });
  });

  it("maps bounded traversal failures on both history routes", async () => {
    const { port, save } = fixture(1);
    const first = await save(null);
    const second = await save(first.commit, "Second", '{"version":2}');
    const base = listen(port);
    for (const [operation, body] of [["ops", { op_ids: [OP], head: second.commit }], ["changes", { since: null, head: second.commit, limit: 1 }]] as const) {
      expect(await post(base, operation, body)).toEqual({ status: 409, body: { error: { code: "lab_history_too_long" } } });
    }
  });

  it.each(["ops", "changes"] as const)("gates %s before port calls and enforces body caps and JSON parsing", async operation => {
    const { port } = fixture();
    const base = listen(port);
    const body = operation === "ops" ? { op_ids: [OP], head: SHA } : { since: null, head: SHA };
    const call = vi.spyOn(port, operation === "ops" ? "lookupOps" : "changes");
    for (const secret of [null, "wrong"]) expect((await post(base, operation, body, TENANT, secret)).status).toBe(401);
    expect(await post(listen(), operation, body)).toEqual({ status: 501, body: { error: { code: `lab_${operation}_unavailable`, message: `Lab ${operation} is unavailable` } } });
    for (const tenant of [null, "../other", "x".repeat(201)]) {
      const result = await post(base, operation, body, tenant);
      expect(result.status).toBe(400);
      expect(result.body).toMatchObject({ error: { code: "lab_tenant_required" } });
    }
    for (const raw of ["{", "not JSON", '{"head":1,"head":2}']) {
      expect(await post(base, operation, raw, TENANT, SECRET, true)).toEqual({ status: 422, body: { error: { code: `lab_${operation}_invalid`, pointer: "", message: "Invalid JSON body" } } });
    }
    expect(await post(base, operation, { padding: "x".repeat(operation === "ops" ? 8192 : 2048) })).toEqual({ status: 413, body: { error: { code: "lab_body_too_large" } } });
    expect(call).not.toHaveBeenCalled();
  });

  it("refuses operation grammar, duplicate IDs and unknown fields with pointers", async () => {
    const { port } = fixture();
    const base = listen(port);
    const valid = { op_ids: [OP], head: SHA };
    const cases: [unknown, string][] = [
      [null, ""], [[], ""], [{ head: SHA }, "/op_ids"], [{ op_ids: [OP] }, "/head"],
      [{ ...valid, "a~/b": 1 }, "/a~0~1b"], [{ ...valid, op_ids: [] }, "/op_ids"],
      [{ ...valid, op_ids: Array.from({ length: 65 }, (_, i) => i.toString(16).padStart(32, "0")) }, "/op_ids"],
      [{ ...valid, op_ids: [OP, OP] }, "/op_ids/1"],
    ];
    for (const id of [null, 1, "A".repeat(32), "a".repeat(31), OP + "\n"]) cases.push([{ ...valid, op_ids: [id] }, "/op_ids/0"]);
    for (const head of [null, 1, "A".repeat(40), "a".repeat(39), SHA + "\n"]) cases.push([{ ...valid, head }, "/head"]);
    const call = vi.spyOn(port, "lookupOps");
    for (const [body, pointer] of cases) {
      const result = await post(base, "ops", body);
      expect(result.status).toBe(422);
      expect(result.body).toMatchObject({ error: { code: "lab_ops_invalid", pointer } });
    }
    expect(call).not.toHaveBeenCalled();
  });

  it("refuses changes grammar and closed fields with pointers", async () => {
    const { port } = fixture();
    const base = listen(port);
    const valid = { since: null, head: SHA };
    const cases: [unknown, string][] = [[null, ""], [[], ""], [{ head: SHA }, "/since"], [{ since: null }, "/head"], [{ ...valid, "a~/b": 1 }, "/a~0~1b"]];
    for (const field of ["since", "head", "after"]) {
      for (const value of [1, "A".repeat(40), "a".repeat(39), SHA + "\n"]) cases.push([{ ...valid, [field]: value }, `/${field}`]);
    }
    for (const field of ["head", "after"]) cases.push([{ ...valid, [field]: null }, `/${field}`]);
    for (const limit of [null, "1", 0, -1, 1001, 1.5, true]) cases.push([{ ...valid, limit }, "/limit"]);
    const call = vi.spyOn(port, "changes");
    for (const [body, pointer] of cases) {
      const result = await post(base, "changes", body);
      expect(result.status).toBe(422);
      expect(result.body).toMatchObject({ error: { code: "lab_changes_invalid", pointer } });
    }
    expect(call).not.toHaveBeenCalled();
  });

  it.each(["ops", "changes"] as const)("scrubs %s repository refusal and unexpected failures and preserves history codes", async operation => {
    const { port } = fixture();
    const base = listen(port);
    const body = operation === "ops" ? { op_ids: [OP], head: SHA } : { since: null, head: SHA };
    const call = vi.spyOn(port, operation === "ops" ? "lookupOps" : "changes");
    call.mockRejectedValueOnce(new ForgePublicationError("refused", "private mapping"));
    expect(await post(base, operation, body)).toEqual({ status: 403, body: { error: { code: "lab_repository_refused" } } });
    call.mockRejectedValueOnce(new Error("private path"));
    expect(await post(base, operation, body)).toEqual({ status: 500, body: { error: { code: `lab_${operation}_failed`, message: `Lab ${operation} failed` } } });
    for (const code of ["lab_history_nonlinear", "lab_history_too_long", "lab_ops_ambiguous", "lab_history_not_ancestor", "lab_history_invalid"] as const) {
      call.mockRejectedValueOnce(new LabHistoryError(code, "private history detail"));
      expect(await post(base, operation, body)).toEqual({ status: code === "lab_history_invalid" ? 422 : 409, body: { error: { code } } });
    }
  });
});
