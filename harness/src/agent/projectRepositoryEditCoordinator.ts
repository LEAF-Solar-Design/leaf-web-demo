/**
 * P8 Unit 5 internal project repository edit coordinator. It joins the Unit 2A
 * project writer lease, the Unit 4 Git witness adapter, and the Unit 4
 * coordination port into one dormant internal stage/publish/recover/rollback
 * transaction. No customer route, model tool, entitlement, repository path, or
 * prompt reaches this module. `git update-ref refs/heads/main <staged>
 * <expected>` inside the Unit 4 adapter remains the only main mutation.
 */

import { createHash } from "node:crypto";
import type { TenantChangeRepo, TenantChangeSet } from "../vendor/mushy-author/ports/impl/tenantChangeRepo.js";
import {
  PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT,
  type ProjectRepositoryAuthority,
  type ProjectRepositoryEditCoordination,
  type ProjectRepositoryEditPublishMatrix,
  type ProjectRepositoryEditSettlementResponse,
  type ProjectRepositoryEditSettlePublishRequest,
  type ProjectRepositoryGitMatrix,
  type ProjectRepositoryPublishObservation,
  type ProjectRepositoryStageWitness,
  type ProjectRepositoryStagedReceipt,
  type TenantRepoProvider,
} from "../ports/index.js";

const CANONICAL_UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const LOWER_SHA = /^[0-9a-f]{40}$/;
const LOWER_DIGEST = /^[0-9a-f]{64}$/;
const POSITIVE_GENERATION = /^[1-9][0-9]*$/;
const REASON_CODE = /^[a-z0-9_]{1,64}$/;

/** Fixed-code error. Codes and messages never carry content, paths, or secrets. */
export class ProjectRepositoryEditError extends Error {
  override readonly name: string = "ProjectRepositoryEditError";

  constructor(readonly code: string) {
    super(code);
  }
}

/**
 * The publish compare-and-swap returned an unexpected observation, or settlement
 * failed. Carries the frozen observation and exact settle request so recovery
 * can resume from Git truth without repeating publication or confirmation.
 */
export class ProjectRepositoryEditSettlementUnavailable extends ProjectRepositoryEditError {
  override readonly name = "ProjectRepositoryEditSettlementUnavailable";

  constructor(
    readonly observation: ProjectRepositoryPublishObservation,
    readonly settleRequest: ProjectRepositoryEditSettlePublishRequest,
  ) {
    super("settlement_unavailable");
  }
}

export type ProjectRepositoryEditGit = Pick<TenantChangeRepo,
  "createOrResume" | "stageCommitWitness" | "publishToMainObserved" |
  "observeGitMatrix" | "resolveCommitTree" | "cleanupWorktree" | "readRef">;

export interface ProjectRepositoryEditCoordinatorPorts {
  readonly leases: TenantRepoProvider;
  /** Server-bound repository handle for one already resolved authority. */
  readonly changeRepo: (authority: ProjectRepositoryAuthority) => ProjectRepositoryEditGit;
  readonly coordination: ProjectRepositoryEditCoordination;
}

export type DerivedChangeEvidence = {
  readonly changedPaths: readonly string[];
  readonly diffDigest: string;
};
export type DeriveChangeEvidence = (worktreeDir: string) =>
  DerivedChangeEvidence | Promise<DerivedChangeEvidence>;

export type StageProjectRepositoryEditRequest = {
  readonly authority: ProjectRepositoryAuthority;
  readonly editId: string;
  readonly actorBindingId: string;
  readonly operation: "edit" | "rollback";
  readonly sourceEditId: string | null;
  readonly expectedBaseCommit: string;
  readonly instructionDigest: string;
  readonly idempotencyKey: string;
  readonly commitMessage: string;
  /** The already bounded edit. It receives only the isolated worktree directory. */
  readonly apply: (worktreeDir: string) => void | Promise<void>;
} & (DerivedChangeEvidence & { readonly deriveChangeEvidence?: never } | {
  readonly deriveChangeEvidence: DeriveChangeEvidence;
  readonly changedPaths?: never;
  readonly diffDigest?: never;
});

export interface StagedProjectRepositoryEdit {
  readonly witness: ProjectRepositoryStageWitness;
  readonly receipt: ProjectRepositoryStagedReceipt;
  readonly receiptDigest: string;
  readonly version: number;
}

