// STRANGLER SHIM (mushy-code extraction, 2026-08-06): this module moved to the
// vendored mushy-code library. The path and every export stay stable for all
// in-repo importers; the implementation lives at the re-exported location and
// is synced by scripts/sync-mushy-code.py (pin: harness/src/vendor/VENDOR-PIN.json).
export * from "../vendor/mushy-author/ports/index.js";

import type {
  TenantMutationFence,
  TenantRepoProvider as VendoredTenantRepoProvider,
} from "../vendor/mushy-author/ports/index.js";

/** Server-resolved authority for one project repository. No path is caller-selectable. */
export interface ProjectRepositoryAuthority {
  readonly tenantId: string;
  readonly organizationId: string;
  readonly projectId: string;
  readonly repoKey: string;
}

/** Exact PostgreSQL fence returned by the acquisition that owns the mutation. */
export interface WriterLeaseWitness {
  readonly writerLeaseId: string;
  readonly writerLeaseGeneration: string;
}

/** Additive project-repository lease boundary; legacy tenant methods stay unchanged. */
export interface TenantRepoProvider extends VendoredTenantRepoProvider {
  exportProjectSourceBundle?(
    authority: ProjectRepositoryAuthority,
    request: ProjectRepositorySourceBundleRequest,
  ): Promise<ProjectRepositorySourceBundleResult>;
  initializeProjectSource?(
    authority: ProjectRepositoryAuthority,
    request: ProjectRepositorySourceInitializationRequest,
  ): Promise<ProjectRepositorySourceInitializationResult>;
  withProjectWriterLease?<T>(
    authority: ProjectRepositoryAuthority,
    action: (
      witness: WriterLeaseWitness,
      runFenced: TenantMutationFence,
    ) => Promise<T>,
  ): Promise<T>;
  /** Read-only project source witness. It serializes with the writer lease but
   * deliberately exposes neither a writer witness nor a mutation fence. */
  verifyProjectSource?(
    authority: ProjectRepositoryAuthority,
    request: ProjectRepositorySourceVerificationRequest,
  ): Promise<void>;
  prepareProjectInverse?(
    authority: ProjectRepositoryAuthority,
    request: ProjectRepositoryInversePreparationRequest,
  ): Promise<ProjectRepositoryInversePreparationResult>;
}

export const PROJECT_REPOSITORY_SOURCE_WITNESS_CONTRACT =
  "leaf.project-repository-source-witness.v1";

export const PROJECT_REPOSITORY_SOURCE_BUNDLE_CONTRACT = "leaf.project-repository-source-bundle.v1";

export interface ProjectRepositorySourceBundleRequest {
  readonly sourceCommit: string;
  readonly sourceTree: string;
}

export interface ProjectRepositorySourceBundleResult extends ProjectRepositorySourceBundleRequest {
  readonly bundle: Buffer;
  readonly bundleSha256: string;
  readonly sizeBytes: number;
  readonly leaseId: string;
  readonly leaseGeneration: string;
}

export const PROJECT_REPOSITORY_SOURCE_INITIALIZER_CONTRACT =
  "leaf.project-repository-source-initializer.v1";

export class ProjectRepositorySourceConflict extends Error {}
export class ProjectRepositorySourceUnavailable extends Error {}

export interface ProjectRepositorySourceInitializationRequest {
  readonly seedDocument: string;
  readonly seedDigest: string;
}

export interface ProjectRepositorySourceInitializationResult extends WriterLeaseWitness {
  readonly sourceCommit: string;
  readonly sourceTree: string;
  readonly seedDigest: string;
  readonly replayed: boolean;
}

export type ProjectRepositorySourceRelation = "preview" | "inverse";

/** Closed immutable Git proof. The server owns the authority tuple separately. */
export type ProjectRepositorySourceVerificationRequest =
  | Readonly<{
      relation: "preview";
      baseCommit: string;
      baseTree: string;
      candidateCommit: string;
      candidateTree: string;
    }>
  | Readonly<{
      relation: "inverse";
      originalCommit: string;
      originalTree: string;
      targetCommit: string;
      targetTree: string;
      inverseCommit: string;
      inverseTree: string;
    }>;

export const PROJECT_REPOSITORY_INVERSE_PRODUCER_CONTRACT =
  "leaf.project-repository-inverse-producer.v1";

/** Server-issued operation identity plus immutable accepted Git witnesses. */
export interface ProjectRepositoryInversePreparationRequest {
  readonly sourceBatchId: string;
  readonly originalCommit: string;
  readonly originalTree: string;
  readonly targetCommit: string;
  readonly targetTree: string;
}

/** Closed producer result. The private ref and repository path never cross the port. */
export interface ProjectRepositoryInversePreparationResult {
  readonly inverseCommit: string;
  readonly inverseTree: string;
  readonly payloadDigest: string;
  readonly writerLeaseId: string;
  readonly writerLeaseGeneration: string;
}

/* ---------------------------------------------------------------------------
 * P8 Unit 4: project repository edit coordination. Closed, digest-only wire
 * values. No prompt, diff, source bytes, repository path, credential, or model
 * output ever appears in these shapes.
 * ------------------------------------------------------------------------- */

