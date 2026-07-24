/**
 * Hermetic end-to-end acceptance test (NO network, NO real Anthropic creds).
 *
 * POST /author {"description":"count entities per layer"} against the full
 * pipeline wired to fakes + a REAL git working copy of test/fixtures/tenant-repo:
 *   - 200 with body exactly shaped { tool, code, preview } (CONTRACT section 4);
 *   - `tool` validates against the frozen CONTRACT section 2 schema;
 *   - the temp tenant repo gains a tool-package dir (tool.json w/ SPEC section 7
 *     fields + an entry script);
 *   - registry.json gains EXACTLY one entry whose name === tool.name;
 *   - `git log` shows EXACTLY one commit authored by the harness identity.
 *
 * The checked-in fixture is never mutated (the fake checks out a temp clone).
 */

import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { createHarness } from "../src/server.js";
import type { HarnessPorts } from "../src/ports/index.js";
import { validateToolPackage } from "../src/registry/toolPackageSchema.js";
import { HARNESS_IDENTITY } from "../src/registry/registerTool.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { FakeTenantRepoProvider } from "../src/ports/fakes/fakeTenantRepo.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";
import type { TenantRepo } from "../src/ports/index.js";
import { AuthorLoop } from "../src/agent/authorLoop.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE = join(HERE, "fixtures", "tenant-repo");

function git(dir: string, args: string[]): string {
  return execFileSync("git", ["-C", dir, ...args], { encoding: "utf8" });
}

class LeaseAwareFakeTenantRepoProvider extends FakeTenantRepoProvider {
  leaseActive = false;
  leaseCalls = 0;
  readLeaseActive = false;
  readLeaseCalls = 0;

  override async withTenantLease<T>(_tenantId: string, action: () => Promise<T>): Promise<T> {
    this.leaseCalls += 1;
    this.leaseActive = true;
    try {
      return await action();
    } finally {
      this.leaseActive = false;
    }
  }

  async withTenantReadLease<T>(_tenantId: string, action: () => Promise<T>): Promise<T> {
    this.readLeaseCalls += 1;
    this.readLeaseActive = true;
    try {
      return await action();
    } finally {
      this.readLeaseActive = false;
    }
  }

  override async checkout(tenantId: string): Promise<TenantRepo> {
    expect(this.leaseActive || this.readLeaseActive).toBe(true);
    const repo = await super.checkout(tenantId);
    const commit = repo.commit.bind(repo);
    repo.commit = async (message, identity) => {
      expect(this.leaseActive).toBe(true);
      return commit(message, identity);
    };
    return repo;
  }
}