export interface PublishProjectRepositoryEditRequest {
  readonly authority: ProjectRepositoryAuthority;
  readonly editId: string;
  readonly actorBindingId: string;
  readonly confirmationId: string;
  readonly receiptDigest: string;
  readonly expectedVersion: number;
  readonly transitionKey: string;
}

export interface PublishedProjectRepositoryEdit {
  readonly matrix: ProjectRepositoryEditPublishMatrix;
  readonly observation: ProjectRepositoryPublishObservation;
  readonly settlement: ProjectRepositoryEditSettlementResponse;
}

export interface RecoverProjectRepositoryEditRequest {
  readonly authority: ProjectRepositoryAuthority;
  readonly editId: string;
  readonly actorBindingId: string;
  /** The immutable staged receipt's base, staged head, and staged tree. */
  readonly expectedMainCommit: string;
  readonly stagedHeadCommit: string;
  readonly stagedTree: string;
  readonly expectedVersion: number;
  readonly transitionKey: string;
  readonly reasonCode: string;
}

export interface RecoveredProjectRepositoryEdit {
  readonly observation: ProjectRepositoryGitMatrix;
  readonly settlement: ProjectRepositoryEditSettlementResponse;
  /** True only when recovery resumed the already consumed compare-and-swap. */
  readonly compareAndSwap: boolean;
}

function fail(code: string): never {
  throw new ProjectRepositoryEditError(code);
}

function requireUuid(value: string, code: string): string {
  if (typeof value !== "string" || !CANONICAL_UUID.test(value)) fail(code);
  return value;
}

function requireSha(value: string, code: string): string {
  if (typeof value !== "string" || !LOWER_SHA.test(value)) fail(code);
  return value;
}

function requireDigest(value: string, code: string): string {
  if (typeof value !== "string" || !LOWER_DIGEST.test(value)) fail(code);
  return value;
}

function requireKey(value: string, code: string): string {
  if (typeof value !== "string" || value.length < 1 || value.length > 200) fail(code);
  return value;
}

function requireGeneration(value: string, code: string): number {
  if (typeof value !== "string" || !POSITIVE_GENERATION.test(value)) fail(code);
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed)) fail(code);
  return parsed;
}

function requireAuthority(value: ProjectRepositoryAuthority): ProjectRepositoryAuthority {
  if (!value || typeof value !== "object") fail("invalid_authority");
  requireUuid(value.tenantId, "invalid_authority");
  requireUuid(value.organizationId, "invalid_authority");
  requireUuid(value.projectId, "invalid_authority");
  requireUuid(value.repoKey, "invalid_authority");
  return value;
}

function privateRefFor(editId: string): string {
  return `refs/leaf/changes/${editId}`;
}

/** RFC 8785 (JCS) canonical JSON for the receipt value domain. */
function canonicalJson(value: unknown): string {
  if (value === null) return "null";
  if (typeof value === "string") return JSON.stringify(value);
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value)) fail("receipt_not_canonical");
    return String(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0));
    return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${canonicalJson(item)}`).join(",")}}`;
  }
  fail("receipt_not_canonical");
}

export function projectRepositoryReceiptDigest(receipt: ProjectRepositoryStagedReceipt): string {
  return createHash("sha256").update(canonicalJson(receipt), "utf8").digest("hex");
}

export class ProjectRepositoryEditCoordinator {
  constructor(private readonly ports: ProjectRepositoryEditCoordinatorPorts) {}

  private leaseProvider(): Required<Pick<TenantRepoProvider, "withProjectWriterLease">> {
    const leases = this.ports.leases;
    if (typeof leases.withProjectWriterLease !== "function") {
      fail("project_writer_lease_required");
    }
    return leases as Required<Pick<TenantRepoProvider, "withProjectWriterLease">>;
  }

