import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtempSync, rmSync } from "node:fs";
import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createHarness } from "../src/server.js";
import { handleLabInspect } from "../src/lab/labInspect.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { FakeTenantRepoProvider } from "../src/ports/fakes/fakeTenantRepo.js";
import { LAB_DOCUMENT_REF, TenantDocumentRepo, type LabDocumentInspection, type LabDocumentPort, type TenantDocumentRepoOptions } from "../src/vendor/mushy-author/ports/labDocumentPort.js";
import { ForgePublicationError, ForgeRemoteAuthority } from "../src/vendor/mushy-author/ports/impl/forgeRemoteAuthority.js";
import { HARNESS_IDENTITY } from "../src/vendor/mushy-author/registry/registerTool.js";

const TENANT = "lab-inspect-tenant";
const SECRET = "lab-inspect-test-secret";
const empty: LabDocumentInspection = { ref: LAB_DOCUMENT_REF, head: null, commits: [], documents: [] };

describe("Lab inspect", () => {
  const directories: string[] = [];
  const servers: Server[] = [];
  afterEach(async () => {
    await Promise.all(servers.splice(0).map(server => new Promise<void>((resolve, reject) => {
      server.close(error => error ? reject(error) : resolve());
      server.closeAllConnections();
    })));
    for (const directory of directories.splice(0)) rmSync(directory, { recursive: true, force: true });
    vi.restoreAllMocks();
  });

  function git(dir: string, args: string[], input?: string | Buffer): string {
    return execFileSync("git", [
      "-c", "user.name=Lab Test", "-c", "user.email=lab@example.test",
      "-c", "commit.gpgSign=false", "-c", "core.hooksPath=/dev/null", ...args,
    ], { cwd: dir, input, encoding: "utf8", windowsHide: true, maxBuffer: 4 * 1024 * 1024 }).trim();
  }

  function fixture() {
    const root = mkdtempSync(join(tmpdir(), "harness-lab-inspect-"));
    directories.push(root);
    const remote = join(root, "remote.git");
    const cache = join(root, "cache.git");
    git(root, ["init", "--bare", remote]);
    const tree = git(remote, ["mktree"], "");
    const main = git(remote, ["commit-tree", tree], "Main\n");
    git(remote, ["update-ref", "refs/heads/main", main]);
    git(root, ["clone", "--bare", remote, cache]);
    const authority = new ForgeRemoteAuthority({
      locate: async () => remote,
      credentials: async (tenantId, remoteUrl) => ({ tenantId, remoteUrl }),
      allowLocalPathsForTests: true,
    });
    const lease: TenantDocumentRepoOptions["lease"] = {
      async withLease(tenantId, action) {
        return action({ tenantId, ownerToken: "inspect-test-owner", generation: "1", lost: false, repoDirs: new Set<string>() });
      },
      async runFenced(_lease, action) { return action(); },
    };
    const port = new TenantDocumentRepo({ locate: async () => ({ dir: cache, sourceRef: remote }), lease, authority, identity: HARNESS_IDENTITY });
    return { port, cache, remote, tree, lease };
  }

  function fakePort(): LabDocumentPort {
    return {
      inspect: vi.fn(async () => empty),
      commit: vi.fn<LabDocumentPort["commit"]>(async () => ({ ref: LAB_DOCUMENT_REF, commit: "a".repeat(40) })),
      publishRef: vi.fn(async () => ({ commit: "a".repeat(40) })),
      generation: vi.fn<LabDocumentPort["generation"]>(async () => ({ generation: null, generationOid: null, head: null })),
      fence: vi.fn<LabDocumentPort["fence"]>(async () => { throw new Error("fence is not used by this route"); }),
      lookupOps: vi.fn<LabDocumentPort["lookupOps"]>(async () => ({ ops: {} })),
      changes: vi.fn<LabDocumentPort["changes"]>(async () => ({ commits: [], next: null })),
    };
  }

  function listen(labDocuments?: LabDocumentPort): string {
    const server = createHarness({
      oauth: new FakeOAuthGrantProvider(), tenantRepo: new FakeTenantRepoProvider(process.cwd()),
      broker: new FakeBrokerApsClient(), agentRunner: new FakeAgentRunner(),
    }, { auth: { enabled: true, secret: SECRET }, ...(labDocuments ? { labDocuments } : {}) }).listen(0);
    servers.push(server);
    return `http://127.0.0.1:${(server.address() as AddressInfo).port}/lab/inspect`;
  }

  async function post(url: string, body: unknown = { limit: 200 }, tenant: string | null = TENANT, secret: string | null = SECRET) {
    const response = await fetch(url, {
      method: "POST", headers: {
        "content-type": "application/json",
        ...(tenant === null ? {} : { "x-tenant-id": tenant }),
        ...(secret === null ? {} : { "x-harness-secret": secret }),
      }, body: JSON.stringify(body),
    });
    return { status: response.status, body: await response.json(), cacheControl: response.headers.get("cache-control") };
  }

  it("reads an empty ref under the lease and fence without changing objects or refs", async () => {
    const { port, cache, lease } = fixture();
    const withLease = vi.spyOn(lease, "withLease");
    const runFenced = vi.spyOn(lease, "runFenced");
    const refs = git(cache, ["show-ref"]);
    const objects = git(cache, ["cat-file", "--batch-all-objects", "--batch-check"]);
    expect(await port.inspect(TENANT, { limit: 1 })).toEqual(empty);
    expect(withLease).toHaveBeenCalledTimes(1);
    expect(runFenced).toHaveBeenCalledTimes(1);
    expect(git(cache, ["show-ref"])).toBe(refs);
    expect(git(cache, ["cat-file", "--batch-all-objects", "--batch-check"])).toBe(objects);
  });

  it("returns newest first history, full messages, and exact head blob digests without writing", async () => {
    const { port, cache, remote } = fixture();
    const path = "lab/graphs/one.json";
    const first = await port.commit({ tenantId: TENANT, expectedSha: null, documents: [{ path, content: "{}" }], message: "First\n\nFull body\n" });
    const content = '{"label":"é☀"}\n';
    const second = await port.commit({ tenantId: TENANT, expectedSha: first.commit, documents: [
      { path, content }, { path: "lab/widgets/two.json", content: "{}" },
    ], message: "Second\n" });
    const refs = git(cache, ["show-ref"]);
    const remoteRefs = git(remote, ["show-ref"]);
    const objects = git(cache, ["cat-file", "--batch-all-objects", "--batch-check"]);
    const result = await post(listen(port));
    expect(result.status).toBe(200);
    expect(result.cacheControl).toBe("no-store");
    expect(result.body).toEqual({ ref: LAB_DOCUMENT_REF, head: second.commit, commits: [
      { commit: second.commit, parent: first.commit, message: "Second\n" },
      { commit: first.commit, parent: null, message: "First\n\nFull body\n" },
    ], documents: [
      { path, blob: git(cache, ["rev-parse", `${second.commit}:${path}`]), sha256: createHash("sha256").update(content).digest("hex"), bytes: Buffer.byteLength(content) },
      { path: "lab/widgets/two.json", blob: git(cache, ["rev-parse", `${second.commit}:lab/widgets/two.json`]), sha256: createHash("sha256").update("{}").digest("hex"), bytes: 2 },
    ] });
    expect((await port.inspect(TENANT, { limit: 1 })).commits).toEqual((result.body as LabDocumentInspection).commits.slice(0, 1));
    expect(git(cache, ["show-ref"])).toBe(refs);
    expect(git(remote, ["show-ref"])).toBe(remoteRefs);
    expect(git(cache, ["cat-file", "--batch-all-objects", "--batch-check"])).toBe(objects);
  });

  it("validates direct repository limit bounds", async () => {
    const { port } = fixture();
    for (const limit of [0, -1, 201, 1.5, NaN, Infinity]) {
      await expect(port.inspect(TENANT, { limit })).rejects.toThrow("invalid inspect limit");
    }
    expect(await port.inspect(TENANT, { limit: 200 })).toEqual(empty);
  });

  it("requires authentication, a configured port and an authoritative tenant header", async () => {
    const port = fakePort();
    const url = listen(port);
    for (const secret of [null, "wrong"]) expect((await post(url, { limit: 1 }, TENANT, secret)).status).toBe(401);
    expect((await post(listen())).body).toEqual({ error: { code: "lab_inspect_unavailable", message: "Lab inspect is unavailable" } });
    expect((await post(listen())).status).toBe(501);
    for (const tenant of [null, "../other", "x".repeat(201)]) {
      const result = await post(url, { limit: 1 }, tenant);
      expect(result.status).toBe(400);
      expect((result.body as { error: { code: string } }).error.code).toBe("lab_tenant_required");
    }
    expect(port.inspect).not.toHaveBeenCalled();
  });

  it("returns a fixed error for malformed JSON and reader validation failures", async () => {
    const port = fakePort();
    const url = listen(port);
    for (const body of ['{"limit":', "not JSON", '{"limit":1,"limit":2}', new Uint8Array([0xff])]) {
      const response = await fetch(url, {
        method: "POST",
        headers: { "content-type": "application/json", "x-tenant-id": TENANT, "x-harness-secret": SECRET },
        body,
      });
      expect(response.status).toBe(422);
      expect(response.headers.get("cache-control")).toBe("no-store");
      expect(await response.json()).toEqual({
        error: { code: "lab_inspect_invalid", pointer: "", message: "Invalid JSON body" },
      });
    }
    expect(port.inspect).not.toHaveBeenCalled();
  });

  it("rejects closed-body and limit violations with JSON pointers", async () => {
    const port = fakePort();
    const cases: [unknown, string][] = [
      [null, ""], [[], ""], [1, ""], [{}, "/limit"],
      ...[null, "1", 0, 201, 1.5, true].map(limit => [{ limit }, "/limit"] as [unknown, string]),
      [{ limit: 1, tenant_id: "other" }, "/tenant_id"], [{ limit: 1, "a~/b": 1 }, "/a~0~1b"],
    ];
    for (const [body, pointer] of cases) {
      const result = await handleLabInspect(port, TENANT, body);
      expect(result.status).toBe(422);
      expect(result.body).toMatchObject({ error: { code: "lab_inspect_invalid", pointer } });
    }
    expect(port.inspect).not.toHaveBeenCalled();
    const oversized = await post(listen(port), { limit: 1, padding: "x".repeat(4096) });
    expect(oversized.status).toBe(413);
    expect(oversized.body).toEqual({ error: { code: "lab_body_too_large" } });
    expect(oversized.cacheControl).toBe("no-store");
    expect(port.inspect).not.toHaveBeenCalled();
  });

  it("maps mapping refusal to 403 and scrubs other failures without retry", async () => {
    const { port, cache } = fixture();
    git(cache, ["config", "remote.origin.url", "refused"]);
    expect(await handleLabInspect(port, TENANT, { limit: 1 })).toEqual({ status: 403, body: { error: { code: "lab_repository_refused" } } });
    const fake = fakePort();
    fake.inspect = vi.fn(async () => { throw new Error("private repository path"); });
    expect(await handleLabInspect(fake, TENANT, { limit: 1 })).toEqual({ status: 500, body: { error: { code: "lab_inspect_failed", message: "Lab inspect failed" } } });
    expect(fake.inspect).toHaveBeenCalledExactlyOnceWith(TENANT, { limit: 1 });
    fake.inspect = vi.fn(async () => { throw new ForgePublicationError("unknown", "private detail"); });
    expect((await handleLabInspect(fake, TENANT, { limit: 1 })).status).toBe(500);
    expect(fake.commit).not.toHaveBeenCalled();
    expect(fake.publishRef).not.toHaveBeenCalled();
  });

  it("refuses oversized messages, blobs, excessive entries and forbidden paths or modes", async () => {
    const { port, cache, tree } = fixture();
    const setHead = (headTree: string, message = "Inspect\n") => {
      const commit = git(cache, ["commit-tree", headTree], message);
      git(cache, ["update-ref", LAB_DOCUMENT_REF, commit]);
    };
    setHead(tree, "x".repeat(16 * 1024 + 1));
    await expect(port.inspect(TENANT, { limit: 1 })).rejects.toThrow();
    const blob = git(cache, ["hash-object", "-w", "--stdin"], Buffer.alloc(1024 * 1024 + 1, 0xff));
    const wrap = (entries: string) => {
      const graphs = git(cache, ["mktree", "-z"], entries);
      const lab = git(cache, ["mktree"], `040000 tree ${graphs}\tgraphs\n`);
      return git(cache, ["mktree"], `040000 tree ${lab}\tlab\n`);
    };
    setHead(wrap(`100644 blob ${blob}\tone.json\0`));
    await expect(port.inspect(TENANT, { limit: 1 })).rejects.toThrow();
    const small = git(cache, ["hash-object", "-w", "--stdin"], "{}");
    const boundaryBytes = Buffer.alloc(1024 * 1024, 0xff);
    const boundaryBlob = git(cache, ["hash-object", "-w", "--stdin"], boundaryBytes);
    const boundaryMessage = "x".repeat(16 * 1024 - 1) + "\n";
    setHead(wrap(`100644 blob ${boundaryBlob}\tone.json\0`), boundaryMessage);
    const boundary = await port.inspect(TENANT, { limit: 1 });
    expect(boundary.commits[0]!.message).toBe(boundaryMessage);
    expect(boundary.documents).toEqual([{
      path: "lab/graphs/one.json", blob: boundaryBlob,
      sha256: createHash("sha256").update(boundaryBytes).digest("hex"), bytes: boundaryBytes.length,
    }]);
    for (const entries of [
      `100755 blob ${small}\tone.json\0`, `100644 blob ${small}\tforbidden.txt\0`,
      Array.from({ length: 2001 }, (_, i) => `100644 blob ${small}\tg${i}.json\0`).join(""),
    ]) {
      setHead(wrap(entries));
      await expect(port.inspect(TENANT, { limit: 1 })).rejects.toThrow();
    }
  });
});
