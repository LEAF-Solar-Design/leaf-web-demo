/**
 * Lab document storage. Git results are not execution receipts. Consumers that
 * emit receipts must use contract/leaf-receipt.v1.schema.json (STD-6).
 * This port deliberately has its own export; ports/index.ts is unchanged.
 */
export const LAB_DOCUMENT_REF = "refs/heads/lab/main";

export interface LabDocumentWrite {
  readonly tenantId: string;
  /** null reserves the first, parentless Lab commit. */
  readonly expectedSha: string | null;
  /** Exact UTF-8 JSON bytes at lab/graphs/<id>.json or lab/widgets/<id>.json. */
  readonly documents: ReadonlyArray<{ readonly path: string; readonly content: string }>;
  readonly message: string;
}

export interface LabDocumentPort {
  commit(request: LabDocumentWrite): Promise<{ ref: typeof LAB_DOCUMENT_REF; commit: string }>;
  publishRef(tenantId: string, expectedSha: string | null): Promise<{ commit: string }>;
}

export { TenantDocumentRepo } from "./impl/tenantDocumentRepo.js";
export type { TenantDocumentRepoOptions } from "./impl/tenantDocumentRepo.js";
