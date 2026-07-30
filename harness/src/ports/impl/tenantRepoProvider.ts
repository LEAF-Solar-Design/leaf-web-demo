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
import { AsyncLocalStorage } from "node:async_hooks";
import { randomUUID } from "node:crypto";
import { scrubSecrets } from "./envScrub.js";
import { gitWorkerAvailable, workerCommit } from "./gitWorker.js";
import { redactTokens } from "../../redact.js";
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { Pool } from "pg";
import type { PoolClient, PoolConfig } from "pg";
import { HARNESS_IDENTITY } from "../../registry/registerTool.js";
import type {
  HarnessIdentity,
  TenantBareRepo,
  TenantMutationFence,
  TenantRepo,
  TenantRepoProvider,
} from "../index.js";

/** Resolves a tenant to its mushy-codebase git remote/worktree. */
export interface TenantRepoLocator {
  /** Return the git URL/path to clone for this tenant. */
  repoRef(tenantId: string): Promise<string>;
}

export interface TenantRepoProviderOptions {
  locator: TenantRepoLocator;
  /** Base dir for per-session checkouts (default: OS temp). */
  workBase?: string;
  /** Durable base directory for canonical bare tenant repositories. */
  bareBase?: string;
  /**
   * In-place mode: treat the locator's ref as a LOCAL working directory and operate
   * on it directly (no clone into a temp dir). This is the single-node demo model
   * where the tenant's mushy-repo is checked out on the app host at a known path, so
   * a `build` commit and a later `run` read the SAME registry.json + tool files.
   * The broker (whose cwd is that dir) then resolves the authored `entry` file.
   */
  inPlace?: boolean;
  /**
   * Wave 4 auto-provision (in-place only): when set, the FIRST checkout of a tenant
   * whose repo does not yet exist materializes it from this pristine fixture dir
   * (copy + ensure the __pycache__ .gitignore + `git init` + ONE seed commit). Later
   * checkouts see the existing repo and skip provisioning. This is what makes a brand
   * new tenant's first authoring "just work" with no manual repo setup.
   */
  autoProvisionFrom?: string;
  /** Git identity for the seed commit (default HARNESS_IDENTITY). */
  seedIdentity?: HarnessIdentity;
  /** PostgreSQL lease authority. False keeps the legacy single-process mode. */
  lease?: PgTenantRepoLeaseCoordinator | false;
  /** Explicit live authoring mode. Defaults to LEAF_HARNESS_AUTHORING_MODE, then disabled. */
  authoringMode?: "disabled" | "singleton" | "fleet";
}

export function resolveAuthoringMode(
  env: NodeJS.ProcessEnv = process.env,
): "disabled" | "singleton" | "fleet" {
  const mode = (env.LEAF_HARNESS_AUTHORING_MODE ?? "disabled").trim().toLowerCase();
  if (mode !== "disabled" && mode !== "singleton" && mode !== "fleet") {
    throw new Error("LEAF_HARNESS_AUTHORING_MODE must be disabled, singleton, or fleet");
  }
  return mode;
}

export function assertAuthoringModeSafe(
  mode: "disabled" | "singleton" | "fleet",
): void {
  if (mode === "disabled") {
    throw new Error(
      "build authoring is disabled; set LEAF_HARNESS_AUTHORING_MODE=singleton explicitly",
    );
  }
  if (mode === "fleet") {
    throw new Error(
      "multi-task build authoring is disabled until LEAF_GRANT_STORE=vault has a real Secrets Manager backend",
    );
  }
}

export class TenantRepoLeaseHeldError extends Error {
  constructor(readonly tenantId: string) {
    super(`tenant repo lease is held: ${tenantId}`);
    this.name = "TenantRepoLeaseHeldError";
  }
}

export class TenantRepoLeaseLostError extends Error {
  constructor(readonly tenantId: string) {
    super(`tenant repo lease was lost: ${tenantId}`);
    this.name = "TenantRepoLeaseLostError";
  }
}

interface RepoLease {
  tenantId: string;
  ownerToken: string;
  generation: string;
  lost: boolean;
  timer?: NodeJS.Timeout;
  repoDirs: Set<string>;
}

export interface PgTenantRepoLeaseCoordinatorOptions {
  pool?: Pool;
  poolConfig?: PoolConfig;
  ttlMs?: number;
  tableName?: string;
}

