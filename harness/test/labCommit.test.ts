import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createHarness } from "../src/server.js";
import * as labCommit from "../src/lab/labCommit.js";
import type { LabCommitResponseBody } from "../src/lab/labCommit.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { FakeTenantRepoProvider } from "../src/ports/fakes/fakeTenantRepo.js";
import {
  LAB_DOCUMENT_REF, TenantDocumentRepo,
  type LabDocumentPort, type TenantDocumentRepoOptions,
} from "../src/vendor/mushy-author/ports/labDocumentPort.js";
import { ForgePublicationError, ForgeRemoteAuthority } from "../src/vendor/mushy-author/ports/impl/forgeRemoteAuthority.js";
import { HARNESS_IDENTITY } from "../src/vendor/mushy-author/registry/registerTool.js";

const SECRET = "lab-commit-test-harness-secret";
const TENANT = "lab-tenant";
const SHA = "a".repeat(40);
const validBody = () => ({
  expected_sha: null,
  documents: [{ path: "lab/graphs/one.json", content: '{"nodes":[]}' }],
  message: "Save Lab graph",
});

function okBody(result: { body: LabCommitResponseBody }) {
  expect(result.body).toHaveProperty("commit");
  if (!("commit" in result.body)) throw new Error("Expected a successful Lab commit response");
  return result.body;
}

function errorBody(result: { body: LabCommitResponseBody }) {
  expect(result.body).toHaveProperty("error");
  if (!("error" in result.body)) throw new Error("Expected a Lab commit error response");
  return result.body;
}

