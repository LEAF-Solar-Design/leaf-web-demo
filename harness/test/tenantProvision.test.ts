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

function providerWithBareBase(base: string, bareBase: string) {
  return new TenantRepoProviderImpl({
    locator: { async repoRef(tenantId: string) { return join(base, tenantId); } },
    inPlace: true,
    autoProvisionFrom: FIXTURE,
    bareBase,
  });
}

describe("TenantRepoProviderImpl auto-provision", () => {
  let base: string;
  let previousGlobalConfig: string | undefined;

  beforeEach(() => {
    base = mkdtempSync(join(tmpdir(), "leaf-tenants-"));
    previousGlobalConfig = process.env.GIT_CONFIG_GLOBAL;
    process.env.GIT_CONFIG_GLOBAL = join(base, "test.gitconfig");
  });
  afterEach(() => {
    if (previousGlobalConfig === undefined) delete process.env.GIT_CONFIG_GLOBAL;
    else process.env.GIT_CONFIG_GLOBAL = previousGlobalConfig;
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
    const previousOwnerTest = process.env.GIT_TEST_ASSUME_DIFFERENT_OWNER;
    process.env.GIT_TEST_ASSUME_DIFFERENT_OWNER = "1";
    try {
      const p = provider(base);
      const repo = await p.checkout("different-owner");
      expect(repo.dir).toBe(join(base, "different-owner"));
      expect(existsSync(join(repo.dir, ".git"))).toBe(true);
      const bare = await p.bare("different-owner");
      expect(git(bare.dir, ["show-ref", "--verify", "refs/heads/main"]).trim()).toContain("refs/heads/main");
    } finally {
      if (previousOwnerTest === undefined) delete process.env.GIT_TEST_ASSUME_DIFFERENT_OWNER;
      else process.env.GIT_TEST_ASSUME_DIFFERENT_OWNER = previousOwnerTest;
    }
  });

  it("recovers an interrupted provision and refreshes an existing empty bare clone", async () => {
    const dir = join(base, "interrupted");
    const bareBase = join(base, "tenant-git");
    const bareDir = join(bareBase, "interrupted.git");
    mkdirSync(dir, { recursive: true });
    mkdirSync(bareBase, { recursive: true });
    writeFileSync(
      join(dir, "registry.json"),
      JSON.stringify({ tools: [{ name: "preserved-tool" }] }),
      "utf8",
    );
    writeFileSync(join(dir, "preserved.txt"), "keep me", "utf8");
    git(dir, ["init", "-q", "-b", "main"]);
    git(bareBase, ["clone", "--bare", dir, bareDir]);

    const bare = await providerWithBareBase(base, bareBase).bare("interrupted");

    expect(git(dir, ["show-ref", "--verify", "refs/heads/main"]).trim())
      .toContain("refs/heads/main");
    expect(git(bare.dir, ["show-ref", "--verify", "refs/heads/main"]).trim())
      .toContain("refs/heads/main");
    expect(readFileSync(join(dir, "preserved.txt"), "utf8")).toBe("keep me");
    expect(JSON.parse(readFileSync(join(dir, "registry.json"), "utf8")))
      .toEqual({ tools: [{ name: "preserved-tool" }] });
    expect(git(dir, ["log", "--oneline"]).split("\n").filter(Boolean)).toHaveLength(1);
  });

  it("repairs an app-created empty bare repo without origin and preserves private refs", async () => {
    const dir = join(base, "missing-origin");
    const bareBase = join(base, "tenant-git");
    const bareDir = join(bareBase, "missing-origin.git");
    mkdirSync(dir, { recursive: true });
    mkdirSync(bareBase, { recursive: true });
    writeFileSync(join(dir, "registry.json"), JSON.stringify({ tools: [] }), "utf8");
    git(dir, ["init", "-q", "-b", "main"]);
    git(dir, [
      "-c", "user.name=Leaf Harness",
      "-c", "user.email=harness@leaf.local",
      "add", "registry.json",
    ]);
    git(dir, [
      "-c", "user.name=Leaf Harness",
      "-c", "user.email=harness@leaf.local",
      "commit", "-q", "-m", "seed",
    ]);

    git(bareBase, ["init", "-q", "--bare", bareDir]);
    git(bareDir, ["fetch", dir, "main:refs/leaf/changes/preserved"]);
    const privateRef = git(bareDir, ["rev-parse", "refs/leaf/changes/preserved"]).trim();
    expect(git(bareDir, ["remote"]).trim()).toBe("");

    const bare = await providerWithBareBase(base, bareBase).bare("missing-origin");

    expect(git(bare.dir, ["remote", "get-url", "origin"]).trim()).toBe(dir);
    expect(git(bare.dir, ["show-ref", "--verify", "refs/heads/main"]).trim())
      .toContain("refs/heads/main");
    expect(git(bare.dir, ["symbolic-ref", "HEAD"]).trim()).toBe("refs/heads/main");
    expect(git(bare.dir, ["rev-parse", "refs/leaf/changes/preserved"]).trim()).toBe(privateRef);
  });

  it("refuses to rewrite an existing bare origin for another repository", async () => {
    const bareBase = join(base, "tenant-git");
    const bareDir = join(bareBase, "wrong-origin.git");
    const wrongOrigin = join(base, "not-the-tenant-repo");
    mkdirSync(bareBase, { recursive: true });
    git(bareBase, ["init", "-q", "--bare", bareDir]);
    git(bareDir, ["remote", "add", "origin", wrongOrigin]);

    await expect(providerWithBareBase(base, bareBase).bare("wrong-origin"))
      .rejects.toThrow("existing bare origin does not match the canonical tenant repository");
    expect(git(bareDir, ["remote", "get-url", "origin"]).trim()).toBe(wrongOrigin);
    expect(() => git(bareDir, ["show-ref", "--verify", "refs/heads/main"])).toThrow();
    expect(existsSync(join(base, "wrong-origin"))).toBe(false);
  });

  it("does not rewind canonical bare main from the seed worktree on reuse", async () => {
    const bareBase = join(base, "tenant-git");
    const p = providerWithBareBase(base, bareBase);
    const bare = await p.bare("published-main");
    const sourceDir = join(base, "published-main");
    const originalMain = git(bare.dir, ["rev-parse", "refs/heads/main"]).trim();

    writeFileSync(join(sourceDir, "published.txt"), "published\n", "utf8");
    git(sourceDir, ["add", "published.txt"]);
    git(sourceDir, [
      "-c", "user.name=Leaf Harness",
      "-c", "user.email=harness@leaf.local",
      "commit", "-q", "-m", "published",
    ]);
    const published = git(sourceDir, ["rev-parse", "HEAD"]).trim();
    git(bare.dir, ["fetch", sourceDir, "main:refs/leaf/changes/published"]);
    git(bare.dir, ["update-ref", "refs/heads/main", published, originalMain]);
    git(sourceDir, ["reset", "--hard", originalMain]);

    const reopened = await p.bare("published-main");

    expect(git(reopened.dir, ["rev-parse", "refs/heads/main"]).trim()).toBe(published);
    expect(git(sourceDir, ["rev-parse", "refs/heads/main"]).trim()).toBe(originalMain);
  });

  it("opens a bare clone from an existing foreign-owned tenant repo", async () => {
    const dir = join(base, "partial");
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, "registry.json"), JSON.stringify({ tools: [] }), "utf8");
    git(dir, ["init", "-q", "-b", "main"]);
    git(dir, ["-c", "user.name=Leaf Harness", "-c", "user.email=harness@leaf.local", "add", "registry.json"]);
    git(dir, ["-c", "user.name=Leaf Harness", "-c", "user.email=harness@leaf.local", "commit", "-q", "-m", "seed"]);

    const previousOwnerTest = process.env.GIT_TEST_ASSUME_DIFFERENT_OWNER;
    process.env.GIT_TEST_ASSUME_DIFFERENT_OWNER = "1";
    try {
      const bare = await provider(base).bare("partial");
      expect(git(bare.dir, ["show-ref", "--verify", "refs/heads/main"]).trim()).toContain("refs/heads/main");
    } finally {
      if (previousOwnerTest === undefined) delete process.env.GIT_TEST_ASSUME_DIFFERENT_OWNER;
      else process.env.GIT_TEST_ASSUME_DIFFERENT_OWNER = previousOwnerTest;
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
