/**
 * Adapter CONFORMANCE suite — the reusable contract assertions a CONSUMER runs
 * against its REAL port implementations, so a product that vendors mushy-author
 * proves its adapters uphold the security-critical guarantees before it cuts over
 * from a forked copy. Framework-agnostic: each assertion is a plain async
 * function that throws on the first violation, so a consumer wraps it in one
 * `it(...)` / `test(...)` of whatever runner it uses.
 *
 * Scope of this first increment: the TenantRepoProvider contract — the tenant
 * isolation and writer-lease boundary. The remaining ports (OAuthGrantProvider,
 * BrokerApsClient, AgentRunner) follow the same shape and land next.
 */
import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { join } from "node:path";
import type { TenantRepoProvider, HarnessIdentity } from "../ports/index.js";

const git = (dir: string, args: string[]): string =>
  execFileSync("git", ["-C", dir, ...args], { encoding: "utf8" }).trim();

/**
 * Core TenantRepoProvider contract every implementation MUST uphold regardless of
 * backing store:
 *  1. checkout(tenant) yields a usable git working copy rooted at `.dir`;
 *  2. commit() stages the change into exactly one commit authored by the harness
 *     identity and returns the new HEAD;
 *  3. isolation — a second tenant's checkout is a distinct repository that cannot
 *     see the first tenant's commit.
 * Throws on the first violation.
 */
export async function assertTenantRepoProviderConformance(
  makeProvider: () => TenantRepoProvider | Promise<TenantRepoProvider>,
  identity: HarnessIdentity,
): Promise<void> {
  const provider = await makeProvider();

  const a = await provider.checkout("conformance-tenant-a");
  assert.ok(a.dir, "checkout().dir must be a path");
  // `--is-inside-work-tree` prints "false" (and still exits 0) for a BARE repo, so
  // check the value, not just that the command succeeded.
  assert.equal(
    git(a.dir, ["rev-parse", "--is-inside-work-tree"]),
    "true",
    "checkout().dir must be a non-bare git work tree",
  );

  const head0 = git(a.dir, ["rev-parse", "HEAD"]);
  writeFileSync(join(a.dir, "conformance-probe.txt"), "conformance-probe");
  const { commit } = await a.commit("conformance probe", identity);
  assert.notEqual(commit, head0, "commit() must advance HEAD");
  assert.equal(git(a.dir, ["rev-parse", "HEAD"]), commit, "commit() must return the new HEAD");
  // Exactly ONE commit built ON TOP of the prior HEAD: the new commit's parents
  // must be precisely [head0]. This rejects several commits (parent would be an
  // intermediate, not head0), a merge (two parents), and a history-replacing
  // orphan (no parent / a different parent) — all of which a `rev-list --count`
  // range check would miss.
  const parents = git(a.dir, ["rev-list", "--parents", "-n", "1", commit])
    .split(/\s+/).filter(Boolean).slice(1);
  assert.deepEqual(
    parents,
    [head0],
    "commit() must create exactly ONE commit whose sole parent is the prior HEAD (no extra commits, no merge, no history replace)",
  );
  assert.equal(
    git(a.dir, ["log", "-1", "--format=%an <%ae>"]),
    `${identity.name} <${identity.email}>`,
    "the new commit must be authored by the harness identity",
  );
  const committedPaths = git(a.dir, ["diff-tree", "--no-commit-id", "--name-only", "-r", commit])
    .split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  assert.ok(
    committedPaths.includes("conformance-probe.txt"),
    "commit() must include the EXACT staged change (stage-all-then-one-commit)",
  );

  const b = await provider.checkout("conformance-tenant-b");
  assert.notEqual(b.dir, a.dir, "distinct tenants must check out to distinct working dirs");
  let bHasA = true;
  try { git(b.dir, ["cat-file", "-e", commit]); } catch { bHasA = false; }
  assert.equal(bHasA, false, "isolation breach: tenant B's repo must NOT contain tenant A's commit");
}

/**
 * Writer-lease exclusion: if the provider offers `withTenantLease`, two
 * overlapping leases for the SAME tenant must NOT run their fenced bodies
 * concurrently. A pass-through/no-op lease (a common shortcut in a test double)
 * fails this by design — that is the point: a production adapter must serialize.
 * Throws if the provider does not offer a lease, or if it fails to serialize.
 */
export async function assertTenantWriterLeaseExclusion(
  makeProvider: () => TenantRepoProvider | Promise<TenantRepoProvider>,
): Promise<void> {
  const provider = await makeProvider();
  assert.ok(
    typeof provider.withTenantLease === "function",
    "provider does not offer withTenantLease; the exclusion contract does not apply",
  );
  let inside = 0;
  let maxConcurrent = 0;
  const body = async (): Promise<void> => {
    inside += 1;
    maxConcurrent = Math.max(maxConcurrent, inside);
    await new Promise((r) => setTimeout(r, 25));
    inside -= 1;
  };
  await Promise.all([
    provider.withTenantLease!("conformance-lease-tenant", async () => body()),
    provider.withTenantLease!("conformance-lease-tenant", async () => body()),
  ]);
  assert.equal(
    maxConcurrent,
    1,
    "withTenantLease must serialize writers for the same tenant (observed concurrent fenced bodies)",
  );
}
