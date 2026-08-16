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
  withProjectWriterLease?<T>(
    authority: ProjectRepositoryAuthority,
    action: (
      witness: WriterLeaseWitness,
      runFenced: TenantMutationFence,
    ) => Promise<T>,
  ): Promise<T>;
}