function quoteIdentifier(value: string): string {
  if (!/^[a-z_][a-z0-9_]*$/i.test(value)) {
    throw new Error(`invalid PostgreSQL identifier: ${JSON.stringify(value)}`);
  }
  return `"${value}"`;
}

/**
 * Fleet-wide, per-tenant authoring lease. Advisory transaction locks serialize
 * transitions. Owner token plus generation fences stale workers.
 */
export class PgTenantRepoLeaseCoordinator {
  private readonly pool: Pool;
  private readonly ownsPool: boolean;
  private readonly ttlMs: number;
  private readonly table: string;

  constructor(opts: PgTenantRepoLeaseCoordinatorOptions) {
    if (!opts.pool && !opts.poolConfig) {
      throw new Error("PgTenantRepoLeaseCoordinator requires an explicit pool or poolConfig");
    }
    this.pool = opts.pool ?? new Pool(opts.poolConfig);
    this.ownsPool = !opts.pool;
    this.ttlMs = opts.ttlMs ?? 120_000;
    if (!Number.isFinite(this.ttlMs) || this.ttlMs < 250) {
      throw new Error("tenant repo lease ttlMs must be finite and at least 250");
    }
    this.table = quoteIdentifier(opts.tableName ?? "harness_tenant_repo_leases");
  }

  async close(): Promise<void> {
    if (this.ownsPool) await this.pool.end();
  }

  private async transaction<T>(
    tenantId: string,
    fn: (client: PoolClient) => Promise<T>,
  ): Promise<T> {
    const client = await this.pool.connect();
    try {
      await client.query("BEGIN");
      await client.query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", [tenantId]);
      const result = await fn(client);
      await client.query("COMMIT");
      return result;
    } catch (error) {
      await client.query("ROLLBACK").catch(() => undefined);
      throw error;
    } finally {
      client.release();
    }
  }

  private async acquire(tenantId: string): Promise<RepoLease> {
    const ownerToken = randomUUID();
    const row = await this.transaction(tenantId, async (client) => {
      const result = await client.query<{ owner_token: string; generation: string }>(
        `INSERT INTO ${this.table}
           (tenant_id, owner_token, generation, acquired_at, heartbeat_at, expires_at)
         VALUES ($1, $2, 1, clock_timestamp(), clock_timestamp(),
                 clock_timestamp() + ($3 * interval '1 millisecond'))
         ON CONFLICT (tenant_id) DO UPDATE
           SET owner_token = EXCLUDED.owner_token,
               generation = ${this.table}.generation + 1,
               acquired_at = clock_timestamp(),
               heartbeat_at = clock_timestamp(),
               expires_at = clock_timestamp() + ($3 * interval '1 millisecond')
         WHERE ${this.table}.expires_at <= clock_timestamp()
         RETURNING owner_token, generation::text`,
        [tenantId, ownerToken, this.ttlMs],
      );
      return result.rows[0];
    });
    if (!row) throw new TenantRepoLeaseHeldError(tenantId);
    return {
      tenantId,
      ownerToken: row.owner_token,
      generation: row.generation,
      lost: false,
      repoDirs: new Set(),
    };
  }

  private async heartbeat(lease: RepoLease): Promise<void> {
    if (lease.lost) return;
    const result = await this.pool.query(
      `UPDATE ${this.table}
          SET heartbeat_at = clock_timestamp(),
              expires_at = clock_timestamp() + ($4 * interval '1 millisecond')
        WHERE tenant_id = $1 AND owner_token = $2 AND generation = $3
          AND expires_at > clock_timestamp()`,
      [lease.tenantId, lease.ownerToken, lease.generation, this.ttlMs],
    );
    if (result.rowCount !== 1) lease.lost = true;
  }

  async runFenced<T>(lease: RepoLease, action: () => Promise<T>): Promise<T> {
    if (lease.lost) throw new TenantRepoLeaseLostError(lease.tenantId);
    return this.transaction(lease.tenantId, async (client) => {
      const current = await client.query(
        `SELECT 1 FROM ${this.table}
          WHERE tenant_id = $1 AND owner_token = $2 AND generation = $3
            AND expires_at > clock_timestamp()
          FOR UPDATE`,
        [lease.tenantId, lease.ownerToken, lease.generation],
      );
      if (current.rowCount !== 1) {
        lease.lost = true;
        throw new TenantRepoLeaseLostError(lease.tenantId);
      }
      const result = await action();
      await client.query(
        `UPDATE ${this.table}
            SET heartbeat_at = clock_timestamp(),
                expires_at = clock_timestamp() + ($4 * interval '1 millisecond')
          WHERE tenant_id = $1 AND owner_token = $2 AND generation = $3`,
        [lease.tenantId, lease.ownerToken, lease.generation, this.ttlMs],
      );
      return result;
    });
  }