  /**
   * Stage one bounded edit on its deterministic private ref. Main is never
   * mutated. The immutable receipt keeps the exact stage lease witness.
   */
  async stageEdit(request: StageProjectRepositoryEditRequest): Promise<StagedProjectRepositoryEdit> {
    const authority = requireAuthority(request.authority);
    const editId = requireUuid(request.editId, "invalid_edit_id");
    const actorBindingId = requireUuid(request.actorBindingId, "invalid_actor");
    if (request.operation !== "edit" && request.operation !== "rollback") fail("invalid_operation");
    if (request.operation === "edit" && request.sourceEditId !== null) fail("invalid_source_edit");
    const sourceEditId = request.operation === "rollback"
      ? requireUuid(request.sourceEditId ?? fail("invalid_source_edit"), "invalid_source_edit")
      : null;
    const expectedBaseCommit = requireSha(request.expectedBaseCommit, "invalid_base_commit");
    const freezeEvidence = (value: DerivedChangeEvidence): DerivedChangeEvidence => {
      if (!value || !Array.isArray(value.changedPaths) || value.changedPaths.length < 1 ||
          value.changedPaths.length > 1000 ||
          value.changedPaths.some((path) => typeof path !== "string" || path.length < 1) ||
          new Set(value.changedPaths).size !== value.changedPaths.length) fail("invalid_changed_paths");
      return Object.freeze({
        changedPaths: Object.freeze([...value.changedPaths].sort((left, right) =>
          Buffer.compare(Buffer.from(left, "utf8"), Buffer.from(right, "utf8")))),
        diffDigest: requireDigest(value.diffDigest, "invalid_diff_digest"),
      });
    };
    if (request.deriveChangeEvidence !== undefined && typeof request.deriveChangeEvidence !== "function") {
      fail("invalid_change_evidence");
    }
    const legacyEvidence = request.deriveChangeEvidence ? undefined : freezeEvidence(request as DerivedChangeEvidence);
    const instructionDigest = requireDigest(request.instructionDigest, "invalid_instruction_digest");
    const idempotencyKey = requireKey(request.idempotencyKey, "invalid_idempotency_key");
    if (typeof request.commitMessage !== "string" ||
        request.commitMessage.length < 1 || request.commitMessage.length > 512) {
      fail("invalid_commit_message");
    }
    if (typeof request.apply !== "function") fail("invalid_apply");

    return this.leaseProvider().withProjectWriterLease(authority, async (witness, runFenced) => {
      const stageGeneration = requireGeneration(witness.writerLeaseGeneration, "invalid_lease_witness");
      requireUuid(witness.writerLeaseId, "invalid_lease_witness");
      const repo = this.ports.changeRepo(authority);
      if (request.deriveChangeEvidence && await runFenced(() => repo.readRef("refs/heads/main")) !== expectedBaseCommit) {
        fail("source_base_conflict");
      }
      const change = await runFenced(() => repo.createOrResume(editId, expectedBaseCommit));
      try {
        await runFenced(() => request.apply(change.dir));
        const evidence = request.deriveChangeEvidence
          ? await runFenced(async () => freezeEvidence(await request.deriveChangeEvidence!(change.dir)))
          : legacyEvidence!;
        const stageWitness = await runFenced(() =>
          repo.stageCommitWitness(change, request.commitMessage));
        const main = await runFenced(() => repo.readRef("refs/heads/main"));
        if (main !== expectedBaseCommit) fail("main_moved_during_stage");
        const changedPaths = evidence.changedPaths;
        const receipt: ProjectRepositoryStagedReceipt = Object.freeze({
          contract: "leaf.project-repository-edit.v2",
          edit_id: editId,
          state: "staged",
          operation: request.operation,
          source_edit_id: sourceEditId,
          actor_binding_id: actorBindingId,
          tenant_id: authority.tenantId,
          organization_id: authority.organizationId,
          project_id: authority.projectId,
          repo_key: authority.repoKey,
          writer_lease_id: witness.writerLeaseId,
          writer_lease_generation: stageGeneration,
          base_commit: stageWitness.base_commit,
          staged_head_commit: stageWitness.staged_head_commit,
          staged_tree: stageWitness.staged_tree,
          changed_paths: Object.freeze(changedPaths),
          diff_digest: evidence.diffDigest,
          instruction_digest: instructionDigest,
          idempotency_key: idempotencyKey,
        });
        const receiptDigest = projectRepositoryReceiptDigest(receipt);
        const response = await this.ports.coordination.recordStaged({
          contract: PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT,
          action: "record_staged",
          receipt,
          receipt_digest: receiptDigest,
          expected_version: 0,
          transition_key: idempotencyKey,
        });
        if (response.edit_id !== editId || response.state !== "staged") {
          fail("stage_record_mismatch");
        }
        return Object.freeze({
          witness: stageWitness, receipt, receiptDigest, version: response.version,
        });
      } finally {
        await runFenced(() => repo.cleanupWorktree(change));
      }
    });
  }

