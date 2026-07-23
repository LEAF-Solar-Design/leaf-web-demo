import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { GitRefConflictError, TenantChangeRepo } from "../src/ports/impl/tenantChangeRepo.js";

const IDENTITY = { name: "Leaf Harness", email: "harness@leaf.test" };
const CHANGE_A = "11111111-1111-4111-8111-111111111111";
const CHANGE_B = "22222222-2222-4222-8222-222222222222";

function git(dir: string, args: string[]): string {
  return execFileSync("git", args, { cwd: dir, encoding: "utf8" }).trim();
}

describe("TenantChangeRepo", () => {
  let root: string;
  let repoDir: string;
  let workBase: string;
  let seedDir: string;

  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), "leaf-change-repo-"));
    seedDir = join(root, "seed");
    repoDir = join(root, "tenant.git");
    workBase = join(root, "worktrees");
    git(root, ["init", "-b", "main", seedDir]);
    writeFileSync(join(seedDir, "registry.json"), "{\"tools\":[]}\n", "utf8");
    git(seedDir, ["add", "registry.json"]);
    git(seedDir, ["-c", `user.name=${IDENTITY.name}`, "-c", `user.email=${IDENTITY.email}`, "commit", "-m", "seed"]);
    git(root, ["init", "--bare", repoDir]);
    git(seedDir, ["remote", "add", "origin", repoDir]);
    git(seedDir, ["push", "origin", "main"]);
  });

  afterEach(() => {
    rmSync(root, { recursive: true, force: true });
  });

  it("uses isolated deterministic change refs without changing the tenant worktree", () => {
    const base = git(repoDir, ["rev-parse", "main"]);
    const changes = new TenantChangeRepo({ repoDir, workBase, identity: IDENTITY });
    const first = changes.create(CHANGE_A, base);
    const second = changes.create(CHANGE_B, base);

    expect(first.ref).toBe(`refs/leaf/changes/${CHANGE_A}`);
    expect(second.ref).toBe(`refs/leaf/changes/${CHANGE_B}`);
    expect(first.dir).not.toBe(repoDir);
    expect(second.dir).not.toBe(repoDir);
    expect(changes.readRef(first.ref)).toBe(base);
    expect(changes.readRef(second.ref)).toBe(base);
    expect(git(repoDir, ["worktree", "list", "--porcelain"])).not.toContain(seedDir);

    changes.cleanupWorktree(first);
    changes.cleanupWorktree(second);
  });

  it("does not let one change set overwrite another change ref", () => {
    const base = git(repoDir, ["rev-parse", "main"]);
    const changes = new TenantChangeRepo({ repoDir, workBase, identity: IDENTITY });
    const first = changes.create(CHANGE_A, base);
    const second = changes.create(CHANGE_B, base);
    writeFileSync(join(first.dir, "first.txt"), "first\n", "utf8");

    const staged = changes.stageCommit(first, "stage first");

    expect(changes.readRef(first.ref)).toBe(staged);
    expect(changes.readRef(second.ref)).toBe(base);
    changes.cleanupWorktree(first);
    changes.cleanupWorktree(second);
  });

  it("allows only one writer from the same main base to publish", () => {
    const base = git(repoDir, ["rev-parse", "main"]);
    const changes = new TenantChangeRepo({ repoDir, workBase, identity: IDENTITY });
    const first = changes.create(CHANGE_A, base);
    const second = changes.create(CHANGE_B, base);
    writeFileSync(join(first.dir, "first.txt"), "first\n", "utf8");
    writeFileSync(join(second.dir, "second.txt"), "second\n", "utf8");
    const firstCommit = changes.stageCommit(first, "stage first");
    changes.stageCommit(second, "stage second");

    expect(changes.publishToMain(first, base)).toBe(firstCommit);
    let conflict: unknown;
    try {
      changes.publishToMain(second, base);
    } catch (error) {
      conflict = error;
    }
    expect(conflict).toBeInstanceOf(GitRefConflictError);
    expect(conflict).toMatchObject({ expectedSha: base, observedSha: firstCommit });
    expect(git(repoDir, ["rev-parse", "main"])).toBe(firstCommit);

    changes.cleanupWorktree(first);
    changes.cleanupWorktree(second);
  });
});
