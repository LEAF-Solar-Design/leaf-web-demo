import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
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

function gitBlob(dir: string, revision: string, path: string): Buffer {
  return execFileSync("git", ["cat-file", "blob", `${revision}:${path}`], { cwd: dir });
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

  it("materializes create and resume worktrees with blob-exact LF bytes", () => {
    git(repoDir, ["config", "core.autocrlf", "true"]);
    const base = git(repoDir, ["rev-parse", "main"]);
    const changes = new TenantChangeRepo({ repoDir, workBase, identity: IDENTITY });
    const created = changes.create(CHANGE_A, base);

    expect(readFileSync(join(created.dir, "registry.json")))
      .toEqual(gitBlob(repoDir, base, "registry.json"));
    expect(readFileSync(join(created.dir, "registry.json"), "utf8")).not.toContain("\r\n");

    writeFileSync(join(created.dir, "lf-only.txt"), "first\nsecond\n", "utf8");
    const staged = changes.stageCommit(created, "stage LF bytes");
    changes.cleanupWorktree(created);

    const resumed = changes.createOrResume(CHANGE_A, base);
    expect(resumed.stagedSha).toBe(staged);
    expect(readFileSync(join(resumed.dir, "registry.json")))
      .toEqual(gitBlob(repoDir, staged, "registry.json"));
    expect(readFileSync(join(resumed.dir, "lf-only.txt")))
      .toEqual(gitBlob(repoDir, staged, "lf-only.txt"));
    expect(readFileSync(join(resumed.dir, "lf-only.txt"), "utf8")).toBe("first\nsecond\n");
    changes.cleanupWorktree(resumed);
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

  it("binds the staged tree to the exact committed object after the private-ref swap", () => {
    const base = git(repoDir, ["rev-parse", "main"]);
    const changes = new TenantChangeRepo({ repoDir, workBase, identity: IDENTITY });
    const change = changes.create(CHANGE_A, base);
    writeFileSync(join(change.dir, "tree.txt"), "one\n", "utf8");

    const witness = changes.stageCommitWitness(change, "stage witness");

    expect(witness.base_commit).toBe(base);
    expect(witness.private_ref).toBe(`refs/leaf/changes/${CHANGE_A}`);
    expect(witness.staged_head_commit).toMatch(/^[0-9a-f]{40}$/);
    expect(witness.staged_tree).toBe(git(repoDir, ["rev-parse", `${witness.staged_head_commit}^{tree}`]));
    expect(changes.readRef(witness.private_ref)).toBe(witness.staged_head_commit);
    expect(Object.isFrozen(witness)).toBe(true);

    // The tree changes exactly when the committed bytes change.
    writeFileSync(join(change.dir, "tree.txt"), "two\n", "utf8");
    const second = changes.stageCommitWitness(change, "stage witness two");
    expect(second.staged_head_commit).not.toBe(witness.staged_head_commit);
    expect(second.staged_tree).not.toBe(witness.staged_tree);
    expect(second.staged_tree).toBe(git(repoDir, ["rev-parse", `${second.staged_head_commit}^{tree}`]));

    changes.cleanupWorktree(change);
  });

  it("refuses a stage witness when the private ref expected value drifted", () => {
    const base = git(repoDir, ["rev-parse", "main"]);
    const changes = new TenantChangeRepo({ repoDir, workBase, identity: IDENTITY });
    const change = changes.create(CHANGE_A, base);
    writeFileSync(join(change.dir, "drift.txt"), "drift\n", "utf8");
    // Simulate a caller-supplied stale staged value: the private-ref CAS must refuse.
    change.stagedSha = "0".repeat(39) + "1";

    expect(() => changes.stageCommitWitness(change, "stage drift")).toThrow(GitRefConflictError);
    expect(changes.readRef(change.ref)).toBe(base);

    changes.cleanupWorktree(change);
  });

  it("returns the full publish observation and refuses a stale main", () => {
    const base = git(repoDir, ["rev-parse", "main"]);
    const changes = new TenantChangeRepo({ repoDir, workBase, identity: IDENTITY });
    const first = changes.create(CHANGE_A, base);
    const second = changes.create(CHANGE_B, base);
    writeFileSync(join(first.dir, "first.txt"), "first\n", "utf8");
    writeFileSync(join(second.dir, "second.txt"), "second\n", "utf8");
    const firstWitness = changes.stageCommitWitness(first, "stage first");
    changes.stageCommitWitness(second, "stage second");

    const observation = changes.publishToMainObserved(first, base);
    expect(observation).toEqual({
      private_ref: firstWitness.private_ref,
      private_ref_commit: firstWitness.staged_head_commit,
      before_main_commit: base,
      after_main_commit: firstWitness.staged_head_commit,
      after_main_tree: firstWitness.staged_tree,
      compare_and_swap: true,
    });
    expect(Object.isFrozen(observation)).toBe(true);
    expect(git(repoDir, ["rev-parse", "main"])).toBe(firstWitness.staged_head_commit);

    // A stale expected main is refused before any mutation.
    let conflict: unknown;
    try {
      changes.publishToMainObserved(second, base);
    } catch (error) {
      conflict = error;
    }
    expect(conflict).toBeInstanceOf(GitRefConflictError);
    expect(git(repoDir, ["rev-parse", "main"])).toBe(firstWitness.staged_head_commit);

    changes.cleanupWorktree(first);
    changes.cleanupWorktree(second);
  });

  it("treats an exact already-published retry as read-only and rejects any drift", () => {
    const base = git(repoDir, ["rev-parse", "main"]);
    const changes = new TenantChangeRepo({ repoDir, workBase, identity: IDENTITY });
    const change = changes.create(CHANGE_A, base);
    writeFileSync(join(change.dir, "published.txt"), "published\n", "utf8");
    const witness = changes.stageCommitWitness(change, "stage");
    const first = changes.publishToMainObserved(change, base);

    const retry = changes.publishToMainObserved(change, base);
    expect(retry.compare_and_swap).toBe(false);
    expect(retry.before_main_commit).toBe(witness.staged_head_commit);
    expect(retry.after_main_commit).toBe(witness.staged_head_commit);
    expect(retry.after_main_tree).toBe(witness.staged_tree);
    expect(retry.private_ref_commit).toBe(witness.staged_head_commit);
    expect(retry.after_main_tree).toBe(first.after_main_tree);

    // A moved private ref is never "already published": presence is not proof.
    git(repoDir, ["update-ref", witness.private_ref, base]);
    expect(() => changes.publishToMainObserved(change, base)).toThrow(GitRefConflictError);
    expect(git(repoDir, ["rev-parse", "main"])).toBe(witness.staged_head_commit);
    git(repoDir, ["update-ref", witness.private_ref, witness.staged_head_commit]);

    // A main that moved past the staged commit is a typed conflict, not a retry.
    const other = changes.create(CHANGE_B, base);
    writeFileSync(join(other.dir, "other.txt"), "other\n", "utf8");
    const otherWitness = changes.stageCommitWitness(other, "stage other");
    git(repoDir, ["update-ref", "refs/heads/main", otherWitness.staged_head_commit]);
    expect(() => changes.publishToMainObserved(change, base)).toThrow(GitRefConflictError);
    expect(git(repoDir, ["rev-parse", "main"])).toBe(otherWitness.staged_head_commit);

    changes.cleanupWorktree(change);
    changes.cleanupWorktree(other);
  });

  it("observes the Git matrix without mutating any ref", () => {
    const base = git(repoDir, ["rev-parse", "main"]);
    const changes = new TenantChangeRepo({ repoDir, workBase, identity: IDENTITY });
    const change = changes.create(CHANGE_A, base);
    writeFileSync(join(change.dir, "observe.txt"), "observe\n", "utf8");
    const witness = changes.stageCommitWitness(change, "stage");

    const matrix = changes.observeGitMatrix(change);
    expect(matrix).toEqual({
      private_ref_commit: witness.staged_head_commit,
      main_commit: base,
      main_tree: git(repoDir, ["rev-parse", `${base}^{tree}`]),
    });
    expect(Object.isFrozen(matrix)).toBe(true);
    expect(changes.readRef(witness.private_ref)).toBe(witness.staged_head_commit);
    expect(git(repoDir, ["rev-parse", "main"])).toBe(base);

    changes.cleanupWorktree(change);
  });
});
