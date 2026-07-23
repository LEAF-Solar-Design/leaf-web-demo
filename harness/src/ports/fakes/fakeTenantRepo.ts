/**
 * Fake TenantRepoProvider - materializes a REAL git working copy from a pristine
 * fixture, so the acceptance test can assert on real `git log` output. The
 * checked-in fixture is never mutated: each checkout copies it into a fresh temp
 * dir, `git init`s it, and seeds ONE baseline commit under a DISTINCT identity
 * (so the harness's own commit is the only one authored by the harness identity).
 *
 * The provider records every checkout so a test can inspect the working dir after
 * a POST (see `lastCheckout`).
 */

import { execFileSync } from "node:child_process";
import { cpSync, mkdirSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import type { HarnessIdentity, TenantBareRepo, TenantRepo, TenantRepoProvider } from "../index.js";

const SEED_IDENTITY: HarnessIdentity = {
  name: "fixture-seed",
  email: "seed@fixture.local",
};

function git(cwd: string, args: string[], identity?: HarnessIdentity): string {
  const base = [
    "-c",
    "core.autocrlf=false",
    "-c",
    `user.name=${identity?.name ?? SEED_IDENTITY.name}`,
    "-c",
    `user.email=${identity?.email ?? SEED_IDENTITY.email}`,
  ];
  return execFileSync("git", [...base, ...args], {
    cwd,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
}

class FakeTenantRepo implements TenantRepo {
  constructor(readonly dir: string) {}

  async commit(message: string, identity: HarnessIdentity): Promise<{ commit: string }> {
    git(this.dir, ["add", "-A"], identity);
    git(this.dir, ["commit", "-m", message, `--author=${identity.name} <${identity.email}>`], identity);
    const commit = git(this.dir, ["rev-parse", "HEAD"]).trim();
    return { commit };
  }
}

export class FakeTenantRepoProvider implements TenantRepoProvider {
  /** The most recent checkout (handy for post-POST assertions in tests). */
  lastCheckout: FakeTenantRepo | null = null;
  readonly checkouts: FakeTenantRepo[] = [];
  private readonly bareRepos = new Map<string, TenantBareRepo>();

  constructor(private readonly fixtureDir: string) {
    this.fixtureDir = resolve(fixtureDir);
  }

  async checkout(tenantId: string): Promise<TenantRepo> {
    const dir = mkdtempSync(join(tmpdir(), `leaf-tenant-${tenantId}-`));
    cpSync(this.fixtureDir, dir, { recursive: true });
    git(dir, ["init", "-q"]);
    git(dir, ["add", "-A"]);
    git(dir, ["commit", "-q", "-m", "seed: pristine tenant fixture", `--author=${SEED_IDENTITY.name} <${SEED_IDENTITY.email}>`]);
    const repo = new FakeTenantRepo(dir);
    this.lastCheckout = repo;
    this.checkouts.push(repo);
    return repo;
  }

  /** Materialize one real bare repository per tenant for lifecycle tests. */
  async bare(tenantId: string): Promise<TenantBareRepo> {
    const existing = this.bareRepos.get(tenantId);
    if (existing) return existing;

    const root = mkdtempSync(join(tmpdir(), `leaf-tenant-bare-${tenantId}-`));
    const seed = join(root, "seed");
    const dir = join(root, "tenant.git");
    mkdirSync(seed, { recursive: true });
    cpSync(this.fixtureDir, seed, { recursive: true });
    git(seed, ["init", "-q"]);
    git(seed, ["add", "-A"]);
    git(seed, ["commit", "-q", "-m", "seed: pristine tenant fixture"]);
    git(root, ["init", "--bare", dir]);
    git(seed, ["remote", "add", "origin", dir]);
    git(seed, ["push", "origin", "HEAD:refs/heads/main"]);
    const repo = { dir };
    this.bareRepos.set(tenantId, repo);
    return repo;
  }
}