  async withLease<T>(tenantId: string, action: (lease: RepoLease) => Promise<T>): Promise<T> {
    const lease = await this.acquire(tenantId);
    lease.timer = setInterval(() => {
      void this.heartbeat(lease).catch(() => {
        lease.lost = true;
      });
    }, Math.max(100, Math.floor(this.ttlMs / 3)));
    lease.timer.unref();
    try {
      return await action(lease);
    } finally {
      clearInterval(lease.timer);
      await this.transaction(tenantId, async (client) => {
        await client.query(
          `UPDATE ${this.table}
              SET heartbeat_at = clock_timestamp(), expires_at = clock_timestamp()
            WHERE tenant_id = $1 AND owner_token = $2 AND generation = $3`,
          [tenantId, lease.ownerToken, lease.generation],
        );
      }).catch(() => undefined);
    }
  }
}

const trustedGitDirectories = new Set<string>();

/**
 * Git accepts safe.directory only from protected configuration. Tenant repos
 * can be owned by the EFS access-point UID rather than the container UID, so
 * add only the exact resolved worktree and git-dir paths to this task's
 * ephemeral global config. Local clone/upload-pack children inherit the same
 * protected config. No wildcard trust is permitted.
 */
function trustSharedRepo(dir: string): void {
  const root = (existsSync(dir) ? realpathSync(dir) : resolve(dir)).replaceAll("\\", "/");
  const configScope = process.env.GIT_CONFIG_GLOBAL ?? "<default>";
  for (const candidate of [root, join(root, ".git")].map((path) => path.replaceAll("\\", "/"))) {
    const cacheKey = `${configScope}\0${candidate}`;
    if (trustedGitDirectories.has(cacheKey)) continue;
    execFileSync("git", ["config", "--global", "--add", "safe.directory", candidate], {
      cwd: tmpdir(),
      encoding: "utf8",
      env: scrubSecrets(process.env),
    });
    trustedGitDirectories.add(cacheKey);
  }
}

function git(cwd: string, args: string[], identity?: HarnessIdentity): string {
  const cfg = identity
    ? ["-c", `user.name=${identity.name}`, "-c", `user.email=${identity.email}`]
    : [];
  try {
    // Scrubbed env (sol-critic R5, same rule as the git worker): git and anything
    // it runs (filters, hooks) must never inherit a credential or hop secret.
    return execFileSync("git", [...cfg, ...args], { cwd, encoding: "utf8", env: scrubSecrets(process.env) });
  } catch (e) {
    const err = e as { status?: number; signal?: string; code?: string; stderr?: string; stdout?: string; message: string };
    throw new Error(
      `git ${args[0]} failed: status=${err.status} signal=${err.signal} code=${err.code} :: ` +
        `stderr=${err.stderr ?? ""} stdout=${err.stdout ?? ""}`,
    );
  }
}

class GitTenantRepo implements TenantRepo {
  constructor(
    readonly dir: string,
    private readonly fence?: <T>(action: () => Promise<T>) => Promise<T>,
  ) {}

  async commit(message: string, identity: HarnessIdentity): Promise<{ commit: string }> {
    if (this.fence) {
      return this.fence(() => this.commitUnfenced(message, identity));
    }
    return this.commitUnfenced(message, identity);
  }

  /** Keep the registry mutation and its Git commit in one fenced transaction. */
  async mutateAndCommit(
    mutation: () => void,
    message: string,
    identity: HarnessIdentity,
  ): Promise<{ commit: string }> {
    const action = async () => {
      mutation();
      return this.commitUnfenced(message, identity);
    };
    return this.fence ? this.fence(action) : action();
  }