describe("POST /author (build route) - hermetic e2e", () => {
  let server: Server;
  let baseUrl: string;
  let tenantRepo: LeaseAwareFakeTenantRepoProvider;

  beforeEach(() => {
    tenantRepo = new LeaseAwareFakeTenantRepoProvider(FIXTURE);
    const ports: HarnessPorts = {
      oauth: new FakeOAuthGrantProvider(),
      tenantRepo,
      broker: new FakeBrokerApsClient(),
      agentRunner: new FakeAgentRunner(),
    };
    server = createHarness(ports).listen(0);
    const addr = server.address() as AddressInfo;
    baseUrl = `http://127.0.0.1:${addr.port}`;
  });

  afterEach(() => {
    server.close();
  });

  it("authors, validates, registers, and commits a deterministic tool", async () => {
    const res = await fetch(`${baseUrl}/author`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ description: "count entities per layer" }),
    });

    // (1) 200 + exact { tool, code, preview } shape
    expect(res.status).toBe(200);
    const body = (await res.json()) as { tool: unknown; code: unknown; preview: unknown };
    expect(Object.keys(body).sort()).toEqual(["code", "preview", "tool"]);
    expect(typeof body.code).toBe("string");
    expect(typeof body.preview).toBe("string");

    // (1b) tool validates against the frozen CONTRACT section 2 schema
    const diagnostics = validateToolPackage(body.tool);
    expect(diagnostics).toEqual([]);
    const tool = body.tool as { name: string; engine_op: string };
    expect(tool.name).toBe("count-entities-per-layer");
    expect(tool.engine_op).toBe("count_by_layer");

    // The temp checkout the pipeline operated on
    const repoDir = tenantRepo.lastCheckout!.dir;
    expect(repoDir).toBeTruthy();

    // (2) new tool-package dir: tool.json (SPEC section 7 fields) + entry script
    const pkgDir = join(repoDir, "tools", tool.name);
    const manifestPath = join(pkgDir, "tool.json");
    expect(existsSync(manifestPath)).toBe(true);
    const manifest = JSON.parse(readFileSync(manifestPath, "utf8")) as Record<string, unknown>;
    // SPEC section 7.1 fields present:
    expect(manifest.entry).toBe("tool.py");
    expect(manifest.timeout_ms).toBeTypeOf("number");
    expect(manifest.idempotent).toBe(true);
    expect((manifest.review as { status: string }).status).toBe("unreviewed");
    // and it still validates against CONTRACT section 2:
    expect(validateToolPackage(manifest)).toEqual([]);
    // the entry script exists
    expect(existsSync(join(pkgDir, "tool.py"))).toBe(true);
    expect(readFileSync(join(pkgDir, "tool.py"), "utf8")).toContain("def run(intake, params)");

    // (2b) registry.json gained EXACTLY one entry, name === tool.name
    const registry = JSON.parse(readFileSync(join(repoDir, "registry.json"), "utf8")) as {
      tools: { name: string }[];
    };
    expect(registry.tools).toHaveLength(2); // 1 fixture (count-by-layer) + 1 new
    const added = registry.tools.filter((t) => t.name === tool.name);
    expect(added).toHaveLength(1);

    // (2c) exactly one commit authored by the harness identity, message names the tool
    const harnessLog = git(repoDir, ["log", `--author=${HARNESS_IDENTITY.email}`, "--oneline"])
      .split("\n")
      .filter((l) => l.trim().length > 0);
    expect(harnessLog).toHaveLength(1);
    expect(tenantRepo.leaseCalls).toBe(1);
    expect(tenantRepo.leaseActive).toBe(false);
    const subject = git(repoDir, ["log", "-1", `--author=${HARNESS_IDENTITY.email}`, "--format=%s"]).trim();
    expect(subject).toContain(tool.name);
  });

  it("does not mutate the checked-in fixture", () => {
    // The pristine fixture still has exactly its one seeded tool.
    const registry = JSON.parse(readFileSync(join(FIXTURE, "registry.json"), "utf8")) as {
      tools: { name: string }[];
    };
    expect(registry.tools.map((t) => t.name)).toEqual(["count-by-layer"]);
    expect(existsSync(join(FIXTURE, "tools"))).toBe(false);
  });

  it("kill switch refuses authoring before any tenant checkout", async () => {
    const previous = process.env.LEAF_AUTHORED_EXECUTION;
    process.env.LEAF_AUTHORED_EXECUTION = "0";
    try {
      const res = await fetch(`${baseUrl}/author`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ description: "count entities per layer" }),
      });
      expect(res.status).toBe(403);
      expect(tenantRepo.lastCheckout).toBeNull();
    } finally {
      if (previous === undefined) delete process.env.LEAF_AUTHORED_EXECUTION;
      else process.env.LEAF_AUTHORED_EXECUTION = previous;
    }
  });

  it("holds the tenant read lease through registered-tool lookup and execution", async () => {
    const loop = new AuthorLoop({
      oauth: new FakeOAuthGrantProvider(),
      tenantRepo,
      broker: new FakeBrokerApsClient(),
      agentRunner: new FakeAgentRunner(),
    });
    await loop.run("tenant-read", "count-by-layer");
    expect(tenantRepo.readLeaseCalls).toBe(1);
    expect(tenantRepo.readLeaseActive).toBe(false);
  });
});
