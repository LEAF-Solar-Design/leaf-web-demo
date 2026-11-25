/** HTTP lifecycle boundary: caller auth, non-effective staging, and exact publish. */

import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantRepoProviderImpl } from "../src/ports/impl/tenantRepoProvider.js";
import { ForgeRemoteAuthority } from "../src/vendor/mushy-author/ports/impl/forgeRemoteAuthority.js";
import { createHarness } from "../src/server.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { FakeTenantRepoProvider } from "../src/ports/fakes/fakeTenantRepo.js";
import type { CustomizationCoordination, HarnessPorts, StagedCustomizationReceipt } from "../src/ports/index.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE = join(HERE, "fixtures", "tenant-repo");
const SECRET = "test-harness-secret";
const TENANT = "route-customization-tenant";
const CHANGE = "11111111-1111-4111-8111-111111111111";
const DIGEST = "a".repeat(64);
const SOURCE_COMMIT = "b".repeat(40);
const SOURCE_BYTES = Buffer.from([35, 32, 101, 120, 97, 99, 116, 13, 10, 0, 255]);

function sourceResult(bytes = SOURCE_BYTES) {
  return {
    bytes, commit: SOURCE_COMMIT,
    blob: createHash("sha1").update(`blob ${bytes.length}\0`).update(bytes).digest("hex"),
    sha256: createHash("sha256").update(bytes).digest("hex"),
  };
}

function sourceReader() {
  return vi.fn(async (_tenant: string, _request: { changeSetId: string; stagedCommit: string; slug: string }) => sourceResult());
}

function git(dir: string, args: string[]): string {
  return execFileSync("git", args, { cwd: dir, encoding: "utf8" }).trim();
}

class Coordination implements CustomizationCoordination {
  staged: StagedCustomizationReceipt | null = null;
  approved = false;
  authorizeCalls = 0;

  async recordStaged(receipt: StagedCustomizationReceipt): Promise<void> {
    this.staged = receipt;
  }

  async authorizePublish(receipt: StagedCustomizationReceipt, expectedMainSha: string): Promise<void> {
    this.authorizeCalls += 1;
    if (!this.approved || !this.staged || expectedMainSha !== receipt.base_commit ||
      JSON.stringify(this.staged) !== JSON.stringify(receipt)) {
      throw new Error("publish requires the approved exact staged receipt");
    }
  }
}

