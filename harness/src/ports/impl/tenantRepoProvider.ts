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
  TenantMutationFence,
  TenantRepoProvider,
  WriterLeaseWitness,
} from "../index.js";

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

  constructor(opts: TenantRepoProviderOptions) {
    const projectLease = configuredProjectLease(opts);
    super({
      ...opts,
      lease: opts.lease === false ? false : projectLease,
    });
    this.projectLease = projectLease;
    this.projectAuthoringMode = opts.authoringMode ?? resolveAuthoringMode();
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
}
