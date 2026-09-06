import { execFileSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { mkdtempSync, mkdirSync, readFileSync, readdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Pool } from "pg";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { createHarness } from "../src/server.js";
import { PROJECT_REPOSITORY_SOURCE_INITIALIZER_CONTRACT as CONTRACT } from "../src/ports/index.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { PgTenantRepoLeaseCoordinator, TenantRepoProviderImpl } from "../src/ports/impl/tenantRepoProvider.js";

const url = process.env.PG_REPO_LEASE_TEST_URL ?? process.env.PG_SESSION_STORE_TEST_URL;
const hash = (text: string) => createHash("sha256").update(text, "utf8").digest("hex");
const canonical = (value: Record<string, string>) => JSON.stringify(value, Object.keys(value).sort());
const seed = (seedDocument = "Recettes 🍲\nPréserver exactement.\n") => ({ seedDocument, seedDigest: hash(seedDocument) });

describe.skipIf(!url)("real Git project source with PostgreSQL fencing", () => {
  const pool = new Pool({ connectionString: url, max: 5 });
  const tableName = `source_lease_${randomUUID().replaceAll("-", "")}`;
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
  const identity = () => ({ contract: CONTRACT, tenant_id: authority.tenantId,
    organization_id: authority.organizationId, project_id: authority.projectId, repo_key: authority.repoKey });
  const git = (args: string[], input?: string) => execFileSync("git", ["--git-dir", bare(), ...args], {
    encoding: "utf8", input, stdio: [input === undefined ? "ignore" : "pipe", "pipe", "ignore"],
  });

  beforeAll(async () => {
    await pool.query(`CREATE TABLE "${tableName}" (
      tenant_id TEXT PRIMARY KEY, owner_token UUID NOT NULL, generation BIGINT NOT NULL CHECK (generation > 0),
      acquired_at TIMESTAMPTZ NOT NULL, heartbeat_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ NOT NULL)`);
  });
  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), "leaf-source-initialize-"));
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

  it("creates exact prompt bytes and real commit/tree, then replays after restart and a different prompt", async () => {
    const first = await provider.initializeProjectSource(authority, seed());
    expect(first.replayed).toBe(false);
    expect(git(["rev-parse", "main"]).trim()).toBe(first.sourceCommit);
    expect(git(["rev-parse", "main^{tree}"]).trim()).toBe(first.sourceTree);
    expect(git(["show", "main:PROMPT.md"])).toBe(seed().seedDocument);
    expect(git(["show", "main:.leaf/source-seed.json"])).toBe(canonical({ ...identity(), seed_digest: seed().seedDigest }));
    const replay = await makeProvider().initializeProjectSource(authority, seed());
    const later = await makeProvider().initializeProjectSource(authority, seed("A later campaign"));
    for (const result of [replay, later]) {
      expect(result.replayed).toBe(true);
      expect(result.sourceCommit).toBe(first.sourceCommit);
      expect(result.sourceTree).toBe(first.sourceTree);
      expect(result.seedDigest).toBe(first.seedDigest);
    }
    const leaseKey = ["leaf-project-repository-v1", authority.tenantId, authority.organizationId,
      authority.projectId, authority.repoKey].join(":");
    const rows = await pool.query(`SELECT owner_token::text, generation::text FROM "${tableName}" WHERE tenant_id=$1`, [leaseKey]);
    expect(rows.rows[0]).toEqual({ owner_token: later.writerLeaseId, generation: later.writerLeaseGeneration });
    expect(BigInt(later.writerLeaseGeneration)).toBeGreaterThan(BigInt(first.writerLeaseGeneration));
    const descendant = git(["-c", "user.name=test", "-c", "user.email=test@leaf.invalid",
      "commit-tree", first.sourceTree, "-p", first.sourceCommit], "Later legitimate source commit\n").trim();
    git(["update-ref", "refs/heads/main", descendant, first.sourceCommit]);
    const current = await makeProvider().initializeProjectSource(authority, seed("Another campaign"));
    expect(current.sourceCommit).toBe(descendant);
    expect(current.seedDigest).toBe(first.seedDigest);
  });

  it("recovers a marker-only and initialized partial directory through an expired PostgreSQL lease", async () => {
    mkdirSync(bare());
    writeFileSync(join(bare(), ".leaf-source-owner.json"), canonical(identity()));
    const key = ["leaf-project-repository-v1", authority.tenantId, authority.organizationId,
      authority.projectId, authority.repoKey].join(":");
    await pool.query(`INSERT INTO "${tableName}" VALUES ($1,$2,7,clock_timestamp(),clock_timestamp(),clock_timestamp()-interval '1 second')`,
      [key, randomUUID()]);
    const result = await makeProvider().initializeProjectSource(authority, seed());
    expect(result.writerLeaseGeneration).toBe("8");
    expect(git(["show", "main:PROMPT.md"])).toBe(seed().seedDocument);
    // A process that stopped after plumbing wrote objects but before publication.
    git(["update-ref", "-d", "refs/heads/main", result.sourceCommit]);
    const recovered = await makeProvider().initializeProjectSource(authority, seed());
    expect(recovered.sourceCommit).toBe(result.sourceCommit);
    expect(recovered.sourceTree).toBe(result.sourceTree);
  });

  it("refuses wrong full authority and malformed seed without changing an existing ref", async () => {
    const result = await provider.initializeProjectSource(authority, seed());
    for (const field of ["tenantId", "organizationId", "projectId"] as const) {
      await expect(provider.initializeProjectSource({ ...authority, [field]: randomUUID() }, seed())).rejects.toThrow();
      expect(git(["rev-parse", "main"]).trim()).toBe(result.sourceCommit);
    }
    const bad = git(["hash-object", "-w", "--stdin"], "{}").trim();
    const leaf = git(["mktree"], `100644 blob ${bad}\tsource-seed.json\n`).trim();
    const prompt = git(["rev-parse", "main:PROMPT.md"]).trim();
    const tree = git(["mktree"], `040000 tree ${leaf}\t.leaf\n100644 blob ${prompt}\tPROMPT.md\n`).trim();
    const commit = git(["-c", "user.name=test", "-c", "user.email=test@leaf.invalid", "commit-tree", tree, "-p", result.sourceCommit], "bad metadata\n").trim();
    git(["update-ref", "refs/heads/main", commit, result.sourceCommit]);
    await expect(provider.initializeProjectSource(authority, seed())).rejects.toThrow();
    expect(git(["rev-parse", "main"]).trim()).toBe(commit);
  });

  it("preserves unowned contents and rejects candidate and base symlinks", async () => {
    mkdirSync(bare());
    writeFileSync(join(bare(), "foreign"), "keep");
    await expect(provider.initializeProjectSource(authority, seed())).rejects.toThrow();
    expect(readdirSync(bare())).toEqual(["foreign"]);
    expect(readFileSync(join(bare(), "foreign"), "utf8")).toBe("keep");
    const old = bare();
    authority = { ...authority, repoKey: randomUUID() };
    symlinkSync(old, bare(), "junction");
    await expect(provider.initializeProjectSource(authority, seed())).rejects.toThrow();
    const alias = join(root, "alias");
    symlinkSync(old, alias, "junction");
    const escaped = new TenantRepoProviderImpl({ locator: { async repoRef() { return "unused"; } },
      bareBase: alias, lease: coordinator, authoringMode: "singleton" });
    await expect(escaped.initializeProjectSource(authority, seed())).rejects.toThrow();
    expect(readFileSync(join(old, "foreign"), "utf8")).toBe("keep");
  });

  it("requires the private secret with local auth off and bounds Unicode and closed authority", async () => {
    const server = createHarness({ oauth: new FakeOAuthGrantProvider(), tenantRepo: provider,
      broker: new FakeBrokerApsClient(), agentRunner: new FakeAgentRunner() },
      { auth: { enabled: false, secret: "source-test-secret" } }).listen(0);
    close = () => new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
    const endpoint = `http://127.0.0.1:${(server.address() as AddressInfo).port}/internal/project-repository-source/initialize`;
    const request = seed("🍲".repeat(32768));
    const body = { tenant_id: authority.tenantId, organization_id: authority.organizationId,
      project_id: authority.projectId, repo_key: authority.repoKey,
      seed_document: request.seedDocument, seed_digest: request.seedDigest };
    const post = (value: unknown, secret = "source-test-secret", tenant = authority.tenantId) => fetch(endpoint, {
      method: "POST", headers: { "content-type": "application/json", "x-harness-secret": secret, "x-tenant-id": tenant },
      body: JSON.stringify(value),
    });
    expect((await post(body, "")).status).toBe(401);
    expect((await post(body, "wrong")).status).toBe(401);
    expect((await post(body, "source-test-secret", randomUUID())).status).toBe(400);
    expect((await post({ ...body, repo_path: "forbidden" })).status).toBe(400);
    const oversized = "🍲".repeat(32769);
    expect((await post({ ...body, seed_document: oversized, seed_digest: hash(oversized) })).status).toBe(400);
    expect((await post({ ...body, padding: "x".repeat(256 * 1024) })).status).toBe(400);
    const response = await post(body);
    expect(response.status).toBe(200);
    const result = await response.json() as Record<string, unknown>;
    expect(Object.keys(result).sort()).toEqual(["contract", "replayed", "request_digest", "seed_digest",
      "source_commit", "source_tree", "writer_lease_generation", "writer_lease_id"]);
    expect(result.request_digest).toBe(hash(canonical({ ...identity(), seed_digest: request.seedDigest })));
    expect(git(["show", "main:PROMPT.md"])).toBe(request.seedDocument);
    await close(); close = undefined;
    const unconfigured = createHarness({ oauth: new FakeOAuthGrantProvider(), tenantRepo: provider,
      broker: new FakeBrokerApsClient(), agentRunner: new FakeAgentRunner() },
      { auth: { enabled: false, secret: "" } }).listen(0);
    close = () => new Promise<void>((resolve, reject) => unconfigured.close(error => error ? reject(error) : resolve()));
    const denied = await fetch(`http://127.0.0.1:${(unconfigured.address() as AddressInfo).port}/internal/project-repository-source/initialize`,
      { method: "POST", body: "not JSON" });
    expect(denied.status).toBe(401);
  });
});