describe("customization routes", () => {
  let server: Server;
  let baseUrl: string;
  let bareDir: string;
  let coordination: Coordination;
  let ports: HarnessPorts;
  let readSource: ReturnType<typeof sourceReader>;

  beforeEach(async () => {
    const tenantRepo = new FakeTenantRepoProvider(FIXTURE);
    readSource = sourceReader();
    Object.assign(tenantRepo, { readStagedToolSource: readSource });
    coordination = new Coordination();
    ports = {
      oauth: new FakeOAuthGrantProvider(),
      tenantRepo,
      broker: new FakeBrokerApsClient(),
      agentRunner: new FakeAgentRunner(),
      customizationCoordination: coordination,
    };
    bareDir = (await tenantRepo.bare(TENANT)).dir;
    server = createHarness(ports, { auth: { enabled: true, secret: SECRET } }).listen(0);
    baseUrl = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  });

  afterEach(() => server.close());

  function stageBody() {
    return {
      tenant_id: TENANT,
      description: "count entities per layer",
      changeSetId: CHANGE,
      expectedBaseSha: git(bareDir, ["rev-parse", "refs/heads/main"]),
      platformRelease: "sha256:platform-release",
      workspaceContractDigest: DIGEST,
      idempotencyKey: `stage-${CHANGE}`,
    };
  }

  function sourceBody() {
    return { tenant_id: TENANT, changeSetId: CHANGE, stagedCommit: SOURCE_COMMIT, slug: "route-tool" };
  }

  function readRequest(body: unknown = sourceBody(), headers: Record<string, string> = {}) {
    return fetch(`${baseUrl}/internal/author/staged-source`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-harness-secret": SECRET, "x-tenant-id": TENANT, ...headers },
      body: typeof body === "string" ? body : JSON.stringify(body),
    });
  }

  it("exports the private change blob through the pinned reader for an explicitly local tenant", async () => {
    const tenant = "64444444-4444-4444-8444-444444444444";
    const root = mkdtempSync(join(tmpdir(), "c02-staged-"));
    try {
      const origin = join(root, "origin");
      const repos = join(root, "repos");
      const bare = join(repos, tenant + ".git");
      mkdirSync(origin);
      mkdirSync(repos);
      git(origin, ["init", "-b", "main"]);
      const commit = (message: string) => git(origin, ["-c", "user.name=fixture",
        "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-m", message]);
      commit("Initial fixture");
      const main = git(origin, ["rev-parse", "HEAD"]);
      git(root, ["clone", "--bare", origin, bare]);
      const entry = "tools/route-tool/tool.py";
      mkdirSync(dirname(join(origin, entry)), { recursive: true });
      const bytes = Buffer.from("# synthetic read-only fixture\n", "utf8");
      writeFileSync(join(origin, entry), bytes);
      git(origin, ["-c", "core.autocrlf=false", "add", entry]);
      commit("Staged fixture");
      const staged = git(origin, ["rev-parse", "HEAD"]);
      const ref = "refs/leaf/changes/" + CHANGE;
      git(origin, ["push", bare, "HEAD:" + ref]);
      const refs = () => git(bare, ["for-each-ref", "--format=%(refname):%(objectname)"]);
      const before = refs();
      const noRemote = async (): Promise<never> => { throw new Error("unexpected remote access"); };
      ports.tenantRepo = new TenantRepoProviderImpl({
        bareBase: repos, authoringMode: "disabled", lease: false,
        locator: { repoRef: async () => origin },
        remoteAuthority: new ForgeRemoteAuthority({ locate: noRemote, credentials: noRemote }),
        isRemoteTenant: id => id !== tenant,
      });
      const response = await readRequest({ tenant_id: tenant, changeSetId: CHANGE,
        stagedCommit: staged, slug: "route-tool" }, { "x-tenant-id": tenant });
      expect(response.status).toBe(200);
      expect(Buffer.from(await response.arrayBuffer())).toEqual(bytes);
      expect(response.headers.get("x-leaf-source-commit")).toBe(staged);
      expect(response.headers.get("x-leaf-source-blob")).toBe(git(origin, ["rev-parse", staged + ":" + entry]));
      expect(response.headers.get("x-leaf-source-sha256")).toBe(createHash("sha256").update(bytes).digest("hex"));
      expect(git(bare, ["rev-parse", "refs/heads/main"])).toBe(main);
      expect(refs()).toBe(before);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("returns exact bounded source and both digest bindings through the normal provider", async () => {
    const main = git(bareDir, ["rev-parse", "refs/heads/main"]);
    const refs = git(bareDir, ["for-each-ref", "--format=%(refname):%(objectname)"]);
    const response = await readRequest();
    expect(response.status).toBe(200);
    expect(Buffer.from(await response.arrayBuffer())).toEqual(SOURCE_BYTES);
    expect(response.headers.get("content-type")).toBe("application/octet-stream");
    expect(response.headers.get("content-length")).toBe(String(SOURCE_BYTES.length));
    expect(response.headers.get("x-leaf-source-commit")).toBe(SOURCE_COMMIT);
    expect(response.headers.get("x-leaf-source-blob")).toBe(sourceResult().blob);
    expect(response.headers.get("x-leaf-source-sha256")).toBe(sourceResult().sha256);
    expect(response.headers.get("x-leaf-tool-slug")).toBe("route-tool");
    expect(response.headers.get("cache-control")).toContain("no-store");
    expect(response.headers.get("x-content-type-options")).toBe("nosniff");
    expect(readSource).toHaveBeenCalledExactlyOnceWith(TENANT, {
      changeSetId: CHANGE, stagedCommit: SOURCE_COMMIT, slug: "route-tool",
    });
    expect(git(bareDir, ["rev-parse", "refs/heads/main"])).toBe(main);
    expect(git(bareDir, ["for-each-ref", "--format=%(refname):%(objectname)"])).toBe(refs);
  });

  it.each(["", "wrong"])("requires the existing secret before source reads (%s)", async (secret) => {
    const response = await readRequest(sourceBody(), { "x-harness-secret": secret });
    expect(response.status).toBe(401);
    expect(response.headers.get("cache-control")).toContain("no-store");
    expect(readSource).not.toHaveBeenCalled();
  });

  it.each(["", "other-tenant"])("requires an exactly matching tenant header (%s)", async (tenant) => {
    const response = await readRequest(sourceBody(), { "x-tenant-id": tenant });
    expect(response.status).toBe(400);
    expect(readSource).not.toHaveBeenCalled();
  });

  it("requires a secret even when the ordinary harness gate is disabled", async () => {
    const privateServer = createHarness(ports, { auth: { enabled: false, secret: SECRET } }).listen(0);
    try {
      const url = `http://127.0.0.1:${(privateServer.address() as AddressInfo).port}/internal/author/staged-source`;
      const response = await fetch(url, {
        method: "POST", headers: { "content-type": "application/json", "x-tenant-id": TENANT },
        body: JSON.stringify(sourceBody()),
      });
      expect(response.status).toBe(401);
      expect(readSource).not.toHaveBeenCalled();
    } finally {
      privateServer.close();
    }
  });

  it("refuses when the existing secret is unconfigured", async () => {
    const privateServer = createHarness(ports, { auth: { enabled: false, secret: "" } }).listen(0);
    try {
      const response = await fetch(`http://127.0.0.1:${(privateServer.address() as AddressInfo).port}/internal/author/staged-source`, {
        method: "POST", headers: { "content-type": "application/json", "x-harness-secret": SECRET, "x-tenant-id": TENANT },
        body: JSON.stringify(sourceBody()),
      });
      expect(response.status).toBe(401);
      expect(readSource).not.toHaveBeenCalled();
    } finally {
      privateServer.close();
    }
  });

  it.each([
    { ref: "main" }, { repo: "/some/repo" }, { path: "/etc/passwd" },
    { slug: "../escape" }, { slug: "a/b" }, { slug: "a\\b" }, { slug: "a".repeat(65) },
    { slug: "route-tool\n" }, { changeSetId: `${CHANGE}\n` }, { stagedCommit: `${SOURCE_COMMIT}\n` },
    { changeSetId: "../escape" }, { stagedCommit: "main" }, { tenant_id: "../escape" },
  ])("refuses caller paths, refs, repositories and invalid fields: %j", async (fields) => {
    const response = await readRequest({ ...sourceBody(), ...fields });
    expect(response.status).toBe(400);
    expect(readSource).not.toHaveBeenCalled();
  });

  it("refuses missing fields, duplicate JSON keys, query parameters, and oversized bodies", async () => {
    for (const key of Object.keys(sourceBody())) {
      const body: Record<string, string> = { ...sourceBody() };
      delete body[key];
      expect((await readRequest(body)).status).toBe(400);
    }
    const duplicate = JSON.stringify(sourceBody()).replace("{", '{"slug":"other",');
    expect((await readRequest(duplicate)).status).toBe(400);
    expect((await readRequest({ ...sourceBody(), extra: "x".repeat(1100) })).status).toBe(413);
    const query = await fetch(`${baseUrl}/internal/author/staged-source?ref=main`, {
      method: "POST", headers: { "content-type": "application/json", "x-harness-secret": SECRET, "x-tenant-id": TENANT },
      body: JSON.stringify(sourceBody()),
    });
    expect(query.status).toBe(400);
    expect(readSource).not.toHaveBeenCalled();
  });

  it.each(["commit", "blob", "sha256", "oversize"])("refuses invalid provider %s", async (field) => {
    const source = sourceResult();
    if (field === "commit") source.commit = "c".repeat(40);
    if (field === "blob") source.blob = "0".repeat(40);
    if (field === "sha256") source.sha256 = "0".repeat(64);
    if (field === "oversize") Object.assign(source, sourceResult(Buffer.alloc(256 * 1024 + 1)));
    readSource.mockResolvedValue(source);
    const response = await readRequest();
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: { code: "stage_source_unavailable" } });
    expect(response.headers.get("cache-control")).toContain("no-store");
    expect(response.headers.has("x-leaf-source-blob")).toBe(false);
  });

  it("allows the exact byte cap", async () => {
    const source = sourceResult(Buffer.alloc(256 * 1024, 120));
    readSource.mockResolvedValue(source);
    const response = await readRequest();
    expect(response.status).toBe(200);
    expect(Buffer.from(await response.arrayBuffer())).toEqual(source.bytes);
  });

  it("refuses absent provider capability without a checkout or bare fallback", async () => {
    Reflect.deleteProperty(ports.tenantRepo, "readStagedToolSource");
    const checkout = vi.spyOn(ports.tenantRepo, "checkout");
    const bare = vi.spyOn(ports.tenantRepo, "bare");
    const response = await readRequest();
    expect(response.status).toBe(503);
    expect(checkout).not.toHaveBeenCalled();
    expect(bare).not.toHaveBeenCalled();
  });

  it.each(["symlink", "missing change", "wrong tenant", "wrong commit", "size"])(
    "sanitizes provider refusal for %s", async (reason) => {
      readSource.mockRejectedValue(new Error(`${reason}: internal /private/path secret=do-not-release`));
      const response = await readRequest();
      expect(response.status).toBe(503);
      expect(await response.json()).toEqual({ error: { code: "stage_source_unavailable" } });
    },
  );

  it("requires harness caller auth before staging", async () => {
    const main = git(bareDir, ["rev-parse", "refs/heads/main"]);
    const response = await fetch(`${baseUrl}/author/stage`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(stageBody()),
    });
    expect(response.status).toBe(401);
    expect(git(bareDir, ["rev-parse", "refs/heads/main"])).toBe(main);
  });

  it("provisions and reports the canonical repository before staging", async () => {
    const response = await fetch(`${baseUrl}/author/repository`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-harness-secret": SECRET },
      body: JSON.stringify({ tenant_id: TENANT }),
    });
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      tenant_id: TENANT,
      base_commit: git(bareDir, ["rev-parse", "refs/heads/main"]),
    });
  });

  it("requires harness caller auth before provisioning a repository", async () => {
    const response = await fetch(`${baseUrl}/author/repository`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ tenant_id: TENANT }),
    });
    expect(response.status).toBe(401);
  });

  it("stops staging before any repository or coordination mutation when authored execution is off", async () => {
    const main = git(bareDir, ["rev-parse", "refs/heads/main"]);
    const previous = process.env.LEAF_AUTHORED_EXECUTION;
    process.env.LEAF_AUTHORED_EXECUTION = "0";
    try {
      const response = await fetch(`${baseUrl}/author/stage`, {
        method: "POST",
        headers: { "content-type": "application/json", "x-harness-secret": SECRET },
        body: JSON.stringify(stageBody()),
      });
      expect(response.status).toBe(403);
      expect(coordination.staged).toBeNull();
      expect(git(bareDir, ["rev-parse", "refs/heads/main"])).toBe(main);
      expect(git(bareDir, ["for-each-ref", "--format=%(refname)", "refs/leaf/changes"]))
        .toBe("");
    } finally {
      if (previous === undefined) delete process.env.LEAF_AUTHORED_EXECUTION;
      else process.env.LEAF_AUTHORED_EXECUTION = previous;
    }
  });

  it("stages through AuthorLoop without moving main", async () => {
    const main = git(bareDir, ["rev-parse", "refs/heads/main"]);
    const response = await fetch(`${baseUrl}/author/stage`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-harness-secret": SECRET },
      body: JSON.stringify(stageBody()),
    });
    expect(response.status).toBe(200);
    const body = await response.json() as { receipt: StagedCustomizationReceipt };
    expect(body.receipt.base_commit).toBe(main);
    expect(git(bareDir, ["rev-parse", "refs/heads/main"])).toBe(main);
    expect(git(bareDir, ["rev-parse", `refs/leaf/changes/${CHANGE}`])).toBe(body.receipt.staged_commit);
  });

  it("requires a complete app authority tuple when standard services are enabled", async () => {
    const previous = process.env.LEAF_TENANT_MCP_BROKER_URL;
    process.env.LEAF_TENANT_MCP_BROKER_URL = "https://staging-api.leafdesign.ai";
    try {
      const missing = await fetch(`${baseUrl}/author/stage`, {
        method: "POST",
        headers: { "content-type": "application/json", "x-harness-secret": SECRET },
        body: JSON.stringify(stageBody()),
      });
      expect(missing.status).toBe(409);

      const partial = await fetch(`${baseUrl}/author/stage`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-harness-secret": SECRET,
          "x-authority-session-id": "session-a",
        },
        body: JSON.stringify(stageBody()),
      });
      expect(partial.status).toBe(409);

      const complete = await fetch(`${baseUrl}/author/stage`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-harness-secret": SECRET,
          "x-authority-session-id": "session-a",
          "x-authority-turn-id": "turn-a",
        },
        body: JSON.stringify(stageBody()),
      });
      expect(complete.status).toBe(200);
    } finally {
      if (previous === undefined) delete process.env.LEAF_TENANT_MCP_BROKER_URL;
      else process.env.LEAF_TENANT_MCP_BROKER_URL = previous;
    }
  });

  it("publishes only the exact approved receipt", async () => {
    const stagedResponse = await fetch(`${baseUrl}/author/stage`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-harness-secret": SECRET },
      body: JSON.stringify(stageBody()),
    });
    const staged = await stagedResponse.json() as { receipt: StagedCustomizationReceipt };
    const main = staged.receipt.base_commit;
    coordination.approved = true;

    const altered = { ...staged.receipt, catalog_digest: "b".repeat(64) };
    const rejected = await fetch(`${baseUrl}/author/publish`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-harness-secret": SECRET },
      body: JSON.stringify({ receipt: altered, expectedMainSha: main }),
    });
    expect(rejected.status).toBe(500);
    expect(git(bareDir, ["rev-parse", "refs/heads/main"])).toBe(main);

    const published = await fetch(`${baseUrl}/author/publish`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-harness-secret": SECRET },
      body: JSON.stringify({ receipt: staged.receipt, expectedMainSha: main }),
    });
    expect(published.status).toBe(200);
    expect(git(bareDir, ["rev-parse", "refs/heads/main"])).toBe(staged.receipt.staged_commit);
  });

  it("stops publish before authorization or main mutation when authored execution is off", async () => {
    const stagedResponse = await fetch(`${baseUrl}/author/stage`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-harness-secret": SECRET },
      body: JSON.stringify(stageBody()),
    });
    const staged = await stagedResponse.json() as { receipt: StagedCustomizationReceipt };
    const main = staged.receipt.base_commit;
    coordination.approved = true;
    const previous = process.env.LEAF_AUTHORED_EXECUTION;
    process.env.LEAF_AUTHORED_EXECUTION = "0";
    try {
      const response = await fetch(`${baseUrl}/author/publish`, {
        method: "POST",
        headers: { "content-type": "application/json", "x-harness-secret": SECRET },
        body: JSON.stringify({ receipt: staged.receipt, expectedMainSha: main }),
      });
      expect(response.status).toBe(403);
      expect(coordination.authorizeCalls).toBe(0);
      expect(git(bareDir, ["rev-parse", "refs/heads/main"])).toBe(main);
      expect(git(bareDir, ["rev-parse", `refs/leaf/changes/${CHANGE}`]))
        .toBe(staged.receipt.staged_commit);
    } finally {
      if (previous === undefined) delete process.env.LEAF_AUTHORED_EXECUTION;
      else process.env.LEAF_AUTHORED_EXECUTION = previous;
    }
  });
});
