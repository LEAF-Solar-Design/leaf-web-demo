// STRANGLER SHIM (mushy-code extraction, 2026-08-06): this module moved to the
// vendored mushy-code library. The path and every export stay stable for all
// in-repo importers; the implementation lives at the re-exported location and
// is synced by scripts/sync-mushy-code.py (pin: harness/src/vendor/VENDOR-PIN.json).
export * from "../../vendor/mushy-author/ports/impl/tenantRepoProvider.js";

import {
  PgTenantRepoLeaseCoordinator as VendoredPgTenantRepoLeaseCoordinator,
  TenantRepoProviderImpl as VendoredTenantRepoProviderImpl,
  assertAuthoringModeSafe,
  resolveAuthoringMode,
} from "../../vendor/mushy-author/ports/impl/tenantRepoProvider.js";
import type {
  PgTenantRepoLeaseCoordinatorOptions,
  TenantRepoProviderOptions as VendoredTenantRepoProviderOptions,
} from "../../vendor/mushy-author/ports/impl/tenantRepoProvider.js";
import type {
  ProjectRepositoryAuthority,
  ProjectRepositoryInversePreparationRequest,
  ProjectRepositoryInversePreparationResult,
  ProjectRepositorySourceVerificationRequest,
  TenantMutationFence,
  TenantRepoProvider,
  WriterLeaseWitness,
} from "../index.js";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, lstatSync, realpathSync } from "node:fs";
import { dirname, join, relative, sep } from "node:path";

const PROJECT_AUTHORITY_KEYS = [
  "organizationId",
  "projectId",
  "repoKey",
  "tenantId",
] as const;
const CANONICAL_UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const POSITIVE_GENERATION = /^[1-9][0-9]*$/;

function requireProjectRepositoryAuthority(
  value: ProjectRepositoryAuthority,
): ProjectRepositoryAuthority {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("project repository authority must be a closed object");
  }
  const keys = Object.keys(value).sort();
  if (
    keys.length !== PROJECT_AUTHORITY_KEYS.length ||
    keys.some((key, index) => key !== PROJECT_AUTHORITY_KEYS[index])
  ) {
    throw new Error("project repository authority has missing or extra fields");
  }
  for (const key of PROJECT_AUTHORITY_KEYS) {
    if (!CANONICAL_UUID.test(value[key])) {
      throw new Error(`project repository authority ${key} must be a canonical UUID`);
    }
  }
  return Object.freeze({
    tenantId: value.tenantId,
    organizationId: value.organizationId,
    projectId: value.projectId,
    repoKey: value.repoKey,
  });
}

function projectRepositoryLeaseKey(authority: ProjectRepositoryAuthority): string {
  return [
    "leaf-project-repository-v1",
    authority.tenantId,
    authority.organizationId,
    authority.projectId,
    authority.repoKey,
  ].join(":");
}

/**
 * Additive coordinator surface for one project repository. The callback witness
 * and fence both close over the exact lease object returned by one acquisition.
 */
export class PgTenantRepoLeaseCoordinator extends VendoredPgTenantRepoLeaseCoordinator {
  constructor(opts: PgTenantRepoLeaseCoordinatorOptions) {
    super(opts);
  }

  async withProjectLease<T>(
    authorityValue: ProjectRepositoryAuthority,
    action: (
      witness: WriterLeaseWitness,
      runFenced: TenantMutationFence,
    ) => Promise<T>,
  ): Promise<T> {
    const authority = requireProjectRepositoryAuthority(authorityValue);
    const leaseKey = projectRepositoryLeaseKey(authority);
    return this.withLease(leaseKey, async (lease) => {
      if (
        !CANONICAL_UUID.test(lease.ownerToken) ||
        !POSITIVE_GENERATION.test(lease.generation)
      ) {
        throw new Error("PostgreSQL project repository lease returned an invalid witness");
      }
      const witness: WriterLeaseWitness = Object.freeze({
        writerLeaseId: lease.ownerToken,
        writerLeaseGeneration: lease.generation,
      });
      const runFenced: TenantMutationFence = <R>(operation: () => R | Promise<R>) =>
        this.runFenced(lease, async () => operation());
      return action(witness, runFenced);
    });
  }
}

export interface TenantRepoProviderOptions extends VendoredTenantRepoProviderOptions {
  lease?: PgTenantRepoLeaseCoordinator | false;
}

function configuredProjectLease(
  opts: TenantRepoProviderOptions,
): PgTenantRepoLeaseCoordinator | undefined {
  if (opts.lease === false) return undefined;
  if (opts.lease) return opts.lease;
  if (
    (process.env.LEAF_HARNESS_SESSION_STORE ?? "file").trim().toLowerCase() !==
    "postgres"
  ) {
    return undefined;
  }
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
  return new PgTenantRepoLeaseCoordinator({
    poolConfig: {
      connectionString,
      max: 5,
      application_name: "leaf-platform-harness-repo-lease",
    },
  });
}

