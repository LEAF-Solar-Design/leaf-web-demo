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
import { LAB_GENERATION_CAPABILITY, TenantDocumentRepo, type LabDocumentPort, type TenantDocumentRepoOptions } from "../src/vendor/mushy-author/ports/labDocumentPort.js";
import { ForgePublicationError, ForgeRemoteAuthority } from "../src/vendor/mushy-author/ports/impl/forgeRemoteAuthority.js";
import { HARNESS_IDENTITY } from "../src/vendor/mushy-author/registry/registerTool.js";

const TENANT = "lab-generation-tenant";
const SECRET = "lab-generation-test-secret";
const SHA = "a".repeat(40);
const fenceBody = () => ({ transition_id: "a".repeat(32), expected_generation: null, new_generation: "1", expected_head: null });

describe("Lab generation and fence routes", () => {
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

  function fixture() {
    const root = mkdtempSync(join(tmpdir(), "harness-lab-generation-"));
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
        return action({ tenantId, ownerToken: "generation-test-owner", generation: "1", lost: false, repoDirs: new Set<string>() });
      },
      async runFenced(_lease, action) { return action(); },
    };
    return { port: new TenantDocumentRepo({ locate: async () => ({ dir: cache, sourceRef: remote }), lease, authority, identity: HARNESS_IDENTITY }), cache };
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

  it("reads generation, fences, replays the receipt, and rejects a legacy commit after fencing", async () => {
    const { port } = fixture();
    const base = listen(port);
    expect(await post(base, "generation", {})).toEqual({ status: 200, body: { generation: null, generation_oid: null, head: null } });
    const call = vi.spyOn(port, "fence");
    const receipt = { transition_id: "a".repeat(32), from_generation: null, to_generation: "1", barrier_head: null };
    expect(await post(base, "fence", fenceBody())).toEqual({ status: 200, body: { receipt, head: null } });
    expect(call).toHaveBeenCalledExactlyOnceWith(TENANT, { transitionId: "a".repeat(32), expectedGeneration: null, newGeneration: "1", expectedHead: null });
    expect(await post(base, "generation", {})).toEqual({ status: 200, body: { generation: "1", generation_oid: expect.stringMatching(/^[0-9a-f]{40}$/), head: null } });
    expect(await post(base, "fence", fenceBody())).toEqual({ status: 200, body: { receipt, head: null } });
    const save = { expected_sha: null, documents: [{ path: "lab/graphs/one.json", content: "{}" }], message: "Save" };
    expect(await post(base, "commit", save)).toEqual({ status: 409, body: { error: { code: "lab_generation_conflict" } } });
    const committed = await post(base, "commit", { ...save, generation: "1" });
    expect(committed.status).toBe(200);
    const head = (committed.body as { commit: string }).commit;
    const next = { transition_id: "b".repeat(32), expected_generation: "1", new_generation: "2", expected_head: head };
    expect(await post(base, "fence", next)).toEqual({ status: 200, body: {
      receipt: { transition_id: next.transition_id, from_generation: "1", to_generation: "2", barrier_head: head }, head,
    } });
  });

  it("maps wrong head and wrong generation to a fixed conflict", async () => {
    const { port } = fixture();
    const base = listen(port);
    for (const fields of [{ expected_head: SHA }, { expected_generation: "1" }]) {
      expect(await post(base, "fence", { ...fenceBody(), ...fields })).toEqual({ status: 409, body: { error: { code: "lab_generation_conflict" } } });
    }
  });

  it("accepts a 19 digit decimal generation and advertises capability on unknown Lab routes", async () => {
    const { port } = fixture();
    const base = listen(port);
    const generation = "1234567890123456789";
    expect((await post(base, "fence", { ...fenceBody(), new_generation: generation })).status).toBe(200);
    expect((await post(base, "generation", {})).body).toMatchObject({ generation });
    expect((await post(base, "unknown", {})).status).toBe(404);
  });

  it.each(["generation", "fence"])("gates %s, caps its body and handles malformed JSON", async operation => {
    const { port } = fixture();
    const base = listen(port);
    const body = operation === "fence" ? fenceBody() : {};
    const call = vi.spyOn(port, operation === "fence" ? "fence" : "generation");
    for (const secret of [null, "wrong"]) expect((await post(base, operation, body, TENANT, secret)).status).toBe(401);
    expect(await post(listen(), operation, body)).toEqual({ status: 501, body: { error: { code: `lab_${operation}_unavailable`, message: `Lab ${operation} is unavailable` } } });
    for (const tenant of [null, "../other", "x".repeat(201)]) {
      const result = await post(base, operation, body, tenant);
      expect(result.status).toBe(400);
      expect(result.body).toMatchObject({ error: { code: "lab_tenant_required" } });
    }
    for (const raw of ["{", "not JSON", '{"a":1,"a":2}']) {
      expect(await post(base, operation, raw, TENANT, SECRET, true)).toEqual({ status: 422, body: { error: { code: `lab_${operation}_invalid`, pointer: "", message: "Invalid JSON body" } } });
    }
    expect(await post(base, operation, { padding: "x".repeat(1024) })).toEqual({ status: 413, body: { error: { code: "lab_body_too_large" } } });
    expect(call).not.toHaveBeenCalled();
  });

  it("refuses closed-field and grammar violations with escaped pointers", async () => {
    const { port } = fixture();
    const base = listen(port);
    const generationCall = vi.spyOn(port, "generation"), fenceCall = vi.spyOn(port, "fence");
    for (const [body, pointer] of [[null, ""], [[], ""], [{ "a~/b": true }, "/a~0~1b"]] as [unknown, string][]) {
      const result = await post(base, "generation", body);
      expect(result.status).toBe(422);
      expect(result.body).toMatchObject({ error: { code: "lab_generation_invalid", pointer } });
    }
    const cases: [unknown, string][] = [[null, ""], [[], ""], [{ ...fenceBody(), "a~/b": true }, "/a~0~1b"]];
    for (const field of ["transition_id", "expected_generation", "new_generation", "expected_head"]) {
      const missing: Record<string, unknown> = { ...fenceBody() };
      delete missing[field];
      cases.push([missing, `/${field}`]);
    }
    for (const value of ["", "0", "01", "-1", "1.0", "1\n", "1".repeat(20), 1]) {
      for (const field of ["expected_generation", "new_generation"]) cases.push([{ ...fenceBody(), [field]: value }, `/${field}`]);
    }
    cases.push([{ ...fenceBody(), new_generation: null }, "/new_generation"]);
    for (const value of [null, "A".repeat(32), "a".repeat(31), "a".repeat(32) + "\n", 1]) cases.push([{ ...fenceBody(), transition_id: value }, "/transition_id"]);
    for (const value of ["A".repeat(40), "a".repeat(39), SHA + "\n", 1]) cases.push([{ ...fenceBody(), expected_head: value }, "/expected_head"]);
    for (const [body, pointer] of cases) {
      const result = await post(base, "fence", body);
      expect(result.status).toBe(422);
      expect(result.body).toMatchObject({ error: { code: "lab_fence_invalid", pointer } });
    }
    expect(generationCall).not.toHaveBeenCalled();
    expect(fenceCall).not.toHaveBeenCalled();
  });

  it.each(["generation", "fence"] as const)("scrubs %s repository refusal and unexpected failures", async operation => {
    const { port } = fixture();
    const base = listen(port);
    const body = operation === "fence" ? fenceBody() : {};
    const call = vi.spyOn(port, operation);
    call.mockRejectedValueOnce(new ForgePublicationError("refused", "private mapping"));
    expect(await post(base, operation, body)).toEqual({ status: 403, body: { error: { code: "lab_repository_refused" } } });
    call.mockRejectedValueOnce(new Error("private path"));
    expect(await post(base, operation, body)).toEqual({ status: 500, body: { error: { code: `lab_${operation}_failed`, message: `Lab ${operation} failed` } } });
  });
});