  private async commitUnfenced(
    message: string,
    identity: HarnessIdentity,
  ): Promise<{ commit: string }> {
    // Preferred path: the boot-time git worker (clean spawn context, immune to the
    // 0xC0000142 spawn failures this process suffers after hosting an SDK tree).
    if (gitWorkerAvailable()) {
      const r = await workerCommit({
        dir: this.dir, message, name: identity.name, email: identity.email,
      });
      if (r.ok && r.commit) return { commit: r.commit };
      console.error('[harness] git worker commit failed, falling back in-process:',
        redactTokens(String(r.error ?? "")));
      // fall through to the in-process retry path on worker failure
    }
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

const GITIGNORE_PYCACHE = "__pycache__/\n*.pyc\n";

/** True iff `dir` already looks like a provisioned tenant repo (has registry.json). */
function repoExists(dir: string): boolean {
  return existsSync(join(dir, "registry.json"));
}

function repoHasMain(dir: string): boolean {
  if (!existsSync(join(dir, ".git"))) return false;
  try {
    return /^[0-9a-f]{40}$/i.test(
      git(dir, ["rev-parse", "--verify", "refs/heads/main"]).trim(),
    );
  } catch {
    return false;
  }
}

function repoHead(dir: string): string | null {
  if (!existsSync(join(dir, ".git"))) return null;
  try {
    const head = git(dir, ["rev-parse", "--verify", "HEAD"]).trim();
    return /^[0-9a-f]{40}$/i.test(head) ? head : null;
  } catch {
    return null;
  }
}

function bareRepoHasMain(dir: string): boolean {
  try {
    return /^[0-9a-f]{40}$/i.test(
      git(dir, ["rev-parse", "--verify", "refs/heads/main"]).trim(),
    );
  } catch {
    return false;
  }
}

/**
 * Bind one existing durable bare repo to its canonical tenant worktree.
 *
 * The app's older bootstrap path could leave `HEAD` in the shared bare
 * directory without configuring `origin`. Treating `HEAD` alone as a complete
 * clone made the first harness repair fail at `git fetch origin`. Adding the
 * missing origin preserves every private change ref already present. An
 * existing different origin is refused rather than silently rewritten.
 */
function ensureBareOrigin(dir: string, sourceRef: string): void {
  const readRemotes = () => git(dir, ["remote"])
      .split(/\r?\n/)
      .map((remote) => remote.trim())
      .filter(Boolean);
  const remotes = readRemotes();
  if (!remotes.includes("origin")) {
    try {
      git(dir, ["remote", "add", "origin", sourceRef]);
    } catch {
      // A concurrent first request may have added the same remote while Git
      // held config.lock. Re-read below and accept only the canonical value.
    }
  }
  const origin = git(dir, ["remote", "get-url", "origin"]).trim();
  const canonical = (value: string) => {
    const absolute = resolve(dir, value);
    return existsSync(absolute) ? realpathSync(absolute) : absolute;
  };
  if (canonical(origin) !== canonical(sourceRef)) {
    throw new Error("existing bare origin does not match the canonical tenant repository");
  }
}

function resetWorkingTree(dir: string): void {
  git(dir, ["reset", "--hard", "HEAD"]);
  git(dir, ["clean", "-fd"]);
}

export class TenantRepoProviderImpl implements TenantRepoProvider {
  private readonly bareRepos = new Map<string, TenantBareRepo & { sourceRef?: string }>();
  private readonly lease?: PgTenantRepoLeaseCoordinator;
  private readonly leaseContext = new AsyncLocalStorage<RepoLease>();
  private readonly authoringMode: "disabled" | "singleton" | "fleet";

  constructor(private readonly opts: TenantRepoProviderOptions) {
    this.authoringMode = opts.authoringMode ?? resolveAuthoringMode();
    if (opts.lease === false) return;
    if (opts.lease) {
      this.lease = opts.lease;
      return;
    }
    if (
      (process.env.LEAF_HARNESS_SESSION_STORE ?? "file").trim().toLowerCase() ===
      "postgres"
    ) {
      const connectionString = (
        process.env.LEAF_HARNESS_DATABASE_URL ??
        process.env.DATABASE_URL ??
        ""
      ).trim();
      if (!connectionString) {
        throw new Error(
          "PostgreSQL harness mode requires LEAF_HARNESS_DATABASE_URL or DATABASE_URL for tenant repo leases",
        );
      }
      this.lease = new PgTenantRepoLeaseCoordinator({
        poolConfig: {
          connectionString,
          max: 5,
          application_name: "leaf-platform-harness-repo-lease",
        },
      });
    }
  }

  /** Hold one tenant lease across checkout, Agent SDK edits, registry update, and commit. */
  async withTenantLease<T>(
    tenantId: string,
    action: (runFenced: TenantMutationFence) => Promise<T>,
  ): Promise<T> {
    assertAuthoringModeSafe(this.authoringMode);
    if (!this.lease) {
      throw new Error("PostgreSQL tenant repository writer lease is required");
    }
    return this.lease.withLease(tenantId, (lease) =>
      this.leaseContext.run(lease, async () => {
        const runFenced = <R>(operation: () => R | Promise<R>) =>
          this.lease!.runFenced(lease, async () => operation());
        let primaryError: unknown;
        try {
          return await action(runFenced);
        } catch (error) {
          primaryError = error;
          throw error;
        } finally {
          const cleanupErrors: unknown[] = [];
          for (const dir of lease.repoDirs) {
            try {
              await this.lease!.runFenced(lease, async () => resetWorkingTree(dir));
            } catch (error) {
              cleanupErrors.push(error);
            }
          }
          if (cleanupErrors.length > 0) {
            const cleanupError = cleanupErrors.length === 1
              ? cleanupErrors[0]
              : new AggregateError(cleanupErrors, "tenant repository teardown failed");
            if (primaryError === undefined) throw cleanupError;
            if (primaryError instanceof Error && primaryError.cause === undefined) {
              primaryError.cause = cleanupError;
            }
          }
        }
      }),
    );
  }

  /** Read a committed snapshot while excluding authoring for this tenant. */
  async withTenantReadLease<T>(tenantId: string, action: () => Promise<T>): Promise<T> {
    if (!this.lease) {
      if (this.authoringMode === "disabled") return action();
      throw new Error("PostgreSQL tenant repository read lease is required while authoring is enabled");
    }
    return this.lease.withLease(tenantId, (lease) =>
      this.leaseContext.run(lease, action),
    );
  }

  /**
   * Materialize a brand-new tenant repo, or finish a prior interrupted provision.
   * A failed Git command can leave registry.json and an empty .git directory on EFS,
   * so registry.json alone is not proof that refs/heads/main exists.
   */
  private provision(dir: string): void {
    const fixture = this.opts.autoProvisionFrom!;
    const identity = this.opts.seedIdentity ?? HARNESS_IDENTITY;
    mkdirSync(dir, { recursive: true });
    trustSharedRepo(dir);
    if (repoHasMain(dir)) return;

    const existingHead = repoHead(dir);
    if (existingHead) {
      git(dir, ["update-ref", "refs/heads/main", existingHead], identity);
      git(dir, ["symbolic-ref", "HEAD", "refs/heads/main"], identity);
      return;
    }

    if (!repoExists(dir)) {
      cpSync(fixture, dir, { recursive: true });
    }
    // ensure the __pycache__ .gitignore exists (keeps the tenant mushy repo pyc-free,
    // matching the broker's sys.dont_write_bytecode discipline).
    const gitignore = join(dir, ".gitignore");
    const current = existsSync(gitignore) ? readFileSync(gitignore, "utf8") : "";
    if (!current.split(/\r?\n/).some((l) => l.trim() === "__pycache__/")) {
      writeFileSync(gitignore, current + (current && !current.endsWith("\n") ? "\n" : "") + GITIGNORE_PYCACHE, "utf8");
    }
    if (!existsSync(join(dir, ".git"))) {
      git(dir, ["init", "-q", "-b", "main"], identity);
    } else {
      git(dir, ["symbolic-ref", "HEAD", "refs/heads/main"], identity);
    }
    git(dir, ["add", "-A"], identity);
    git(
      dir,
      ["commit", "-q", "-m", "seed: provision tenant repo", `--author=${identity.name} <${identity.email}>`],
      identity,
    );
  }

  async checkout(tenantId: string): Promise<TenantRepo> {
    const lease = this.leaseContext.getStore();
    if (lease && lease.tenantId !== tenantId) {
      throw new TenantRepoLeaseLostError(tenantId);
    }
    const ref = await this.opts.locator.repoRef(tenantId);
    if (this.opts.inPlace) {
      // This also covers a partially provisioned repo left by a prior task.
      trustSharedRepo(ref);
      // ref IS the local working dir; operate on it directly (no temp clone).
      // Wave 4: auto-provision a brand-new tenant's repo from the fixture on first use.
      if (this.opts.autoProvisionFrom && !repoExists(ref)) {
        if (this.lease && lease) {
          await this.lease.runFenced(lease, async () => this.provision(ref));
        } else if (this.lease) {
          throw new TenantRepoLeaseLostError(tenantId);
        } else {
          this.provision(ref);
        }
      }
      if (this.lease && lease) {
        await this.lease.runFenced(lease, async () => resetWorkingTree(ref));
        lease.repoDirs.add(ref);
      }
      const fence =
        this.lease && lease
          ? <T>(action: () => Promise<T>) => this.lease!.runFenced(lease, action)
          : this.lease
            ? async <T>(_action: () => Promise<T>): Promise<T> => {
                throw new TenantRepoLeaseLostError(tenantId);
              }
          : undefined;
      return new GitTenantRepo(ref, fence);
    }
    const base = this.opts.workBase ?? tmpdir();
    const dir = mkdtempSync(join(base, `mushy-${tenantId}-`));
    trustSharedRepo(dir);
    if (existsSync(ref)) trustSharedRepo(ref);
    git(dir, ["clone", "--depth", "1", ref, "."]);
    if (this.lease && lease) lease.repoDirs.add(dir);
    const fence =
      this.lease && lease
        ? <T>(action: () => Promise<T>) => this.lease!.runFenced(lease, action)
        : this.lease
          ? async <T>(_action: () => Promise<T>): Promise<T> => {
              throw new TenantRepoLeaseLostError(tenantId);
            }
          : undefined;
    return new GitTenantRepo(dir, fence);
  }

  /**
   * The customization lifecycle always works through a fresh bare clone. It
   * never reuses a live checkout, which prevents a stale worktree from becoming
   * the source of a staged change or publish.
   */
  async bare(tenantId: string): Promise<TenantBareRepo> {
    const ref = await this.opts.locator.repoRef(tenantId);
    if (existsSync(ref)) trustSharedRepo(ref);
    // Refuse a stale or cross-tenant durable origin before auto-provisioning can
    // mutate the working repository. A missing origin is safe to bind now; the
    // fetch happens only after provisioning creates the source main branch.
    const durableBare = this.opts.bareBase && /^[A-Za-z0-9._-]+$/.test(tenantId)
      ? join(this.opts.bareBase, `${tenantId}.git`)
      : undefined;
    if (durableBare && existsSync(join(durableBare, "HEAD"))) {
      trustSharedRepo(durableBare);
      ensureBareOrigin(durableBare, ref);
    }
    if (this.opts.inPlace && this.opts.autoProvisionFrom && !repoHasMain(ref)) {
      this.provision(ref);
    }
    const existing = this.bareRepos.get(tenantId);
    if (existing) {
      // Once main exists, the durable bare repository is the lifecycle
      // authority. A force-fetch from the seed worktree here would rewind a
      // previously published main on the next stage request.
      trustSharedRepo(existing.dir);
      if (existing.sourceRef && existsSync(existing.sourceRef)) trustSharedRepo(existing.sourceRef);
      if (existing.sourceRef) ensureBareOrigin(existing.dir, existing.sourceRef);
      return existing;
    }
    if (!/^[A-Za-z0-9._-]+$/.test(tenantId)) {
      throw new Error("tenantId must be a single safe path component");
    }
    let dir: string;
    if (this.opts.bareBase) {
      mkdirSync(this.opts.bareBase, { recursive: true });
      dir = join(this.opts.bareBase, `${tenantId}.git`);
      trustSharedRepo(dir);
      const alreadyExists = existsSync(join(dir, "HEAD"));
      if (!alreadyExists) {
        git(this.opts.bareBase, ["clone", "--bare", ref, dir]);
      } else {
        ensureBareOrigin(dir, ref);
        // Fetch the source only to bootstrap a missing main. After that,
        // publishToMain owns the canonical branch and private refs remain local.
        if (!bareRepoHasMain(dir)) {
          git(dir, ["fetch", "--prune", "origin", "+refs/heads/*:refs/heads/*"]);
        }
        git(dir, ["symbolic-ref", "HEAD", "refs/heads/main"]);
      }
    } else {
      const base = this.opts.workBase ?? tmpdir();
      mkdirSync(base, { recursive: true });
      dir = mkdtempSync(join(base, `mushy-bare-${tenantId}-`));
      trustSharedRepo(dir);
      git(dir, ["clone", "--bare", ref, "."]);
    }
    const repo = { dir, sourceRef: ref };
    this.bareRepos.set(tenantId, repo);
    return repo;
  }
}