export type {
  TenantGitMatrix as ProjectRepositoryGitMatrix,
  TenantPublishObservation as ProjectRepositoryPublishObservation,
  TenantStageWitness as ProjectRepositoryStageWitness,
} from "../vendor/mushy-author/ports/impl/tenantChangeRepo.js";

export const PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT =
  "leaf.project-repository-edit-coordination.v1";

/** Closed Unit 1 staged receipt mapping (leaf.project-repository-edit.v2). */
export interface ProjectRepositoryStagedReceipt {
  readonly contract: "leaf.project-repository-edit.v2";
  readonly edit_id: string;
  readonly state: "staged";
  readonly operation: "edit" | "rollback";
  readonly source_edit_id: string | null;
  readonly actor_binding_id: string;
  readonly tenant_id: string;
  readonly organization_id: string;
  readonly project_id: string;
  readonly repo_key: string;
  readonly writer_lease_id: string;
  readonly writer_lease_generation: number;
  readonly base_commit: string;
  readonly staged_head_commit: string;
  readonly staged_tree: string;
  readonly changed_paths: readonly string[];
  readonly diff_digest: string;
  readonly instruction_digest: string;
  readonly idempotency_key: string;
}

interface CoordinationAuthorityFields {
  readonly tenant_id: string;
  readonly organization_id: string;
  readonly project_id: string;
  readonly repo_key: string;
}

export interface ProjectRepositoryEditRecordStagedRequest {
  readonly contract: typeof PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT;
  readonly action: "record_staged";
  readonly receipt: ProjectRepositoryStagedReceipt;
  readonly receipt_digest: string;
  readonly expected_version: number;
  readonly transition_key: string;
}

export interface ProjectRepositoryEditRecordStagedResponse {
  readonly contract: typeof PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT;
  readonly action: "record_staged";
  readonly edit_id: string;
  readonly state: "staged";
  readonly version: number;
}

export interface ProjectRepositoryEditAuthorizePublishRequest
  extends CoordinationAuthorityFields {
  readonly contract: typeof PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT;
  readonly action: "authorize_publish";
  readonly edit_id: string;
  readonly actor_binding_id: string;
  readonly confirmation_id: string;
  readonly receipt_digest: string;
  readonly publish_lease_id: string;
  readonly publish_lease_generation: number;
  readonly expected_version: number;
  readonly transition_key: string;
}

/** The frozen Git matrix the harness must recheck before its one compare-and-swap. */
export interface ProjectRepositoryEditPublishMatrix {
  readonly contract: typeof PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT;
  readonly action: "authorize_publish";
  readonly edit_id: string;
  readonly state: "publishing";
  readonly version: number;
  readonly receipt_digest: string;
  readonly expected_main_commit: string;
  readonly staged_head_commit: string;
  readonly staged_tree: string;
  readonly private_ref: string;
  readonly publish_lease_id: string;
  readonly publish_lease_generation: number;
}

export interface ProjectRepositoryEditSettlePublishRequest
  extends CoordinationAuthorityFields {
  readonly contract: typeof PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT;
  readonly action: "settle_publish";
  readonly edit_id: string;
  readonly actor_binding_id: string;
  readonly publish_lease_id: string;
  readonly publish_lease_generation: number;
  readonly private_ref_commit: string;
  readonly main_commit: string;
  readonly main_tree: string;
  readonly expected_version: number;
  readonly transition_key: string;
}

export interface ProjectRepositoryEditRecoverPublishRequest
  extends CoordinationAuthorityFields {
  readonly contract: typeof PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT;
  readonly action: "recover_publish";
  readonly edit_id: string;
  readonly actor_binding_id: string;
  readonly recovery_lease_id: string;
  readonly recovery_lease_generation: number;
  readonly private_ref_commit: string;
  readonly main_commit: string;
  readonly main_tree: string;
  readonly expected_version: number;
  readonly transition_key: string;
  readonly reason_code: string;
}

export interface ProjectRepositoryEditSettlementResponse {
  readonly contract: typeof PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT;
  readonly action: "settle_publish" | "recover_publish";
  readonly edit_id: string;
  readonly state: "published" | "publishing" | "conflicted";
  readonly version: number;
}

/** The Unit 4 coordination port. Unit 3 owns confirmation consumption; this
 * port never creates or consumes a confirmation and never accepts a repository
 * root or repository hint from the harness. */
export interface ProjectRepositoryEditCoordination {
  recordStaged(
    request: ProjectRepositoryEditRecordStagedRequest,
  ): Promise<ProjectRepositoryEditRecordStagedResponse>;
  authorizePublish(
    request: ProjectRepositoryEditAuthorizePublishRequest,
  ): Promise<ProjectRepositoryEditPublishMatrix>;
  settlePublish(
    request: ProjectRepositoryEditSettlePublishRequest,
  ): Promise<ProjectRepositoryEditSettlementResponse>;
  recoverPublish(
    request: ProjectRepositoryEditRecoverPublishRequest,
  ): Promise<ProjectRepositoryEditSettlementResponse>;
}
