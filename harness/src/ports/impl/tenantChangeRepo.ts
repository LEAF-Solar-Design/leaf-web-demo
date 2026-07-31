/**
 * Isolated Git change-set adapter. A change set always has its own detached
 * worktree and private ref. The tenant checkout is never used as a worktree.
 */

import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, realpathSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { scrubSecrets } from "./envScrub.js";
import type { HarnessIdentity } from "../index.js";

const ZERO_SHA = "0".repeat(40);
const CHANGE_SET_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SHA_RE = /^[0-9a-f]{40}$/i;

export interface TenantChangeRepoOptions {
  /** Absolute path to the tenant repository's bare Git directory. */
  repoDir: string;
  /** Parent directory for isolated change-set worktrees. Defaults to the OS temp dir. */
  workBase?: string;
  /** Identity used for staged commits. */
  identity: HarnessIdentity;
}

export interface TenantChangeSet {
  readonly id: string;
  readonly ref: string;
  readonly dir: string;
  readonly expectedBaseSha: string;
  /** The last commit successfully stored on this change set's ref. */
  stagedSha: string | null;
}

/** A failed Git compare-and-swap. Callers can use the SHAs to mark a change conflicted. */
export class GitRefConflictError extends Error {
  override readonly name = "GitRefConflictError";

  constructor(
    readonly ref: string,
    readonly expectedSha: string | null,
    readonly observedSha: string | null,
  ) {
    super(`Git ref conflict for ${ref}: expected ${expectedSha ?? "absent"}, observed ${observedSha ?? "absent"}`);
  }
}

function assertSha(sha: string, label: string): void {
  if (!SHA_RE.test(sha)) throw new Error(`${label} must be a 40-character Git SHA`);
}

function changeRef(changeSetId: string): string {
  if (!CHANGE_SET_ID_RE.test(changeSetId)) throw new Error("changeSetId must be a UUID");
  return `refs/leaf/changes/${changeSetId.toLowerCase()}`;
}

/**
 * A Git adapter for one tenant repository. All updates use `git update-ref`
 * with an expected old SHA, so neither change refs nor main can be overwritten.
 */
export class TenantChangeRepo {
  constructor(private readonly opts: TenantChangeRepoOptions) {
    const bare = this.git(["rev-parse", "--is-bare-repository"]).trim();
    if (bare !== "true") {
      throw new Error("TenantChangeRepo requires a bare repository");
    }
  }

  private git(args: string[], cwd = this.opts.repoDir): string {
    try {
      const safeDirectories = new Set([cwd, this.opts.repoDir].map((dir) =>
        realpathSync(dir).replaceAll("\\", "/")));
      const safeConfig = ["-c", "safe.directory="];
      for (const dir of safeDirectories) safeConfig.push("-c", `safe.directory=${dir}`);
      return execFileSync("git", [...safeConfig, ...args], {
        cwd,
        encoding: "utf8",
        env: scrubSecrets(process.env),
      });
    } catch (error) {
      const err = error as { status?: number; stderr?: string; stdout?: string };
      throw new Error(`git ${args[0]} failed: status=${err.status} stderr=${err.stderr ?? ""} stdout=${err.stdout ?? ""}`);
    }
  }

  /** Read a ref without treating an absent ref as an error. */
  readRef(ref: string): string | null {
    try {
      return this.git(["rev-parse", "--verify", "--quiet", ref]).trim() || null;
    } catch {
      return null;
    }
  }

  readChangeRef(changeSetId: string): string | null {
    return this.readRef(changeRef(changeSetId));
  }

  private updateRef(ref: string, newSha: string, expectedOldSha: string | null): void {
    assertSha(newSha, "new SHA");
    if (expectedOldSha) assertSha(expectedOldSha, "expected SHA");
    try {
      this.git(["update-ref", ref, newSha, expectedOldSha ?? ZERO_SHA]);
    } catch (error) {
      const observedSha = this.readRef(ref);
      if (observedSha !== expectedOldSha) {
        throw new GitRefConflictError(ref, expectedOldSha, observedSha);
      }
      throw error;
    }
  }

