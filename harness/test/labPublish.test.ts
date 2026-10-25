import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createHarness } from "../src/server.js";
import { handleLabPublish } from "../src/lab/labPublish.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { FakeTenantRepoProvider } from "../src/ports/fakes/fakeTenantRepo.js";
import { LAB_DOCUMENT_REF, TenantDocumentRepo, type LabDocumentPort, type TenantDocumentRepoOptions } from "../src/vendor/mushy-author/ports/labDocumentPort.js";
import { ForgePublicationError, ForgeRemoteAuthority } from "../src/vendor/mushy-author/ports/impl/forgeRemoteAuthority.js";
import { HARNESS_IDENTITY } from "../src/vendor/mushy-author/registry/registerTool.js";

const TENANT = "lab-publish-tenant";
const SECRET = "lab-publish-test-secret";
const SHA = "a".repeat(40);
const validBody = () => ({ expected_remote_sha: null });

describe("Lab publish", () => {
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

  function git(dir: string, args: string[], input?: string): string {
    return execFileSync("git", [
      "-c", "user.name=Lab Test", "-c", "user.email=lab@example.test",
      "-c", "commit.gpgSign=false", "-c", "core.hooksPath=/dev/null", ...args,
    ], { cwd: dir, input, encoding: "utf8", windowsHide: true }).trim();
  }

  function fixture() {
    const root = mkdtempSync(join(tmpdir(), "harness-lab-publish-"));
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
        return action({ tenantId, ownerToken: "publish-test-owner", generation: "1", lost: false, repoDirs: new Set<string>() });
      },
      async runFenced(_lease, action) { return action(); },
    };
    const port = new TenantDocumentRepo({ locate: async () => ({ dir: cache, sourceRef: remote }), lease, authority, identity: HARNESS_IDENTITY });
    return { port, cache, remote, main, authority };
  }

  function fakePort(): LabDocumentPort {
    return {
      inspect: vi.fn<LabDocumentPort["inspect"]>(async () => ({ ref: LAB_DOCUMENT_REF, head: null, commits: [], documents: [] })),
      commit: vi.fn<LabDocumentPort["commit"]>(async () => ({ ref: LAB_DOCUMENT_REF, commit: SHA })),
      publishRef: vi.fn<LabDocumentPort["publishRef"]>(async () => ({ commit: SHA })),
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
    return `http://127.0.0.1:${(server.address() as AddressInfo).port}/lab/publish`;
  }

  async function post(url: string, body: unknown = validBody(), tenant: string | null = TENANT, secret: string | null = SECRET) {
    const response = await fetch(url, {
      method: "POST", headers: {
        "content-type": "application/json",
        ...(tenant === null ? {} : { "x-tenant-id": tenant }),
        ...(secret === null ? {} : { "x-harness-secret": secret }),
      }, body: JSON.stringify(body),
    });
    return { status: response.status, body: await response.json(), cacheControl: response.headers.get("cache-control") };
  }

  it("publishes the head once after two separate commits without creating another commit", async () => {
    const { port, cache, remote, main, authority } = fixture();
    const url = listen(port);
    const save = { expected_sha: null, documents: [{ path: "lab/graphs/one.json", content: "{}" }], message: "First" };
    const first = await post(url.replace("/publish", "/commit"), save);
    expect(first.status).toBe(200);
    const second = await post(url.replace("/publish", "/commit"), { ...save, expected_sha: (first.body as { commit: string }).commit, message: "Second" });
    expect(second.status).toBe(200);
    expect(git(remote, ["for-each-ref", "--format=%(objectname)", LAB_DOCUMENT_REF])).toBe("");
    const publish = vi.spyOn(authority, "publishRef");
    const commit = vi.spyOn(port, "commit");
    const result = await post(url);
    expect(result.status).toBe(200);
    expect(result.cacheControl).toBe("no-store");
    expect(result.body).toEqual({ ref: LAB_DOCUMENT_REF, published: (second.body as { commit: string }).commit });
    expect(publish).toHaveBeenCalledTimes(1);
    expect(commit).not.toHaveBeenCalled();
    expect(git(remote, ["rev-parse", LAB_DOCUMENT_REF])).toBe((second.body as { commit: string }).commit);
    expect(git(cache, ["rev-parse", LAB_DOCUMENT_REF])).toBe((second.body as { commit: string }).commit);
    expect(git(remote, ["rev-list", "--count", LAB_DOCUMENT_REF])).toBe("2");
    expect(git(remote, ["rev-parse", "main"])).toBe(main);
  });

  it("reports both shas for stale remote CAS and accepts a fresh expected sha", async () => {
    const { port, remote } = fixture();
    const save = { tenantId: TENANT, documents: [{ path: "lab/graphs/one.json", content: "{}" }], message: "First" };
    const first = await port.commit({ ...save, expectedSha: null });
    const url = listen(port);
    expect((await post(url)).status).toBe(200);
    const second = await port.commit({ ...save, expectedSha: first.commit, message: "Second" });
    const stale = await post(url, { expected_remote_sha: SHA });
    expect(stale.status).toBe(409);
    expect(stale.body).toEqual({ error: { code: "lab_publish_conflict", expected_sha: SHA, observed_sha: first.commit } });
    expect(git(remote, ["rev-parse", LAB_DOCUMENT_REF])).toBe(first.commit);
    const retried = await post(url, { expected_remote_sha: first.commit });
    expect(retried.status).toBe(200);
    expect(retried.body).toEqual({ ref: LAB_DOCUMENT_REF, published: second.commit });
  });

  it("requires authentication, a configured port and a valid tenant header", async () => {
    const port = fakePort();
    const url = listen(port);
    for (const secret of [null, "wrong"]) expect((await post(url, validBody(), TENANT, secret)).status).toBe(401);
    const dark = await post(listen());
    expect(dark.status).toBe(501);
    expect(dark.cacheControl).toBe("no-store");
    expect((dark.body as { error: { code: string } }).error.code).toBe("lab_publish_unavailable");
    for (const tenant of [null, "../other", "x".repeat(201)]) {
      const result = await post(url, validBody(), tenant);
      expect(result.status).toBe(400);
      expect((result.body as { error: { code: string } }).error.code).toBe("lab_tenant_required");
    }
    expect(port.publishRef).not.toHaveBeenCalled();
  });

  it("returns a fixed error for malformed JSON and reader validation failures", async () => {
    const port = fakePort();
    const url = listen(port);
    for (const body of ['{"expected_remote_sha":', "not JSON", '{"expected_remote_sha":null,"expected_remote_sha":null}', new Uint8Array([0xff])]) {
      const response = await fetch(url, {
        method: "POST",
        headers: { "content-type": "application/json", "x-tenant-id": TENANT, "x-harness-secret": SECRET },
        body,
      });
      expect(response.status).toBe(422);
      expect(response.headers.get("cache-control")).toBe("no-store");
      expect(await response.json()).toEqual({
        error: { code: "lab_publish_invalid", pointer: "", message: "Invalid JSON body" },
      });
    }
    expect(port.publishRef).not.toHaveBeenCalled();
  });

  it("validates closed bodies and nonzero lowercase shas with JSON pointers", async () => {
    const port = fakePort();
    const url = listen(port);
    const cases: [unknown, string][] = [
      [null, ""], [[], ""], [1, ""], [{}, "/expected_remote_sha"],
      ...["A".repeat(40), "0".repeat(40), SHA + "\n", "abc", 1, false].map(expected_remote_sha => [{ expected_remote_sha }, "/expected_remote_sha"] as [unknown, string]),
      [{ ...validBody(), tenant_id: "other" }, "/tenant_id"],
      [{ ...validBody(), "a~/b": true }, "/a~0~1b"],
    ];
    for (const [body, pointer] of cases) {
      const result = await post(url, body);
      expect(result.status).toBe(422);
      expect((result.body as { error: { code: string; pointer: string } }).error).toMatchObject({ code: "lab_publish_invalid", pointer });
    }
    const oversized = await post(url, { ...validBody(), padding: "é".repeat(512) });
    expect(oversized.status).toBe(413);
    expect(oversized.body).toEqual({ error: { code: "lab_body_too_large" } });
    expect(oversized.cacheControl).toBe("no-store");
    expect(port.publishRef).not.toHaveBeenCalled();
  });

  it.each([
    ["not-published", 503, "lab_publish_not_published"],
    ["refused", 403, "lab_publish_refused"],
    ["unknown", 502, "lab_publish_unknown"],
  ] as const)("maps %s authority failure without retry or exception text", async (state, status, code) => {
    const port = fakePort();
    port.publishRef = vi.fn(async () => { throw new ForgePublicationError(state, "private authority details"); });
    const result = await post(listen(port));
    expect(result.status).toBe(status);
    expect(result.body).toEqual({ error: { code } });
    expect(port.publishRef).toHaveBeenCalledExactlyOnceWith(TENANT, null);
    expect(port.commit).not.toHaveBeenCalled();
    expect(port.inspect).not.toHaveBeenCalled();
  });

  it("maps repository mapping refusal to lab_publish_refused", async () => {
    const { port, cache } = fixture();
    git(cache, ["config", "remote.origin.url", "refused"]);
    expect(await handleLabPublish(port, TENANT, validBody())).toEqual({ status: 403, body: { error: { code: "lab_publish_refused" } } });
  });

  it("scrubs unexpected failures and permits retry without a commit", async () => {
    const port = fakePort();
    vi.mocked(port.publishRef).mockRejectedValueOnce(new Error("private repository path"));
    const url = listen(port);
    const result = await post(url);
    expect(result.status).toBe(500);
    expect(result.body).toEqual({ error: { code: "lab_publish_failed", message: "Lab publish failed" } });
    expect(port.publishRef).toHaveBeenCalledTimes(1);
    expect((await post(url)).body).toEqual({ ref: LAB_DOCUMENT_REF, published: SHA });
    expect(port.publishRef).toHaveBeenCalledTimes(2);
    expect(port.commit).not.toHaveBeenCalled();
  });
});
