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
  ProjectRepositorySourceBundleRequest,
  ProjectRepositorySourceBundleResult,
  ProjectRepositorySourceInitializationRequest,
  ProjectRepositorySourceInitializationResult,
  ProjectRepositoryInversePreparationRequest,
  ProjectRepositoryInversePreparationResult,
  ProjectRepositorySourceVerificationRequest,
  TenantMutationFence,
  TenantRepoProvider,
  WriterLeaseWitness,
} from "../index.js";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, lstatSync, realpathSync, mkdirSync, readdirSync, readFileSync, writeFileSync, openSync, fsyncSync, closeSync } from "node:fs";
import { dirname, join, relative, sep } from "node:path";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { ProjectRepositorySourceConflict, ProjectRepositorySourceUnavailable } from "../index.js";
import { TenantChangeRepo } from "../../vendor/mushy-author/ports/impl/tenantChangeRepo.js";
import { HARNESS_IDENTITY } from "../../vendor/mushy-author/registry/registerTool.js";

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
  private readonly projectWorkBase?: string;

  constructor(opts: TenantRepoProviderOptions) {
    const projectLease = configuredProjectLease(opts);
    super({
      ...opts,
      lease: opts.lease === false ? false : projectLease,
    });
    this.projectLease = projectLease;
    this.projectAuthoringMode = opts.authoringMode ?? resolveAuthoringMode();
    this.projectBareBase = opts.bareBase;
    this.projectWorkBase = opts.workBase;
  }

  projectChangeRepo(authorityValue: ProjectRepositoryAuthority): TenantChangeRepo {
    const authority = requireProjectRepositoryAuthority(authorityValue);
    const repoDir = this.containedBareRepository(authority.repoKey);
    checkSourceContents(repoDir);
    const marker = canonicalJson({ contract: "leaf.project-repository-source-initializer.v1",
      tenant_id: authority.tenantId, organization_id: authority.organizationId,
      project_id: authority.projectId, repo_key: authority.repoKey });
    if (readFileSync(join(repoDir, ".leaf-source-owner.json"), "utf8") !== marker) {
      throw new ProjectRepositorySourceConflict("project source conflicts");
    }
    return new TenantChangeRepo({ repoDir, identity: HARNESS_IDENTITY,
      ...(this.projectWorkBase ? { workBase: this.projectWorkBase } : {}) });
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
    action: (witness: WriterLeaseWitness) => Promise<T>,
  ): Promise<T> {
    if (!this.projectLease) {
      throw new Error("PostgreSQL project repository read lease is required");
    }
    return this.projectLease.withProjectLease(authority, async (witness, runFenced) =>
      runFenced(() => action(witness)),
    );
  }

  async initializeProjectSource(
    authorityValue: ProjectRepositoryAuthority,
    request: ProjectRepositorySourceInitializationRequest,
  ): Promise<ProjectRepositorySourceInitializationResult> {
    const authority = requireProjectRepositoryAuthority(authorityValue);
    if (!request || Object.keys(request).sort().join(",") !== "seedDigest,seedDocument" ||
        typeof request.seedDocument !== "string" || !request.seedDocument.length ||
        [...request.seedDocument].length > 32768 || Buffer.byteLength(request.seedDocument, "utf8") > 131072 ||
        request.seedDocument.includes("\0") || Buffer.from(request.seedDocument, "utf8").toString("utf8") !== request.seedDocument ||
        typeof request.seedDigest !== "string" || !/^[a-f0-9]{64}$/.test(request.seedDigest) ||
        createHash("sha256").update(request.seedDocument, "utf8").digest("hex") !== request.seedDigest) {
      throw new Error("invalid project source seed");
    }
    request = Object.freeze({ seedDocument: request.seedDocument, seedDigest: request.seedDigest });
    const identity = {
      contract: "leaf.project-repository-source-initializer.v1",
      tenant_id: authority.tenantId, organization_id: authority.organizationId,
      project_id: authority.projectId, repo_key: authority.repoKey,
    };
    const marker = canonicalJson(identity);
    return this.withProjectWriterLease(authority, async (witness, runFenced) => runFenced(() => {
      try {
      const candidate = (): string => {
        if (!this.projectBareBase || !existsSync(this.projectBareBase) ||
            lstatSync(this.projectBareBase).isSymbolicLink() || !lstatSync(this.projectBareBase).isDirectory()) {
          throw new ProjectRepositorySourceUnavailable("project repository base unavailable");
        }
        const base = realpathSync(this.projectBareBase);
        const path = join(base, `${authority.repoKey}.git`);
        if (dirname(path) !== base) throw new Error("project repository containment failed");
        // lstat also catches dangling links, which existsSync deliberately does not.
        if (readdirSync(base).includes(`${authority.repoKey}.git`)) {
          if (lstatSync(path).isSymbolicLink() || !lstatSync(path).isDirectory() || realpathSync(path) !== path) {
            throw new Error("project repository containment failed");
          }
        }
        return path;
      };
      const repoDir = candidate();
      if (!existsSync(repoDir)) mkdirSync(candidate());
      const markerName = ".leaf-source-owner.json";
      const checkContents = checkSourceContents;
      if (!readdirSync(candidate()).includes(markerName)) {
        if (readdirSync(candidate()).length) throw new Error("project repository is unowned");
        const fd = openSync(join(candidate(), markerName), "wx");
        try {
          writeFileSync(fd, marker, "utf8");
          fsyncSync(fd);
        } finally {
          closeSync(fd);
        }
      }
      checkContents(candidate());
      if (readFileSync(join(candidate(), markerName), "utf8") !== marker) {
        throw new Error("project repository ownership conflicts");
      }
      const git = sourceGit(candidate);
      if (["HEAD", "config", "objects", "refs"].some((name) => !existsSync(join(candidate(), name)))) {
        git(["init", "--bare", "--object-format=sha1", "--initial-branch=main", "--template=", candidate()]);
      }
      if (git(["rev-parse", "--is-bare-repository"]).trim() !== "true" ||
          git(["rev-parse", "--show-object-format"]).trim() !== "sha1" ||
          git(["symbolic-ref", "HEAD"]).trim() !== "refs/heads/main") {
        throw new Error("project repository is noncanonical");
      }
      const refs = git(["for-each-ref", "--format=%(refname)"]).trim().split("\n").filter(Boolean);
      const replayed = refs.includes("refs/heads/main");
      if (!replayed) {
        if (refs.length) throw new Error("project repository is noncanonical");
        const blob = (text: string): string => git(["hash-object", "-w", "--stdin"], text).trim();
        const prompt = blob(request.seedDocument);
        const seed = blob(canonicalJson({ ...identity, seed_digest: request.seedDigest }));
        const leaf = git(["mktree"], `100644 blob ${seed}\tsource-seed.json\n`).trim();
        const tree = git(["mktree"], `040000 tree ${leaf}\t.leaf\n100644 blob ${prompt}\tPROMPT.md\n`).trim();
        const commit = git(["commit-tree", tree], "Leaf project source seed\n").trim();
        git(["update-ref", "refs/heads/main", commit, "0000000000000000000000000000000000000000"]);
      }
      const sourceCommit = git(["rev-parse", "--verify", "refs/heads/main^{commit}"]).trim();
      const sourceTree = git(["rev-parse", "--verify", `${sourceCommit}^{tree}`]).trim();
      const seedText = git(["show", `${sourceCommit}:.leaf/source-seed.json`]);
      const seed: unknown = JSON.parse(seedText);
      if (!seed || typeof seed !== "object" || Array.isArray(seed)) throw new Error("invalid source metadata");
      const seedDigest = (seed as Record<string, unknown>).seed_digest;
      if (typeof seedDigest !== "string" || !/^[a-f0-9]{64}$/.test(seedDigest) ||
          seedText !== canonicalJson({ ...identity, seed_digest: seedDigest }) ||
          createHash("sha256").update(git(["show", `${sourceCommit}:PROMPT.md`]), "utf8").digest("hex") !== seedDigest ||
          !SHA40.test(sourceCommit) || !SHA40.test(sourceTree)) {
        throw new Error("project source metadata conflicts");
      }
      for (const path of ["PROMPT.md", ".leaf/source-seed.json"]) {
        if (!git(["ls-tree", sourceCommit, "--", path]).startsWith("100644 blob ")) {
          throw new Error("project source is noncanonical");
        }
      }
      return Object.freeze({ sourceCommit, sourceTree, seedDigest, replayed, ...witness });
      } catch (error) {
        if (error instanceof ProjectRepositorySourceUnavailable) throw error;
        throw new ProjectRepositorySourceConflict("project source conflicts");
      }
    })).catch((error: unknown) => {
      if (error instanceof ProjectRepositorySourceConflict) throw error;
      throw new ProjectRepositorySourceUnavailable("project source is unavailable");
    });
  }

  async exportProjectSourceBundle(
    authorityValue: ProjectRepositoryAuthority,
    request: ProjectRepositorySourceBundleRequest,
  ): Promise<ProjectRepositorySourceBundleResult> {
    const authority = requireProjectRepositoryAuthority(authorityValue);
    if (!request || Object.keys(request).sort().join(",") !== "sourceCommit,sourceTree" ||
        typeof request.sourceCommit !== "string" || !SHA40.test(request.sourceCommit) ||
        typeof request.sourceTree !== "string" || !SHA40.test(request.sourceTree)) {
      throw new ProjectRepositorySourceConflict("project source conflicts");
    }
    const { sourceCommit, sourceTree } = request;
    return this.withProjectReadLease(authority, async (witness) => {
      let temporary: string | undefined;
      try {
        const candidate = (): string => {
          if (!this.projectBareBase || !existsSync(this.projectBareBase) ||
              lstatSync(this.projectBareBase).isSymbolicLink() || !lstatSync(this.projectBareBase).isDirectory()) {
            throw new ProjectRepositorySourceUnavailable("project source is unavailable");
          }
          const base = realpathSync(this.projectBareBase);
          const path = join(base, `${authority.repoKey}.git`);
          if (dirname(path) !== base || !existsSync(path) || lstatSync(path).isSymbolicLink() ||
              !lstatSync(path).isDirectory() || realpathSync(path) !== path) throw new Error();
          checkSourceContents(path);
          if (["HEAD", "config", "objects", "refs"].some(name => !existsSync(join(path, name)))) throw new Error();
          const marker = canonicalJson({ contract: "leaf.project-repository-source-initializer.v1",
            tenant_id: authority.tenantId, organization_id: authority.organizationId,
            project_id: authority.projectId, repo_key: authority.repoKey });
          if (readFileSync(join(path, ".leaf-source-owner.json"), "utf8") !== marker) throw new Error();
          return path;
        };
        const git = sourceGit(candidate);
        if (git(["rev-parse", "--is-bare-repository"]).trim() !== "true" ||
            git(["rev-parse", "--show-object-format"]).trim() !== "sha1" ||
            git(["symbolic-ref", "HEAD"]).trim() !== "refs/heads/main" ||
            git(["rev-parse", "--verify", "refs/heads/main^{commit}"]).trim() !== sourceCommit ||
            git(["rev-parse", "--verify", `${sourceCommit}^{tree}`]).trim() !== sourceTree) throw new Error();
        // Current main is the initial producer boundary. No source refs are written.
        temporary = mkdtempSync(join(tmpdir(), "leaf-source-bundle-"));
        const path = join(temporary, "export.bundle");
        git(["bundle", "create", path, "refs/heads/main"]);
        git(["bundle", "verify", path]);
        const stat = lstatSync(path);
        if (!stat.isFile() || stat.isSymbolicLink() || stat.size < 1 || stat.size > 67108864) throw new Error();
        const bundle = readFileSync(path);
        if (bundle.length !== stat.size) throw new Error();
        return Object.freeze({ sourceCommit, sourceTree, bundle,
          bundleSha256: createHash("sha256").update(bundle).digest("hex"), sizeBytes: bundle.length,
          leaseId: witness.writerLeaseId, leaseGeneration: witness.writerLeaseGeneration });
      } catch (error) {
        if (error instanceof ProjectRepositorySourceUnavailable) throw error;
        throw new ProjectRepositorySourceConflict("project source conflicts");
      } finally {
        if (temporary) rmSync(temporary, { recursive: true, force: true });
      }
    }).catch((error: unknown) => {
      if (error instanceof ProjectRepositorySourceConflict) throw error;
      throw new ProjectRepositorySourceUnavailable("project source is unavailable");
    });
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

function checkSourceContents(dir: string): void {
        for (const name of readdirSync(dir)) {
          const path = join(dir, name);
          const stat = lstatSync(path);
          if (stat.isSymbolicLink() || (!stat.isDirectory() && !stat.isFile())) {
            throw new Error("project repository contains unsafe entries");
          }
          if (stat.isDirectory()) checkSourceContents(path);
        }
}

function sourceGit(candidate: () => string): (args: string[], input?: string) => string {
      const git = (args: string[], input?: string): string => {
        checkSourceContents(candidate());
        if (existsSync(join(candidate(), "objects", "info", "alternates")) ||
            existsSync(join(candidate(), "objects", "info", "http-alternates")) ||
            existsSync(join(candidate(), "info", "grafts"))) {
          throw new Error("project repository has external object authority");
        }
        if (existsSync(join(candidate(), "config"))) {
          const config = readFileSync(join(candidate(), "config"), "utf8");
          for (const line of config.split(/\r?\n/).map(value => value.trim()).filter(Boolean)) {
            if (line !== "[core]" && !/^(repositoryformatversion\s*=\s*0|filemode\s*=\s*(true|false)|bare\s*=\s*true|logallrefupdates\s*=\s*(true|false)|ignorecase\s*=\s*(true|false)|symlinks\s*=\s*(true|false))$/.test(line)) {
              throw new Error("project repository configuration is noncanonical");
            }
          }
        }
        const env = { ...process.env };
        for (const key of Object.keys(env)) if (key.startsWith("GIT_")) delete env[key];
        Object.assign(env, {
          GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: process.platform === "win32" ? "NUL" : "/dev/null",
          GIT_NO_REPLACE_OBJECTS: "1", GIT_AUTHOR_NAME: "Leaf Source Service",
          GIT_AUTHOR_EMAIL: "source@leaf.invalid", GIT_COMMITTER_NAME: "Leaf Source Service",
          GIT_COMMITTER_EMAIL: "source@leaf.invalid", GIT_AUTHOR_DATE: "2000-01-01T00:00:00Z",
          GIT_COMMITTER_DATE: "2000-01-01T00:00:00Z",
        });
        const output = execFileSync("git", ["-c", "core.fsync=all", "-c", "pack.threads=1", "--no-optional-locks", "--git-dir", candidate(), ...args], {
          input, env, stdio: [input === undefined ? "ignore" : "pipe", "pipe", "ignore"],
          maxBuffer: 1024 * 1024, timeout: 20000,
        });
        const text = output.toString("utf8");
        if (!Buffer.from(text, "utf8").equals(output)) throw new Error("project source is not UTF-8");
        return text;
      };
  return git;
}

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
