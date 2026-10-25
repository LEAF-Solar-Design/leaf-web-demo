/**
 * Lab document storage. Git results are not execution receipts. Consumers that
 * emit receipts must use contract/leaf-receipt.v1.schema.json (STD-6).
 * This port deliberately has its own export; ports/index.ts is unchanged.
 */
export const LAB_DOCUMENT_REF = "refs/heads/lab/main";
export const LAB_GENERATION_REF = "refs/leaf/lab-generation";
export const LAB_TRANSITIONS_PREFIX = "refs/leaf/lab-transitions/";
export const LAB_GENERATION_CAPABILITY = "lab-generation-v1";

export type LabHistoryErrorCode = "lab_history_nonlinear" | "lab_history_too_long" | "lab_ops_ambiguous"
  | "lab_history_not_ancestor" | "lab_history_invalid";

export class LabHistoryError extends Error {
  constructor(readonly code: LabHistoryErrorCode, message: string = code) {
    super(message);
    this.name = "LabHistoryError";
  }
}

export interface LabOpIdentity {
  commit: string;
  docId: string;
  revision: number;
  sha256: string;
}

export interface LabHistoryCommit {
  commit: string;
  parent: string | null;
  changedPaths: string[];
  trailers: Record<string, string>;
}

export interface LabChangesRequest {
  since: string | null;
  head: string;
  limit?: number;
  after?: string;
}

export class GitGenerationConflictError extends Error {
  constructor(message = "Lab generation conflict") {
    super(message);
    this.name = "GitGenerationConflictError";
  }
}

export interface LabGenerationReceipt {
  readonly barrier_head: string | null;
  readonly from_generation: string | null;
  readonly to_generation: string;
  readonly transition_id: string;
}

export interface LabGenerationFence {
  readonly transitionId: string;
  readonly expectedGeneration: string | null;
  readonly newGeneration: string;
  readonly expectedHead: string | null;
}

export interface LabDocumentWrite {
  readonly tenantId: string;
  /** null reserves the first, parentless Lab commit. */
  readonly expectedSha: string | null;
  /** Exact UTF-8 JSON bytes at lab/graphs/<id>.json or lab/widgets/<id>.json. */
  readonly documents: ReadonlyArray<{ readonly path: string; readonly content: string }>;
  readonly message: string;
  readonly generation?: string;
}

export interface LabDocumentInspection {
  readonly ref: typeof LAB_DOCUMENT_REF;
  readonly head: string | null;
  readonly commits: ReadonlyArray<{ readonly commit: string; readonly parent: string | null; readonly message: string }>;
  readonly documents: ReadonlyArray<{ readonly path: string; readonly blob: string; readonly sha256: string; readonly bytes: number }>;
}

export interface LabDocumentPort {
  inspect(tenantId: string, options: { readonly limit: number }): Promise<LabDocumentInspection>;
  lookupOps(tenantId: string, request: { opIds: string[]; head: string }): Promise<{ ops: Record<string, LabOpIdentity | null> }>;
  changes(tenantId: string, request: LabChangesRequest): Promise<{ commits: LabHistoryCommit[]; next: string | null }>;
  commit(request: LabDocumentWrite): Promise<{ ref: typeof LAB_DOCUMENT_REF; commit: string }>;
  publishRef(tenantId: string, expectedSha: string | null, options?: { generation?: string; targetSha?: string }): Promise<{ commit: string }>;
  generation(tenantId: string): Promise<{ generation: string | null; generationOid: string | null; head: string | null }>;
  fence(tenantId: string, request: LabGenerationFence): Promise<{ receipt: LabGenerationReceipt; head: string | null }>;
}

export { TenantDocumentRepo } from "./impl/tenantDocumentRepo.js";
export type { TenantDocumentRepoOptions } from "./impl/tenantDocumentRepo.js";
