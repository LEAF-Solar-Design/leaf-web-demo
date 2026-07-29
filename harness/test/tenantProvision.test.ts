/**
 * TenantRepoProviderImpl auto-provision (wave 4).
 *
 * A brand-new tenant's mushy repo is materialized from the pristine fixture on first
 * checkout (copy + ensure __pycache__ .gitignore + git init + ONE seed commit). Later
 * checkouts see the existing repo and skip provisioning. An already-provisioned repo is
 * never clobbered. commit() still works (its Windows spawn-pressure retry is intact).
 */

import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { TenantRepoProviderImpl } from "../src/ports/impl/tenantRepoProvider.js";
import { HARNESS_IDENTITY } from "../src/registry/registerTool.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE = join(HERE, "fixtures", "tenant-repo");

function git(dir: string, args: string[]): string {
  return execFileSync("git", ["-C", dir, ...args], { encoding: "utf8" });
}

function provider(base: string) {
  return new TenantRepoProviderImpl({
    locator: { async repoRef(tenantId: string) { return join(base, tenantId); } },
    inPlace: true,
    autoProvisionFrom: FIXTURE,
  });
}

describe("TenantRepoProviderImpl auto-provision", () => {
  let base: string;

  beforeEach(() => {
    base = mkdtempSync(join(tmpdir(), "leaf-tenants-"));
  });
  afterEach(() => {
    rmSync(base, { recursive: true, force: true });
  });

  it("provisions a brand-new tenant repo from the fixture on first checkout", async () => {
    const repo = await provider(base).checkout("brand-new");
    const dir = join(base, "brand-new");

    expect(repo.dir).toBe(dir);
    expect(existsSync(join(dir, "registry.json"))).toBe(true);
    expect(existsSync(join(dir, ".git"))).toBe(true);

    // the __pycache__ .gitignore is present
    const gi = readFileSync(join(dir, ".gitignore"), "utf8");
    expect(gi.split(/\r?\n/).some((l) => l.trim() === "__pycache__/")).toBe(true);

    // EXACTLY one seed commit
    const log = git(dir, ["log", "--oneline"]).split("\n").filter((l) => l.trim().length > 0);
    expect(log).toHaveLength(1);
    expect(git(dir, ["log", "-1", "--format=%s"]).trim()).toBe("seed: provision tenant repo");
  });

  it("provisions when Git treats the tenant directory as owned by another user", async () => {
    const previous = process.env.GIT_TEST_ASSUME_DIFFERENT_OWNER;
    process.env.GIT_TEST_ASSUME_DIFFERENT_OWNER = "1";
    try {
      const repo = await provider(base).checkout("different-owner");
      expect(repo.dir).toBe(join(base, "different-owner"));
      expect(existsSync(join(repo.dir, ".git"))).toBe(true);
    } finally {
      if (previous === undefined) delete process.env.GIT_TEST_ASSUME_DIFFERENT_OWNER;
      else process.env.GIT_TEST_ASSUME_DIFFERENT_OWNER = previous;
    }
  });

  it("does not re-provision an existing repo on later checkout, and commit() works", async () => {
    const p = provider(base);
    const repo1 = await p.checkout("t1");
    const dir = join(base, "t1");

    // author a change + commit through the provider (retry path intact)
    writeFileSync(join(dir, "note.txt"), "hello", "utf8");
    const { commit } = await repo1.commit("author tool: note", HARNESS_IDENTITY);
    expect(commit).toMatch(/^[0-9a-f]{7,40}$/);

    // second checkout must NOT re-seed (history preserved: seed + author = 2 commits)
    const repo2 = await p.checkout("t1");
    expect(repo2.dir).toBe(dir);
    const log = git(dir, ["log", "--oneline"]).split("\n").filter((l) => l.trim().length > 0);
    expect(log).toHaveLength(2);
    expect(existsSync(join(dir, "note.txt"))).toBe(true);
  });

  it("never clobbers an already-provisioned repo (registry.json preserved)", async () => {
    const dir = join(base, "pre-existing");
    mkdirSync(dir, { recursive: true });
    const custom = { tools: [{ name: "pre-existing-tool" }] };
    writeFileSync(join(dir, "registry.json"), JSON.stringify(custom), "utf8");

    const repo = await provider(base).checkout("pre-existing");
    expect(repo.dir).toBe(dir);
    const reg = JSON.parse(readFileSync(join(dir, "registry.json"), "utf8")) as {
      tools: { name: string }[];
    };
    expect(reg.tools.map((t) => t.name)).toEqual(["pre-existing-tool"]); // untouched
  });
});
