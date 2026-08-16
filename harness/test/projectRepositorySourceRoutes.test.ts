import { execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createHarness } from "../src/server.js";
import type {
  HarnessPorts,
  ProjectRepositoryAuthority,
  ProjectRepositorySourceVerificationRequest,
} from "../src/ports/index.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import {
  PgTenantRepoLeaseCoordinator,
  TenantRepoProviderImpl,
} from "../src/ports/impl/tenantRepoProvider.js";

const SECRET = "source-witness-test-secret";
const AUTHORITY: ProjectRepositoryAuthority = Object.freeze({
  tenantId: "11111111-1111-4111-8111-111111111111",
  organizationId: "22222222-2222-4222-8222-222222222222",
  projectId: "33333333-3333-4333-8333-333333333333",
  repoKey: "44444444-4444-4444-8444-444444444444",
});

function git(dir: string, args: string[]): string {
  return execFileSync("git", args, { cwd: dir, encoding: "utf8" }).trim();
}

function projectLease(): PgTenantRepoLeaseCoordinator {
  return {
    async withProjectLease<T>(
      _authority: ProjectRepositoryAuthority,
      action: (witness: unknown, runFenced: <R>(operation: () => R | Promise<R>) => Promise<R>) => Promise<T>,
    ): Promise<T> {
      return action(Object.freeze({}), async <R>(operation: () => R | Promise<R>) => operation());
    },
  } as unknown as PgTenantRepoLeaseCoordinator;
}

function body(base: string, candidate: string, baseTree: string, candidateTree: string): Record<string, string> {
  return {
    tenant_id: AUTHORITY.tenantId,
    organization_id: AUTHORITY.organizationId,
    project_id: AUTHORITY.projectId,
    repo_key: AUTHORITY.repoKey,
    relation: "preview",
    base_commit: base,
    base_tree: baseTree,
    candidate_commit: candidate,
    candidate_tree: candidateTree,
  };
}

function previewRequest(
  base: string,
  candidate: string,
  baseTree: string,
  candidateTree: string,
): ProjectRepositorySourceVerificationRequest {
  return Object.freeze({
    relation: "preview",
    baseCommit: base,
    baseTree,
    candidateCommit: candidate,
    candidateTree,
  });
}