/** Stable provider plus the project-scoped lease contract owned by this app. */
export class TenantRepoProviderImpl
  extends VendoredTenantRepoProviderImpl
  implements TenantRepoProvider
{
  private readonly projectLease?: PgTenantRepoLeaseCoordinator;
  private readonly projectAuthoringMode: "disabled" | "singleton" | "fleet";
  private readonly projectBareBase?: string;

  constructor(opts: TenantRepoProviderOptions) {
    const projectLease = configuredProjectLease(opts);
    super({
      ...opts,
      lease: opts.lease === false ? false : projectLease,
    });
    this.projectLease = projectLease;
    this.projectAuthoringMode = opts.authoringMode ?? resolveAuthoringMode();
    this.projectBareBase = opts.bareBase;
  }

  async withProjectWriterLease<T>(
    authority: ProjectRepositoryAuthority,
    action: (
      witness: WriterLeaseWitness,
      runFenced: TenantMutationFence,
    ) => Promise<T>,
  ): Promise<T> {
    assertAuthoringModeSafe(this.projectAuthoringMode);
    if (!this.projectLease) {
      throw new Error("PostgreSQL project repository writer lease is required");
    }
    return this.projectLease.withProjectLease(authority, action);
  }

  private async withProjectReadLease<T>(
    authority: ProjectRepositoryAuthority,
    action: () => Promise<T>,
  ): Promise<T> {
    if (!this.projectLease) {
      throw new Error("PostgreSQL project repository read lease is required");
    }
    return this.projectLease.withProjectLease(authority, async (_witness, runFenced) =>
      runFenced(action),
    );
  }

  async verifyProjectSource(
    authorityValue: ProjectRepositoryAuthority,
    request: ProjectRepositorySourceVerificationRequest,
  ): Promise<void> {
    const authority = requireProjectRepositoryAuthority(authorityValue);
    validateSourceVerificationRequest(request);
    return this.withProjectReadLease(authority, async () => {
      const repoDir = this.containedBareRepository(authority.repoKey);
      const treeFor = (commit: string): string =>
        readBareGit(repoDir, ["rev-parse", "--verify", `${commit}^{tree}`]);
      if (request.relation === "preview") {
        if (treeFor(request.baseCommit) !== request.baseTree ||
            treeFor(request.candidateCommit) !== request.candidateTree ||
            !bareGitSucceeds(repoDir, ["merge-base", "--is-ancestor", request.baseCommit, request.candidateCommit])) {
          throw new Error("project repository source witness does not verify");
        }
        return;
      }
      if (
        treeFor(request.originalCommit) !== request.originalTree ||
        treeFor(request.targetCommit) !== request.targetTree ||
        treeFor(request.inverseCommit) !== request.inverseTree ||
        request.targetCommit !== request.originalCommit ||
        readBareGit(repoDir, ["rev-parse", "--verify", `${request.inverseCommit}^1`]) !== request.targetCommit ||
        request.inverseTree !== readBareGit(repoDir, ["rev-parse", "--verify", `${request.originalCommit}^1^{tree}`])
      ) {
        throw new Error("project repository source witness does not verify");
      }
    });
  }

  async prepareProjectInverse(
    authorityValue: ProjectRepositoryAuthority,
    request: ProjectRepositoryInversePreparationRequest,
  ): Promise<ProjectRepositoryInversePreparationResult> {
    const authority = requireProjectRepositoryAuthority(authorityValue);
    validateInversePreparationRequest(request);
    if (!this.withProjectWriterLease) {
      throw new Error("project repository writer lease is unavailable");
    }
    return this.withProjectWriterLease(authority, async (witness, runFenced) =>
      runFenced(() => {
        const repoDir = this.containedBareRepository(authority.repoKey);
        const treeFor = (commit: string): string =>
          readBareGit(repoDir, ["rev-parse", "--verify", `${commit}^{tree}`]);
        if (
          request.targetCommit !== request.originalCommit ||
          treeFor(request.originalCommit) !== request.originalTree ||
          treeFor(request.targetCommit) !== request.targetTree
        ) {
          throw new Error("project repository inverse source does not verify");
        }
        const inverseTree = readBareGit(
          repoDir,
          ["rev-parse", "--verify", `${request.originalCommit}^1^{tree}`],
        );
        const privateRef = `refs/leaf/annotation-inverses/${request.sourceBatchId}`;
        let inverseCommit: string;
        if (bareGitSucceeds(repoDir, ["show-ref", "--verify", "--quiet", privateRef])) {
          inverseCommit = readBareGit(repoDir, ["rev-parse", "--verify", `${privateRef}^{commit}`]);
        } else {
          inverseCommit = writeBareGit(
            repoDir,
            ["commit-tree", inverseTree, "-p", request.targetCommit],
            `Leaf annotation inverse ${request.sourceBatchId}\n`,
          );
          writeBareGit(
            repoDir,
            ["update-ref", privateRef, inverseCommit, "0000000000000000000000000000000000000000"],
          );
        }
        if (
          readBareGit(repoDir, ["rev-parse", "--verify", `${inverseCommit}^1`]) !== request.targetCommit ||
          treeFor(inverseCommit) !== inverseTree
        ) {
          throw new Error("project repository inverse ref does not verify");
        }
        const payloadDigest = createHash("sha256").update(canonicalJson({
          inverse_commit: inverseCommit,
          inverse_tree: inverseTree,
          kind: "annotation_inverse_v1",
          original_commit: request.originalCommit,
          original_tree: request.originalTree,
          source_batch_id: request.sourceBatchId,
          target_commit: request.targetCommit,
          target_tree: request.targetTree,
        })).digest("hex");
        return Object.freeze({
          inverseCommit,
          inverseTree,
          payloadDigest,
          writerLeaseId: witness.writerLeaseId,
          writerLeaseGeneration: witness.writerLeaseGeneration,
        });
      }),
    );
  }

  private containedBareRepository(repoKey: string): string {
    if (!this.projectBareBase || !existsSync(this.projectBareBase) || lstatSync(this.projectBareBase).isSymbolicLink()) {
      throw new Error("configured project repository base is unavailable");
    }
    const base = realpathSync(this.projectBareBase);
    const candidate = join(base, `${repoKey}.git`);
    if (!existsSync(candidate) || lstatSync(candidate).isSymbolicLink()) {
      throw new Error("project repository is unavailable");
    }
    const resolved = realpathSync(candidate);
    if (dirname(resolved) !== base || relative(base, resolved).startsWith(`..${sep}`) || relative(base, resolved) === "") {
      throw new Error("project repository containment failed");
    }
    if (!existsSync(join(resolved, "HEAD")) || readBareGit(resolved, ["rev-parse", "--is-bare-repository"]) !== "true") {
      throw new Error("project repository is not a bare repository");
    }
    return resolved;
  }
}