  /**
   * Reserve a deterministic change ref at the expected base and add a detached,
   * isolated worktree. The source tenant checkout is never modified.
   */
  create(changeSetId: string, expectedBaseSha: string): TenantChangeSet {
    assertSha(expectedBaseSha, "expected base SHA");
    const ref = changeRef(changeSetId);
    this.updateRef(ref, expectedBaseSha, null);
    const workBase = this.opts.workBase ?? tmpdir();
    mkdirSync(workBase, { recursive: true });
    const dir = mkdtempSync(join(workBase, `leaf-change-${changeSetId}-`));
    try {
      this.git(["-c", "core.autocrlf=false", "worktree", "add", "--detach", dir, expectedBaseSha]);
    } catch (error) {
      // Delete only the ref reserved by this call, and only if it still names the base.
      try {
        this.git(["update-ref", "-d", ref, expectedBaseSha]);
      } catch {
        // Preserve the original error. A surviving ref is recoverable by its owner.
      }
      throw error;
    }
    return { id: changeSetId, ref, dir, expectedBaseSha, stagedSha: null };
  }

  /**
   * Resume the deterministic ref left by an interrupted stage, or reserve it
   * when this is the first attempt. A surviving ref must descend from the
   * original base, so another writer cannot substitute unrelated content.
   */
  createOrResume(changeSetId: string, expectedBaseSha: string): TenantChangeSet {
    assertSha(expectedBaseSha, "expected base SHA");
    const ref = changeRef(changeSetId);
    const observed = this.readRef(ref);
    if (observed === null) return this.create(changeSetId, expectedBaseSha);
    assertSha(observed, "observed change SHA");
    const mergeBase = this.git(["merge-base", expectedBaseSha, observed]).trim();
    if (mergeBase !== expectedBaseSha) {
      throw new GitRefConflictError(ref, expectedBaseSha, observed);
    }
    const workBase = this.opts.workBase ?? tmpdir();
    mkdirSync(workBase, { recursive: true });
    const dir = mkdtempSync(join(workBase, `leaf-change-${changeSetId}-`));
    this.git(["-c", "core.autocrlf=false", "worktree", "add", "--detach", dir, observed]);
    return {
      id: changeSetId,
      ref,
      dir,
      expectedBaseSha,
      stagedSha: observed === expectedBaseSha ? null : observed,
    };
  }

  /** Commit the isolated worktree and advance only its private change ref. */
  stageCommit(change: TenantChangeSet, message: string): string {
    this.git(["add", "-A"], change.dir);
    this.git([
      "-c", `user.name=${this.opts.identity.name}`,
      "-c", `user.email=${this.opts.identity.email}`,
      "commit", "-m", message,
      `--author=${this.opts.identity.name} <${this.opts.identity.email}>`,
    ], change.dir);
    const commit = this.git(["rev-parse", "HEAD"], change.dir).trim();
    const expected = change.stagedSha ?? change.expectedBaseSha;
    this.updateRef(change.ref, commit, expected);
    change.stagedSha = commit;
    return commit;
  }

  /**
   * Atomically select this exact staged commit as main, if main still equals the
   * caller's expected SHA. This is a local compare-and-swap, never a force push.
   */
  publishToMain(change: TenantChangeSet, expectedMainSha: string): string {
    assertSha(expectedMainSha, "expected main SHA");
    if (!change.stagedSha) throw new Error("change set has no staged commit");
    const refSha = this.readRef(change.ref);
    if (refSha !== change.stagedSha) {
      throw new GitRefConflictError(change.ref, change.stagedSha, refSha);
    }
    this.updateRef("refs/heads/main", change.stagedSha, expectedMainSha);
    return change.stagedSha;
  }

  /** Remove the isolated worktree. Its immutable change ref remains for recovery. */
  cleanupWorktree(change: TenantChangeSet): void {
    this.git(["worktree", "remove", "--force", change.dir]);
  }
}
