/**
 * REAL TenantRepoProvider - checks out the tenant's mushy-codebase git repo into
 * a per-session working dir and provides commit(). Live path is operator-gated
 * (needs the project-job-schema lane's tenant->repo mapping + real remotes). It
 * COMPILES now and models the real git flow.
 *
 * The mushy codebase is the per-tenant git repo of tool files the author session
 * edits; registry.json lives at its root.
 */

import { execFileSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { HarnessIdentity, TenantRepo, TenantRepoProvider } from "../index.js";

/** Resolves a tenant to its mushy-codebase git remote/worktree. */
export interface TenantRepoLocator {
  /** Return the git URL/path to clone for this tenant. */
  repoRef(tenantId: string): Promise<string>;
}

export interface TenantRepoProviderOptions {
  locator: TenantRepoLocator;
  /** Base dir for per-session checkouts (default: OS temp). */
  workBase?: string;
  /**
   * In-place mode: treat the locator's ref as a LOCAL working directory and operate
   * on it directly (no clone into a temp dir). This is the single-node demo model
   * where the tenant's mushy-repo is checked out on the app host at a known path, so
   * a `build` commit and a later `run` read the SAME registry.json + tool files.
   * The broker (whose cwd is that dir) then resolves the authored `entry` file.
   */
  inPlace?: boolean;
}

function git(cwd: string, args: string[], identity?: HarnessIdentity): string {
  const cfg = identity
    ? ["-c", `user.name=${identity.name}`, "-c", `user.email=${identity.email}`]
    : [];
  try {
    return execFileSync("git", [...cfg, ...args], { cwd, encoding: "utf8" });
  } catch (e) {
    const err = e as { status?: number; signal?: string; code?: string; stderr?: string; stdout?: string; message: string };
    throw new Error(
      `git ${args[0]} failed: status=${err.status} signal=${err.signal} code=${err.code} :: ` +
        `stderr=${err.stderr ?? ""} stdout=${err.stdout ?? ""}`,
    );
  }
}

class GitTenantRepo implements TenantRepo {
  constructor(readonly dir: string) {}

  async commit(message: string, identity: HarnessIdentity): Promise<{ commit: string }> {
    // Windows spawn-pressure: the Agent SDK turn spawns a large `claude` process
    // tree; spawning `git.exe` immediately after can fail with 0xC0000142
    // (STATUS_DLL_INIT_FAILED) until that tree fully releases desktop-heap/handles.
    // Let it settle, then retry with generous exponential backoff (~30s worst case).
    await new Promise((r) => setTimeout(r, 800));
    let lastErr: unknown;
    for (let attempt = 0; attempt < 8; attempt++) {
      try {
        git(this.dir, ["add", "-A"], identity);
        lastErr = undefined;
        break;
      } catch (e) {
        lastErr = e;
        await new Promise((r) => setTimeout(r, Math.min(4000, 500 * 2 ** attempt)));
      }
    }
    if (lastErr) throw lastErr;
    git(
      this.dir,
      ["commit", "-m", message, `--author=${identity.name} <${identity.email}>`],
      identity,
    );
    return { commit: git(this.dir, ["rev-parse", "HEAD"]).trim() };
  }
}

export class TenantRepoProviderImpl implements TenantRepoProvider {
  constructor(private readonly opts: TenantRepoProviderOptions) {}

  async checkout(tenantId: string): Promise<TenantRepo> {
    const ref = await this.opts.locator.repoRef(tenantId);
    if (this.opts.inPlace) {
      // ref IS the local working dir; operate on it directly (no temp clone).
      return new GitTenantRepo(ref);
    }
    const base = this.opts.workBase ?? tmpdir();
    const dir = mkdtempSync(join(base, `mushy-${tenantId}-`));
    git(dir, ["clone", "--depth", "1", ref, "."]);
    return new GitTenantRepo(dir);
  }
}