  /**
   * Publish one confirmed edit under a NEW writer lease on the same authority
   * tuple. The staged receipt's lease witness is never replaced; the publish
   * lease is a separate, strictly newer witness verified by the server before
   * the one main compare-and-swap.
   */
  async publishEdit(
    request: PublishProjectRepositoryEditRequest,
  ): Promise<PublishedProjectRepositoryEdit> {
    const authority = requireAuthority(request.authority);
    const editId = requireUuid(request.editId, "invalid_edit_id");
    const actorBindingId = requireUuid(request.actorBindingId, "invalid_actor");
    const confirmationId = requireUuid(request.confirmationId, "invalid_confirmation");
    const receiptDigest = requireDigest(request.receiptDigest, "invalid_receipt_digest");
    if (!Number.isSafeInteger(request.expectedVersion) || request.expectedVersion <= 0) {
      fail("invalid_expected_version");
    }
    const transitionKey = requireKey(request.transitionKey, "invalid_transition_key");

    return this.leaseProvider().withProjectWriterLease(authority, async (witness, runFenced) => {
      const publishGeneration = requireGeneration(witness.writerLeaseGeneration, "invalid_lease_witness");
      requireUuid(witness.writerLeaseId, "invalid_lease_witness");
      // The server atomically consumes the one-use confirmation and records
      // this publish lease as a separate witness before any Git mutation.
      const matrix = await this.ports.coordination.authorizePublish({
        contract: PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT,
        action: "authorize_publish",
        tenant_id: authority.tenantId,
        organization_id: authority.organizationId,
        project_id: authority.projectId,
        repo_key: authority.repoKey,
        edit_id: editId,
        actor_binding_id: actorBindingId,
        confirmation_id: confirmationId,
        receipt_digest: receiptDigest,
        publish_lease_id: witness.writerLeaseId,
        publish_lease_generation: publishGeneration,
        expected_version: request.expectedVersion,
        transition_key: transitionKey,
      });
      if (matrix.edit_id !== editId ||
          matrix.receipt_digest !== receiptDigest ||
          matrix.publish_lease_id !== witness.writerLeaseId ||
          matrix.publish_lease_generation !== publishGeneration ||
          matrix.private_ref !== privateRefFor(editId) ||
          matrix.state !== "publishing") {
        fail("publish_matrix_mismatch");
      }
      const repo = this.ports.changeRepo(authority);
      const change: TenantChangeSet = {
        id: editId,
        ref: matrix.private_ref,
        dir: "",
        expectedBaseSha: matrix.expected_main_commit,
        stagedSha: matrix.staged_head_commit,
      };
      const observation = await runFenced(() => {
        // Recheck the immutable staged witness inside the publish fence before
        // the one allowed compare-and-swap.
        if (repo.resolveCommitTree(matrix.staged_head_commit) !== matrix.staged_tree) {
          fail("staged_tree_mismatch");
        }
        return repo.publishToMainObserved(change, matrix.expected_main_commit);
      });
      const settleRequest: ProjectRepositoryEditSettlePublishRequest = Object.freeze({
        contract: PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT,
        action: "settle_publish",
        tenant_id: authority.tenantId,
        organization_id: authority.organizationId,
        project_id: authority.projectId,
        repo_key: authority.repoKey,
        edit_id: editId,
        actor_binding_id: actorBindingId,
        publish_lease_id: witness.writerLeaseId,
        publish_lease_generation: publishGeneration,
        private_ref_commit: observation.private_ref_commit,
        main_commit: observation.after_main_commit,
        main_tree: observation.after_main_tree,
        expected_version: matrix.version,
        transition_key: `${transitionKey}:settle`,
      });
      let settlement: ProjectRepositoryEditSettlementResponse;
      if (observation.private_ref_commit !== matrix.staged_head_commit ||
          observation.after_main_commit !== matrix.staged_head_commit ||
          observation.after_main_tree !== matrix.staged_tree) {
        throw new ProjectRepositoryEditSettlementUnavailable(observation, settleRequest);
      }
      try {
        settlement = await this.ports.coordination.settlePublish(settleRequest);
      } catch {
        // Git truth is frozen in the observation. Recovery resumes from it
        // without a second compare-and-swap or confirmation.
        throw new ProjectRepositoryEditSettlementUnavailable(observation, settleRequest);
      }
      if (settlement.edit_id !== editId) {
        throw new ProjectRepositoryEditSettlementUnavailable(observation, settleRequest);
      }
      return Object.freeze({ matrix, observation, settlement });
    });
  }

