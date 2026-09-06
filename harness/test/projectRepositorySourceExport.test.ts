import { execFileSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Pool } from "pg";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { createHarness } from "../src/server.js";
import { PROJECT_REPOSITORY_SOURCE_BUNDLE_CONTRACT as CONTRACT } from "../src/ports/index.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { PgTenantRepoLeaseCoordinator, TenantRepoProviderImpl } from "../src/ports/impl/tenantRepoProvider.js";

const url = process.env.PG_REPO_LEASE_TEST_URL ?? process.env.PG_SESSION_STORE_TEST_URL;
const hash = (text: string) => createHash("sha256").update(text, "utf8").digest("hex");
const canonical = (value: Record<string, string>) => JSON.stringify(value, Object.keys(value).sort());
const seed = (seedDocument = "Recettes 🍲\nPréserver exactement.\n") => ({ seedDocument, seedDigest: hash(seedDocument) });

describe.skipIf(!url)("real Git project source bundle export", () => {
  const pool = new Pool({ connectionString: url, max: 5 });
  const tableName = `export_lease_${randomUUID().replaceAll("-", "")}`;
  const coordinator = new PgTenantRepoLeaseCoordinator({ pool, tableName, ttlMs: 30000 });
  let root: string;
  let authority: { tenantId: string; organizationId: string; projectId: string; repoKey: string };
  let provider: TenantRepoProviderImpl;
  let close: (() => Promise<void>) | undefined;
  const makeProvider = () => new TenantRepoProviderImpl({
    locator: { async repoRef() { throw new Error("legacy locator must not run"); } },
    bareBase: root, lease: coordinator, authoringMode: "singleton",
  });
  const bare = () => join(root, `${authority.repoKey}.git`);
  const git = (args: string[], input?: string) => execFileSync("git", ["--git-dir", bare(), ...args], {
    encoding: "utf8", input, stdio: [input === undefined ? "ignore" : "pipe", "pipe", "ignore"],
  });

  beforeAll(async () => {
    await pool.query(`CREATE TABLE "${tableName}" (
      tenant_id TEXT PRIMARY KEY, owner_token UUID NOT NULL, generation BIGINT NOT NULL CHECK (generation > 0),
      acquired_at TIMESTAMPTZ NOT NULL, heartbeat_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ NOT NULL)`);
  });
  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), "leaf-source-export-"));
    const tenantId = randomUUID();
    authority = { tenantId, organizationId: tenantId, projectId: randomUUID(), repoKey: randomUUID() };
    provider = makeProvider();
  });
  afterEach(async () => {
    if (close) await close();
    close = undefined;
    rmSync(root, { recursive: true, force: true });
  });
  afterAll(async () => { await pool.query(`DROP TABLE "${tableName}"`); await pool.end(); });


  it("exports through the configured PostgreSQL project lease and recovers an expired lease", async () => {
    const initialized = await provider.initializeProjectSource(authority, seed());
    const request = { sourceCommit: initialized.sourceCommit, sourceTree: initialized.sourceTree };
    const key = ["leaf-project-repository-v1", authority.tenantId, authority.organizationId,
      authority.projectId, authority.repoKey].join(":");
    await pool.query(`UPDATE "${tableName}" SET owner_token=$2, generation=7,
      expires_at=clock_timestamp()-interval '1 second' WHERE tenant_id=$1`, [key, randomUUID()]);
    const result = await provider.exportProjectSourceBundle(authority, request);
    expect(result.leaseGeneration).toBe("8");
    const rows = await pool.query(`SELECT owner_token::text, generation::text FROM "${tableName}" WHERE tenant_id=$1`, [key]);
    expect(rows.rows[0]).toEqual({ owner_token: result.leaseId, generation: result.leaseGeneration });
    expect(result.bundleSha256).toBe(createHash("sha256").update(result.bundle).digest("hex"));
    expect(result.sizeBytes).toBe(result.bundle.length);
    const bundlePath = join(root, "export.bundle");
    writeFileSync(bundlePath, result.bundle);
    const clone = join(root, "clone");
    execFileSync("git", ["clone", "--no-checkout", bundlePath, clone], { stdio: "ignore" });
    const localGit = (...args: string[]) => execFileSync("git", ["-C", clone, ...args], { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
    localGit("remote", "remove", "origin");
    localGit("checkout", "--detach", request.sourceCommit);
    expect(localGit("rev-parse", "HEAD")).toBe(request.sourceCommit);
    expect(localGit("rev-parse", "HEAD^{tree}")).toBe(request.sourceTree);
    expect(localGit("remote")).toBe("");
    expect(() => localGit("symbolic-ref", "HEAD")).toThrow();
    expect(readFileSync(join(clone, "PROMPT.md"), "utf8")).toBe(seed().seedDocument);
    expect(git(["rev-parse", "main"]).trim()).toBe(request.sourceCommit);
    const repeat = await provider.exportProjectSourceBundle(authority, request);
    expect(repeat.sourceCommit).toBe(result.sourceCommit);
    expect(repeat.sourceTree).toBe(result.sourceTree);
    if (process.env.LEAF_EXPORT_EVIDENCE_DIR) {
      mkdirSync(process.env.LEAF_EXPORT_EVIDENCE_DIR, { recursive: true });
      writeFileSync(join(process.env.LEAF_EXPORT_EVIDENCE_DIR, "export.bundle"), result.bundle);
      writeFileSync(join(process.env.LEAF_EXPORT_EVIDENCE_DIR, "export.json"), JSON.stringify({
        source_commit: result.sourceCommit, source_tree: result.sourceTree,
        bundle_sha256: result.bundleSha256, size_bytes: result.sizeBytes, repository: authority.repoKey,
      }));
    }
  });

  it("rejects invalid source and tree and foreign full authority without modifying main", async () => {
    const initialized = await provider.initializeProjectSource(authority, seed());
    const request = { sourceCommit: initialized.sourceCommit, sourceTree: initialized.sourceTree };
    for (const sourceCommit of ["a".repeat(39), "A".repeat(40), "f".repeat(40)]) {
      await expect(provider.exportProjectSourceBundle(authority, { ...request, sourceCommit })).rejects.toThrow();
    }
    for (const sourceTree of ["bad", "f".repeat(40)]) {
      await expect(provider.exportProjectSourceBundle(authority, { ...request, sourceTree })).rejects.toThrow();
    }
    for (const field of ["tenantId", "organizationId", "projectId", "repoKey"] as const) {
      await expect(provider.exportProjectSourceBundle({ ...authority, [field]: randomUUID() }, request)).rejects.toThrow();
    }
    expect(git(["rev-parse", "main"]).trim()).toBe(request.sourceCommit);
    const descendant = git(["-c", "user.name=test", "-c", "user.email=test@leaf.invalid",
      "commit-tree", request.sourceTree, "-p", request.sourceCommit], "Later source\n").trim();
    git(["update-ref", "refs/heads/main", descendant, request.sourceCommit]);
    await expect(provider.exportProjectSourceBundle(authority, request)).rejects.toThrow();
  });

  it("rejects unsafe entries configuration alternates and unowned repositories", async () => {
    const initialized = await provider.initializeProjectSource(authority, seed());
    const request = { sourceCommit: initialized.sourceCommit, sourceTree: initialized.sourceTree };
    const configPath = join(bare(), "config");
    const config = readFileSync(configPath, "utf8");
    writeFileSync(configPath, config + "\n[include]\npath = /private/foreign\n");
    await expect(provider.exportProjectSourceBundle(authority, request)).rejects.toThrow();
    writeFileSync(configPath, config);
    for (const name of ["alternates", "http-alternates"]) {
      const path = join(bare(), "objects", "info", name);
      writeFileSync(path, "/private/foreign");
      await expect(provider.exportProjectSourceBundle(authority, request)).rejects.toThrow();
      rmSync(path);
    }
    const link = join(bare(), "unsafe-entry");
    symlinkSync(join(bare(), "objects"), link, "junction");
    await expect(provider.exportProjectSourceBundle(authority, request)).rejects.toThrow();
    rmSync(link);
    rmSync(join(bare(), ".leaf-source-owner.json"));
    await expect(provider.exportProjectSourceBundle(authority, request)).rejects.toThrow();
    expect(git(["rev-parse", "main"]).trim()).toBe(request.sourceCommit);
  });

  it("forces auth with auth off and returns exact binary headers for repeat requests", async () => {
    const initialized = await provider.initializeProjectSource(authority, seed());
    const server = createHarness({ oauth: new FakeOAuthGrantProvider(), tenantRepo: provider,
      broker: new FakeBrokerApsClient(), agentRunner: new FakeAgentRunner() },
      { auth: { enabled: false, secret: "source-test-secret" } }).listen(0);
    close = () => new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
    const endpoint = `http://127.0.0.1:${(server.address() as AddressInfo).port}/internal/project-repository-source/export`;
    const body = { tenant_id: authority.tenantId, organization_id: authority.organizationId,
      project_id: authority.projectId, repo_key: authority.repoKey,
      source_commit: initialized.sourceCommit, source_tree: initialized.sourceTree };
    const post = (raw = JSON.stringify(body), secret = "source-test-secret", tenant = authority.tenantId) => fetch(endpoint, {
      method: "POST", headers: { "content-type": "application/json", "x-harness-secret": secret, "x-tenant-id": tenant },
      body: raw,
    });
    expect((await post(undefined, "")).status).toBe(401);
    expect((await post(undefined, "wrong")).status).toBe(401);
    expect((await post(undefined, "source-test-secret", randomUUID())).status).toBe(400);
    expect((await post(JSON.stringify({ ...body, path: "forbidden" }))).status).toBe(400);
    expect((await post(JSON.stringify({ ...body, source_commit: "bad" }))).status).toBe(400);
    expect((await post(JSON.stringify(body).replace("{", '{"repo_key":"' + authority.repoKey + '",'))).status).toBe(400);
    expect((await post(JSON.stringify(body).replace("{", '{"repo_\\u006bey":"' + authority.repoKey + '",'))).status).toBe(400);
    expect((await post(" ".repeat(256 * 1024) + JSON.stringify(body))).status).toBe(400);
    expect((await post(JSON.stringify({ ...body, source_tree: "f".repeat(40) }))).status).toBe(409);
    for (let i = 0; i < 2; i++) {
      const response = await post();
      expect(response.status).toBe(200);
      const bytes = Buffer.from(await response.arrayBuffer());
      expect(response.headers.get("content-type")).toBe("application/octet-stream");
      expect(response.headers.get("content-length")).toBe(String(bytes.length));
      expect(response.headers.get("x-leaf-source-contract")).toBe(CONTRACT);
      expect(response.headers.get("x-leaf-request-digest")).toBe(hash(canonical({ ...body, contract: CONTRACT })));
      expect(response.headers.get("x-leaf-source-commit")).toBe(initialized.sourceCommit);
      expect(response.headers.get("x-leaf-source-tree")).toBe(initialized.sourceTree);
      expect(response.headers.get("x-leaf-bundle-sha256")).toBe(createHash("sha256").update(bytes).digest("hex"));
      const key = ["leaf-project-repository-v1", authority.tenantId, authority.organizationId, authority.projectId, authority.repoKey].join(":");
      const rows = await pool.query(`SELECT owner_token::text, generation::text FROM "${tableName}" WHERE tenant_id=$1`, [key]);
      expect(response.headers.get("x-leaf-lease-id")).toBe(rows.rows[0].owner_token);
      expect(response.headers.get("x-leaf-lease-generation")).toBe(rows.rows[0].generation);
    }
  });
});