describe("Lab commit", () => {
  const servers: Server[] = [];
  const directories: string[] = [];
  afterEach(async () => {
    await Promise.all(servers.splice(0).map(server => new Promise<void>((resolve, reject) => {
      server.close(error => error ? reject(error) : resolve());
      server.closeAllConnections();
    })));
    for (const directory of directories.splice(0)) rmSync(directory, { recursive: true, force: true });
    vi.restoreAllMocks();
  });

  function fakePort(): LabDocumentPort {
    return {
      inspect: vi.fn<LabDocumentPort["inspect"]>(async () => ({ ref: LAB_DOCUMENT_REF, head: null, commits: [], documents: [] })),
      commit: vi.fn<LabDocumentPort["commit"]>(async () => ({ ref: LAB_DOCUMENT_REF, commit: SHA })),
      publishRef: vi.fn(async () => ({ commit: SHA })),
    };
  }

  function listen(labDocuments?: LabDocumentPort): string {
    const server = createHarness({
      oauth: new FakeOAuthGrantProvider(),
      tenantRepo: new FakeTenantRepoProvider(process.cwd()),
      broker: new FakeBrokerApsClient(),
      agentRunner: new FakeAgentRunner(),
    }, {
      auth: { enabled: true, secret: SECRET },
      ...(labDocuments ? { labDocuments } : {}),
    }).listen(0);
    servers.push(server);
    return `http://127.0.0.1:${(server.address() as AddressInfo).port}/lab/commit`;
  }

  async function post(url: string, body: unknown = validBody(), tenant: string | null = TENANT, secret: string | null = SECRET) {
    const response = await fetch(url, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        ...(tenant === null ? {} : { "x-tenant-id": tenant }),
        ...(secret === null ? {} : { "x-harness-secret": secret }),
      },
      body: JSON.stringify(body),
    });
    return { status: response.status, body: await response.json() as LabCommitResponseBody, cacheControl: response.headers.get("cache-control") };
  }

  function git(dir: string, ...args: string[]): string {
    return execFileSync("git", [
      "-c", "user.name=Lab Test", "-c", "user.email=lab@example.test",
      "-c", "commit.gpgSign=false", "-c", "core.hooksPath=/dev/null", ...args,
    ], { cwd: dir, encoding: "utf8", windowsHide: true }).trim();
  }

  function fixture() {
    const root = mkdtempSync(join(tmpdir(), "harness-lab-commit-"));
    directories.push(root);
    const seed = join(root, "seed");
    const remote = join(root, "remote.git");
    const cache = join(root, "cache.git");
    git(root, "init", "-b", "main", seed);
    writeFileSync(join(seed, "README.md"), "Seed main\n");
    git(seed, "add", "README.md");
    git(seed, "commit", "-m", "Seed");
    const main = git(seed, "rev-parse", "main");
    git(root, "clone", "--bare", seed, remote);
    git(root, "clone", "--bare", remote, cache);
    const authority = new ForgeRemoteAuthority({
      locate: async () => remote,
      credentials: async (tenantId, remoteUrl) => ({ tenantId, remoteUrl }),
      allowLocalPathsForTests: true,
    });
    let tail: Promise<void> = Promise.resolve();
    const lease: TenantDocumentRepoOptions["lease"] = {
      async withLease(tenantId, action) {
        const previous = tail;
        let release!: () => void;
        tail = new Promise<void>(resolve => { release = resolve; });
        await previous;
        try {
          return await action({ tenantId, ownerToken: "lab-test-owner", generation: "1", lost: false, repoDirs: new Set<string>() });
        } finally {
          release();
        }
      },
      async runFenced(_lease, action) { return action(); },
    };
    const port = new TenantDocumentRepo({
      locate: async () => ({ dir: cache, sourceRef: remote }),
      lease, authority, identity: HARNESS_IDENTITY,
    });
    return { port, remote, cache, main };
  }

  it("requires the harness secret before invoking the port", async () => {
    const port = fakePort();
    const url = listen(port);
    for (const secret of [null, "wrong-secret"]) {
      expect((await post(url, validBody(), TENANT, secret)).status).toBe(401);
    }
    expect(port.commit).not.toHaveBeenCalled();
    expect(port.publishRef).not.toHaveBeenCalled();
  });

  it("ships dark when the document seam is absent", async () => {
    const result = await post(listen());
    expect(result.status).toBe(501);
    expect(errorBody(result).error.code).toBe("lab_commit_unavailable");
  });

  it("requires a valid tenant from the server header", async () => {
    const port = fakePort();
    const url = listen(port);
    for (const tenant of [null, "../other", "x".repeat(201)]) {
      const result = await post(url, validBody(), tenant);
      expect(result.status).toBe(400);
      expect(errorBody(result).error.code).toBe("lab_tenant_required");
    }
    expect(port.commit).not.toHaveBeenCalled();
  });

  it("rejects invalid closed bodies with the offending JSON pointer", async () => {
    const port = fakePort();
    const url = listen(port);
    const cases: [unknown, string][] = [
      [{ ...validBody(), tenant_id: "other" }, "/tenant_id"],
      [{ ...validBody(), documents: [{ path: "elsewhere.json", content: "{}" }] }, "/documents/0/path"],
      [{ ...validBody(), documents: [{ path: "lab/graphs/one.json\n", content: "{}" }] }, "/documents/0/path"],
      [{ ...validBody(), documents: [{ path: "lab/graphs/one.json", content: "[]" }] }, "/documents/0/content"],
      [{ ...validBody(), documents: Array.from({ length: 65 }, (_, i) => ({ path: `lab/graphs/g${i}.json`, content: "{}" })) }, "/documents"],
      [{ ...validBody(), documents: [...validBody().documents, ...validBody().documents] }, "/documents/1/path"],
      [{ ...validBody(), message: " \n " }, "/message"],
      [{ ...validBody(), expected_sha: "0".repeat(40) }, "/expected_sha"],
      [{ ...validBody(), expected_sha: SHA + "\n" }, "/expected_sha"],
      [{ documents: validBody().documents, message: "Save" }, "/expected_sha"],
      [{ ...validBody(), publish: null }, "/publish"],
      [{ ...validBody(), publish: {} }, "/publish/expected_remote_sha"],
      [{ ...validBody(), publish: { expected_remote_sha: null, extra: true } }, "/publish/extra"],
      [{ ...validBody(), documents: [{ ...validBody().documents[0], extra: true }] }, "/documents/0/extra"],
      [{ ...validBody(), documents: [{ path: "lab/widgets/one.json", content: '"' + "é".repeat(512 * 1024) + '"' }] }, "/documents/0/content"],
    ];
    for (const [body, pointer] of cases) {
      const result = await post(url, body);
      expect(result.status).toBe(422);
      expect(errorBody(result).error).toMatchObject({ code: "lab_commit_invalid", pointer });
    }
    expect(port.commit).not.toHaveBeenCalled();
  });

  it("creates the first Lab commit in the cache without moving main", async () => {
    const { port, cache, main } = fixture();
    const result = await post(listen(port));
    expect(result.status).toBe(200);
    expect(result.cacheControl).toBe("no-store");
    expect(result.body).toMatchObject({ ref: LAB_DOCUMENT_REF, published: null });
    expect(okBody(result).commit).toMatch(/^[0-9a-f]{40}$/);
    expect(git(cache, "rev-parse", LAB_DOCUMENT_REF)).toBe(okBody(result).commit);
    expect(git(cache, "rev-parse", "main")).toBe(main);
    expect(git(cache, "rev-list", "--count", LAB_DOCUMENT_REF)).toBe("1");
    expect(git(cache, "show", `${okBody(result).commit}:lab/graphs/one.json`)).toBe(validBody().documents[0]!.content);
  });

  it("returns a conflict for a stale local ref", async () => {
    const { port } = fixture();
    const url = listen(port);
    const first = await post(url);
    expect(first.status).toBe(200);
    const stale = await post(url);
    expect(stale.status).toBe(409);
    expect(stale.body).toEqual({ error: { code: "lab_ref_conflict", expected_sha: null, observed_sha: okBody(first).commit } });
  });

  it("publishes and reads back the Lab ref from Forge", async () => {
    const { port, remote, main } = fixture();
    const result = await post(listen(port), { ...validBody(), publish: { expected_remote_sha: null } });
    expect(result.status).toBe(200);
    expect(okBody(result).published).toBe(okBody(result).commit);
    expect(git(remote, "rev-parse", LAB_DOCUMENT_REF)).toBe(okBody(result).commit);
    expect(git(remote, "rev-parse", "main")).toBe(main);
  });

  it("retains the local commit when the remote ref is stale", async () => {
    const { port, remote, cache } = fixture();
    const url = listen(port);
    const first = await post(url, { ...validBody(), publish: { expected_remote_sha: null } });
    expect(first.status).toBe(200);
    const stale = await post(url, {
      ...validBody(), expected_sha: okBody(first).commit, message: "Second Lab commit",
      publish: { expected_remote_sha: null },
    });
    expect(stale.status).toBe(409);
    expect(errorBody(stale).error).toEqual({ code: "lab_publish_conflict", expected_sha: null, observed_sha: okBody(first).commit });
    expect(errorBody(stale).committed).toMatch(/^[0-9a-f]{40}$/);
    expect(git(cache, "rev-parse", LAB_DOCUMENT_REF)).toBe(errorBody(stale).committed);
    expect(git(remote, "rev-parse", LAB_DOCUMENT_REF)).toBe(okBody(first).commit);
  });

  it("never reports unknown publication as success", async () => {
    const port = fakePort();
    port.publishRef = vi.fn(async () => { throw new ForgePublicationError("unknown", "private Forge details"); });
    const result = await post(listen(port), { ...validBody(), publish: { expected_remote_sha: null } });
    expect(result.status).toBe(502);
    expect(result.body).toEqual({ error: { code: "lab_publish_unknown" }, committed: SHA });
    expect(port.publishRef).toHaveBeenCalledExactlyOnceWith(TENANT, null);
  });

  it("returns a scrubbed failure for an unexpected port error", async () => {
    const port = fakePort();
    port.commit = vi.fn(async () => { throw new Error("private repository detail"); });
    const result = await post(listen(port));
    expect(result.status).toBe(500);
    expect(result.body).toEqual({ error: { code: "lab_commit_failed", message: "Lab commit failed" } });
    expect(JSON.stringify(result.body)).not.toContain("private repository detail");
  });

  it("calls the handler once per request and never retries a failed step", async () => {
    const handler = vi.spyOn(labCommit, "handleLabCommit");
    const port = fakePort();
    port.commit = vi.fn(async () => { throw new Error("no retry"); });
    expect((await post(listen(port))).status).toBe(500);
    expect(handler).toHaveBeenCalledTimes(1);
    expect(port.commit).toHaveBeenCalledTimes(1);
    expect(port.publishRef).not.toHaveBeenCalled();
  });
});