const SHA40 = /^[a-f0-9]{40}$/;

function validateInversePreparationRequest(
  request: ProjectRepositoryInversePreparationRequest,
): void {
  if (!request || typeof request !== "object" || Array.isArray(request)) {
    throw new Error("project repository inverse request must be a closed object");
  }
  const expected = ["originalCommit", "originalTree", "sourceBatchId", "targetCommit", "targetTree"];
  const actual = Object.keys(request).sort();
  if (actual.length !== expected.length || actual.some((field, index) => field !== expected[index])) {
    throw new Error("project repository inverse request has missing or extra fields");
  }
  if (!CANONICAL_UUID.test(request.sourceBatchId)) {
    throw new Error("project repository inverse source batch must be a canonical UUID");
  }
  for (const value of [request.originalCommit, request.originalTree, request.targetCommit, request.targetTree]) {
    if (!SHA40.test(value)) {
      throw new Error("project repository inverse source witness is invalid");
    }
  }
}

function validateSourceVerificationRequest(request: ProjectRepositorySourceVerificationRequest): void {
  if (!request || typeof request !== "object" || Array.isArray(request)) {
    throw new Error("project repository source request must be a closed object");
  }
  const fields = request.relation === "preview"
    ? ["baseCommit", "baseTree", "candidateCommit", "candidateTree", "relation"]
    : ["inverseCommit", "inverseTree", "originalCommit", "originalTree", "relation", "targetCommit", "targetTree"];
  const actual = Object.keys(request).sort();
  if (actual.length !== fields.length || actual.some((field, index) => field !== fields[index])) {
    throw new Error("project repository source request has missing or extra fields");
  }
  if (request.relation !== "preview" && request.relation !== "inverse") {
    throw new Error("project repository source relation is invalid");
  }
  for (const [key, value] of Object.entries(request)) {
    if (key !== "relation" && (typeof value !== "string" || !SHA40.test(value))) {
      throw new Error("project repository source witness is invalid");
    }
  }
}

function readBareGit(repoDir: string, args: string[]): string {
  try {
    return execFileSync("git", ["--git-dir", repoDir, "--no-optional-locks", ...args], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
  } catch {
    throw new Error("project repository Git verification failed");
  }
}

function bareGitSucceeds(repoDir: string, args: string[]): boolean {
  try {
    execFileSync("git", ["--git-dir", repoDir, "--no-optional-locks", ...args], {
      stdio: "ignore",
    });
    return true;
  } catch {
    return false;
  }
}

function writeBareGit(repoDir: string, args: string[], input?: string): string {
  try {
    return execFileSync("git", ["--git-dir", repoDir, "--no-optional-locks", ...args], {
      encoding: "utf8",
      input,
      env: {
        ...process.env,
        GIT_AUTHOR_NAME: "Leaf Annotation Service",
        GIT_AUTHOR_EMAIL: "annotations@leaf.invalid",
        GIT_AUTHOR_DATE: "2000-01-01T00:00:00Z",
        GIT_COMMITTER_NAME: "Leaf Annotation Service",
        GIT_COMMITTER_EMAIL: "annotations@leaf.invalid",
        GIT_COMMITTER_DATE: "2000-01-01T00:00:00Z",
      },
      stdio: [input === undefined ? "ignore" : "pipe", "pipe", "ignore"],
    }).trim();
  } catch {
    throw new Error("project repository Git mutation failed");
  }
}

function canonicalJson(value: Record<string, string>): string {
  return `{${Object.keys(value).sort().map((key) =>
    `${JSON.stringify(key)}:${JSON.stringify(value[key])}`).join(",")}}`;
}