  /**
   * Recover one interrupted publish from the frozen Git matrix. A later lease
   * generation is allowed; the staged witness is never replaced and no new
   * confirmation is accepted. Observation-only unless the exact pre-publish
   * matrix permits resuming the already consumed compare-and-swap.
   */
  async recoverEdit(
    request: RecoverProjectRepositoryEditRequest,
  ): Promise<RecoveredProjectRepositoryEdit> {
    const authority = requireAuthority(request.authority);
    const editId = requireUuid(request.editId, "invalid_edit_id");
    const actorBindingId = requireUuid(request.actorBindingId, "invalid_actor");
    const expectedMainCommit = requireSha(request.expectedMainCommit, "invalid_expected_main");
    const stagedHeadCommit = requireSha(request.stagedHeadCommit, "invalid_staged_head");
    const stagedTree = requireSha(request.stagedTree, "invalid_staged_tree");
    if (!Number.isSafeInteger(request.expectedVersion) || request.expectedVersion <= 0) {
      fail("invalid_expected_version");
    }
    const transitionKey = requireKey(request.transitionKey, "invalid_transition_key");
    if (typeof request.reasonCode !== "string" || !REASON_CODE.test(request.reasonCode)) {
      fail("invalid_reason_code");
    }

    return this.leaseProvider().withProjectWriterLease(authority, async (witness, runFenced) => {
      const recoveryGeneration = requireGeneration(witness.writerLeaseGeneration, "invalid_lease_witness");
      requireUuid(witness.writerLeaseId, "invalid_lease_witness");
      const repo = this.ports.changeRepo(authority);
      const change: TenantChangeSet = {
        id: editId,
        ref: privateRefFor(editId),
        dir: "",
        expectedBaseSha: expectedMainCommit,
        stagedSha: stagedHeadCommit,
      };
      let matrix = await runFenced(() => repo.observeGitMatrix(change));
      if (matrix.main_commit === null || matrix.private_ref_commit === null ||
          matrix.main_tree === null) {
        fail("recovery_witness_incomplete");
      }
      let compareAndSwap = false;
      const alreadyPublished = matrix.main_commit === stagedHeadCommit &&
        matrix.private_ref_commit === stagedHeadCommit && matrix.main_tree === stagedTree;
      if (!alreadyPublished) {
        const resumable = matrix.main_commit === expectedMainCommit &&
          matrix.private_ref_commit === stagedHeadCommit &&
          (await runFenced(() => repo.resolveCommitTree(stagedHeadCommit))) === stagedTree;
        if (resumable) {
          // Resume only the failed compare-and-swap under the already consumed
          // transaction. Any other frozen matrix stays observation-only.
          const observation = await runFenced(() =>
            repo.publishToMainObserved(change, expectedMainCommit));
          compareAndSwap = observation.compare_and_swap;
          matrix = Object.freeze({
            private_ref_commit: observation.private_ref_commit,
            main_commit: observation.after_main_commit,
            main_tree: observation.after_main_tree,
          });
        }
      }
      const finalMatrix = matrix as {
        private_ref_commit: string; main_commit: string; main_tree: string;
      };
      const settlement = await this.ports.coordination.recoverPublish({
        contract: PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT,
        action: "recover_publish",
        tenant_id: authority.tenantId,
        organization_id: authority.organizationId,
        project_id: authority.projectId,
        repo_key: authority.repoKey,
        edit_id: editId,
        actor_binding_id: actorBindingId,
        recovery_lease_id: witness.writerLeaseId,
        recovery_lease_generation: recoveryGeneration,
        private_ref_commit: finalMatrix.private_ref_commit,
        main_commit: finalMatrix.main_commit,
        main_tree: finalMatrix.main_tree,
        expected_version: request.expectedVersion,
        transition_key: transitionKey,
        reason_code: request.reasonCode,
      });
      if (settlement.edit_id !== editId) fail("settlement_mismatch");
      return Object.freeze({ observation: matrix, settlement, compareAndSwap });
    });
  }
}