describe("project repository source witness route", () => {
  let root: string;
  let baseUrl: string;
  let close: () => void;
  let source: string;
  let candidate: string;
  let sourceTree: string;
  let candidateTree: string;
  let inverse: string;
  let inverseTree: string;

  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), "leaf-project-source-"));
    const work = join(root, "work");
    const bareBase = join(root, "bare");
    const bare = join(bareBase, `${AUTHORITY.repoKey}.git`);
    execFileSync("git", ["init", "-q", "-b", "main", work]);
    writeFileSync(join(work, "a.txt"), "before\n", "utf8");
    git(work, ["add", "."]);
    git(work, ["-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "base"]);
    source = git(work, ["rev-parse", "HEAD"]);
    sourceTree = git(work, ["rev-parse", "HEAD^{tree}"]);
    writeFileSync(join(work, "a.txt"), "after\n", "utf8");
    git(work, ["add", "."]);
    git(work, ["-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "candidate"]);
    candidate = git(work, ["rev-parse", "HEAD"]);
    candidateTree = git(work, ["rev-parse", "HEAD^{tree}"]);
    git(work, ["-c", "user.name=test", "-c", "user.email=test@example.com", "revert", "--no-edit", "HEAD"]);
    inverse = git(work, ["rev-parse", "HEAD"]);
    inverseTree = git(work, ["rev-parse", "HEAD^{tree}"]);
    execFileSync("git", ["clone", "-q", "--bare", work, bare]);

    const tenantRepo = new TenantRepoProviderImpl({
      locator: { async repoRef() { return work; } },
      bareBase,
      lease: projectLease(),
      authoringMode: "disabled",
    });
    const ports: HarnessPorts = {
      oauth: new FakeOAuthGrantProvider(),
      tenantRepo,
      broker: new FakeBrokerApsClient(),
      agentRunner: new FakeAgentRunner(),
    };
    const server = createHarness(ports, { auth: { enabled: true, secret: SECRET } }).listen(0);
    baseUrl = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
    close = () => server.close();
  });

  afterEach(() => {
    close();
    rmSync(root, { recursive: true, force: true });
  });

  it("authenticates before parsing, then verifies an ancestor preview against the contained bare repository", async () => {
    const provider = (await import("../src/ports/impl/tenantRepoProvider.js"));
    const spy = vi.spyOn(provider.TenantRepoProviderImpl.prototype, "verifyProjectSource");
    const sourceBody = body(source, candidate, sourceTree, candidateTree);
    const request = JSON.stringify(sourceBody);
    const denied = await fetch(`${baseUrl}/internal/project-repository-source/verify`, {
      method: "POST", headers: { "content-type": "application/json" }, body: "not-json",
    });
    expect(denied.status).toBe(401);
    expect(spy).not.toHaveBeenCalled();

    const accepted = await fetch(`${baseUrl}/internal/project-repository-source/verify`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-harness-secret": SECRET, "x-tenant-id": AUTHORITY.tenantId },
      body: request,
    });
    expect(accepted.status).toBe(200);
    const response = await accepted.json() as { contract: string; request_digest: string };
    expect(response).toMatchObject({
      contract: "leaf.project-repository-source-witness.v1",
      request_digest: expect.stringMatching(/^[a-f0-9]{64}$/),
    });
    const canonical = `{${Object.keys(sourceBody).sort().map((key) =>
      `${JSON.stringify(key)}:${JSON.stringify(sourceBody[key])}`).join(",")}}`;
    const expectedDigest = (await import("node:crypto")).createHash("sha256").update(canonical).digest("hex");
    expect(response.request_digest).toBe(expectedDigest);
    expect(spy).toHaveBeenCalledTimes(1);
    spy.mockRestore();
  });

  it("fails closed for extra fields, tenant mismatch, and non-ancestor commits", async () => {
    const extra = { ...body(source, candidate, sourceTree, candidateTree), root: "C:/forbidden" };
    const invalid = await fetch(`${baseUrl}/internal/project-repository-source/verify`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-harness-secret": SECRET, "x-tenant-id": AUTHORITY.tenantId },
      body: JSON.stringify(extra),
    });
    expect(invalid.status).toBe(409);
    expect(await invalid.text()).not.toContain("forbidden");

    const mismatch = await fetch(`${baseUrl}/internal/project-repository-source/verify`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-harness-secret": SECRET, "x-tenant-id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" },
      body: JSON.stringify(body(source, candidate, sourceTree, candidateTree)),
    });
    expect(mismatch.status).toBe(409);

    const falseAncestry = await fetch(`${baseUrl}/internal/project-repository-source/verify`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-harness-secret": SECRET, "x-tenant-id": AUTHORITY.tenantId },
      body: JSON.stringify(body(candidate, source, candidateTree, sourceTree)),
    });
    expect(falseAncestry.status).toBe(409);
  });

  it("verifies an inverse only when it parents the named original and restores its parent tree", async () => {
    const inverseBody = {
      tenant_id: AUTHORITY.tenantId,
      organization_id: AUTHORITY.organizationId,
      project_id: AUTHORITY.projectId,
      repo_key: AUTHORITY.repoKey,
      relation: "inverse",
      original_commit: candidate,
      original_tree: candidateTree,
      target_commit: candidate,
      target_tree: candidateTree,
      inverse_commit: inverse,
      inverse_tree: inverseTree,
    };
    const accepted = await fetch(`${baseUrl}/internal/project-repository-source/verify`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-harness-secret": SECRET, "x-tenant-id": AUTHORITY.tenantId },
      body: JSON.stringify(inverseBody),
    });
    expect(accepted.status).toBe(200);

    const forged = { ...inverseBody, target_commit: source, target_tree: sourceTree };
    const rejected = await fetch(`${baseUrl}/internal/project-repository-source/verify`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-harness-secret": SECRET, "x-tenant-id": AUTHORITY.tenantId },
      body: JSON.stringify(forged),
    });
    expect(rejected.status).toBe(409);
  });

  it("fails closed before Git verification for a missing base, non-bare repo, or symlink escape", async () => {
    const request = previewRequest(source, candidate, sourceTree, candidateTree);
    const options = (bareBase: string) => ({
      locator: { async repoRef() { return join(root, "work"); } },
      bareBase,
      lease: projectLease(),
      authoringMode: "disabled" as const,
    });
    const missing = new TenantRepoProviderImpl(options(join(root, "missing-base")));
    await expect(missing.verifyProjectSource(AUTHORITY, request)).rejects.toThrow(/base is unavailable/);

    const nonBareBase = join(root, "non-bare-base");
    mkdirSync(nonBareBase);
    execFileSync("git", ["init", "-q", join(nonBareBase, `${AUTHORITY.repoKey}.git`)]);
    const nonBare = new TenantRepoProviderImpl(options(nonBareBase));
    await expect(nonBare.verifyProjectSource(AUTHORITY, request)).rejects.toThrow(/not a bare repository/);

    const escapeBase = join(root, "escape-base");
    mkdirSync(escapeBase);
    symlinkSync(join(root, "bare", `${AUTHORITY.repoKey}.git`), join(escapeBase, `${AUTHORITY.repoKey}.git`), "junction");
    const escaped = new TenantRepoProviderImpl(options(escapeBase));
    await expect(escaped.verifyProjectSource(AUTHORITY, request)).rejects.toThrow(/repository is unavailable/);
  });
});
