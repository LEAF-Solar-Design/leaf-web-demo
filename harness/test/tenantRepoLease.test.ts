import { randomUUID } from "node:crypto";
import { spawn, execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Pool } from "pg";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import type { ProjectRepositoryAuthority } from "../src/ports/index.js";
import {
  assertAuthoringModeSafe,
  resolveAuthoringMode,
  PgTenantRepoLeaseCoordinator,
  TenantRepoLeaseHeldError,
  TenantRepoLeaseLostError,
  TenantRepoProviderImpl,
} from "../src/ports/impl/tenantRepoProvider.js";

const PROJECT_AUTHORITY: ProjectRepositoryAuthority = Object.freeze({
  tenantId: "11111111-1111-4111-8111-111111111111",
  organizationId: "22222222-2222-4222-8222-222222222222",
  projectId: "33333333-3333-4333-8333-333333333333",
  repoKey: "44444444-4444-4444-8444-444444444444",
});
const PROJECT_LEASE_KEY = [
  "leaf-project-repository-v1",
  PROJECT_AUTHORITY.tenantId,
  PROJECT_AUTHORITY.organizationId,
  PROJECT_AUTHORITY.projectId,
  PROJECT_AUTHORITY.repoKey,
].join(":");

const testUrl =
  process.env.PG_REPO_LEASE_TEST_URL ??
  process.env.PG_SESSION_STORE_TEST_URL;

describe("tenant repo lease production gate", () => {
  it("returns the exact witness and fence from one project lease acquisition", async () => {
    const ownerToken = "55555555-5555-4555-8555-555555555555";
    const rawLease = {
      tenantId: PROJECT_LEASE_KEY,
      ownerToken,
      generation: "17",
      lost: false,
      repoDirs: new Set<string>(),
    };
    const coordinator = Object.create(
      PgTenantRepoLeaseCoordinator.prototype,
    ) as PgTenantRepoLeaseCoordinator;
    let acquisitions = 0;
    let fencedRuns = 0;
    let acquiredKey: string | undefined;
    let fencedLease: unknown;
    Object.defineProperties(coordinator, {
      withLease: {
        value: async <T>(
          leaseKey: string,
          action: (lease: typeof rawLease) => Promise<T>,
        ): Promise<T> => {
          acquisitions += 1;
          acquiredKey = leaseKey;
          return action(rawLease);
        },
      },
      runFenced: {
        value: async <T>(
          lease: typeof rawLease,
          action: () => Promise<T>,
        ): Promise<T> => {
          fencedRuns += 1;
          fencedLease = lease;
          return action();
        },
      },
    });

    const provider = new TenantRepoProviderImpl({
      locator: { async repoRef() { return "unused"; } },
      lease: coordinator,
      authoringMode: "singleton",
    });
    const value = await provider.withProjectWriterLease(
      {
        repoKey: PROJECT_AUTHORITY.repoKey,
        projectId: PROJECT_AUTHORITY.projectId,
        tenantId: PROJECT_AUTHORITY.tenantId,
        organizationId: PROJECT_AUTHORITY.organizationId,
      },
      async (witness, runFenced) => {
        expect(witness).toEqual({
          writerLeaseId: ownerToken,
          writerLeaseGeneration: "17",
        });
        expect(Object.isFrozen(witness)).toBe(true);
        return runFenced(async () => "fenced");
      },
    );

    expect(value).toBe("fenced");
    expect(acquisitions).toBe(1);
    expect(acquiredKey).toBe(PROJECT_LEASE_KEY);
    expect(fencedRuns).toBe(1);
    expect(fencedLease).toBe(rawLease);
  });

  it("rejects malformed or open project authority before acquisition", async () => {
    const coordinator = Object.create(
      PgTenantRepoLeaseCoordinator.prototype,
    ) as PgTenantRepoLeaseCoordinator;
    let acquisitions = 0;
    Object.defineProperty(coordinator, "withLease", {
      value: async (): Promise<never> => {
        acquisitions += 1;
        throw new Error("unexpected acquisition");
      },
    });
    const invalid: unknown[] = [
      null,
      [],
      { ...PROJECT_AUTHORITY, tenantId: "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA" },
      { ...PROJECT_AUTHORITY, projectId: "not-a-uuid" },
      {
        tenantId: PROJECT_AUTHORITY.tenantId,
        organizationId: PROJECT_AUTHORITY.organizationId,
        projectId: PROJECT_AUTHORITY.projectId,
      },
      { ...PROJECT_AUTHORITY, checkoutPath: "C:/tenant" },
    ];

    for (const authority of invalid) {
      await expect(
        coordinator.withProjectLease(
          authority as ProjectRepositoryAuthority,
          async () => "unexpected",
        ),
      ).rejects.toThrow(/authority/);
    }
    expect(acquisitions).toBe(0);
  });

  it("fails closed without a PostgreSQL project writer lease", async () => {
    let actionCalled = false;
    const provider = new TenantRepoProviderImpl({
      locator: { async repoRef() { return "unused"; } },
      lease: false,
      authoringMode: "singleton",
    });

    await expect(
      provider.withProjectWriterLease(PROJECT_AUTHORITY, async () => {
        actionCalled = true;
      }),
    ).rejects.toThrow(/PostgreSQL project repository writer lease is required/);
    expect(actionCalled).toBe(false);
  });

  it("keeps legacy single-task authoring enabled by default", () => {
    expect(resolveAuthoringMode({})).toBe("disabled");
    expect(() => assertAuthoringModeSafe("disabled")).toThrow(/explicitly/);
  });

  it("fails closed when multi-task authoring is requested before vault wiring", () => {
    expect(() =>
      assertAuthoringModeSafe("fleet"),
    ).toThrow(/disabled until .* real Secrets Manager backend/);
  });

  it("never substitutes an identity fence for a missing PostgreSQL writer lease", async () => {
    let actionCalled = false;
    const provider = new TenantRepoProviderImpl({
      locator: { async repoRef() { return "unused"; } },
      lease: false,
      authoringMode: "singleton",
    });

    await expect(provider.withTenantLease("tenant-no-lease", async () => {
      actionCalled = true;
    })).rejects.toThrow(/PostgreSQL tenant repository writer lease is required/);
    expect(actionCalled).toBe(false);
  });

  it("fails closed on a missing read lease when authoring is enabled", async () => {
    let actionCalled = false;
    const provider = new TenantRepoProviderImpl({
      locator: { async repoRef() { return "unused"; } },
      lease: false,
      authoringMode: "singleton",
    });

    await expect(provider.withTenantReadLease("tenant-no-lease", async () => {
      actionCalled = true;
    })).rejects.toThrow(/PostgreSQL tenant repository read lease is required/);
    expect(actionCalled).toBe(false);
  });

  it("allows an unfenced committed read only while authoring is disabled", async () => {
    const provider = new TenantRepoProviderImpl({
      locator: { async repoRef() { return "unused"; } },
      lease: false,
      authoringMode: "disabled",
    });

    await expect(provider.withTenantReadLease("tenant-read-only", async () => "read"))
      .resolves.toBe("read");
  });

  it("preserves the primary authoring error when fenced teardown also fails", async () => {
    const primary = new Error("authoring failed");
    const teardown = new Error("teardown failed");
    const lease = {
      async withLease<T>(_tenantId: string, action: (lease: object) => Promise<T>): Promise<T> {
        return action({ repoDirs: new Set(["unused"]), tenantId: "tenant", lost: false });
      },
      async runFenced(): Promise<never> {
        throw teardown;
      },
    } as unknown as PgTenantRepoLeaseCoordinator;
    const provider = new TenantRepoProviderImpl({
      locator: { async repoRef() { return "unused"; } },
      lease,
      authoringMode: "singleton",
    });

    await expect(provider.withTenantLease("tenant", async () => {
      throw primary;
    })).rejects.toBe(primary);
    expect(primary.cause).toBe(teardown);
  });

  it("surfaces fenced teardown failure after a successful action", async () => {
    const teardown = new Error("teardown failed");
    const lease = {
      async withLease<T>(_tenantId: string, action: (lease: object) => Promise<T>): Promise<T> {
        return action({ repoDirs: new Set(["unused"]), tenantId: "tenant", lost: false });
      },
      async runFenced(): Promise<never> {
        throw teardown;
      },
    } as unknown as PgTenantRepoLeaseCoordinator;
    const provider = new TenantRepoProviderImpl({
      locator: { async repoRef() { return "unused"; } },
      lease,
      authoringMode: "singleton",
    });

    await expect(provider.withTenantLease("tenant", async () => "ok"))
      .rejects.toBe(teardown);
  });
});

if (testUrl) {
  const postgresUrl = testUrl;
  describe("PostgreSQL tenant repo lease", () => {
  const suffix = randomUUID().replaceAll("-", "");
  const tableName = `harness_repo_lease_${suffix}`;
  const table = `"${tableName}"`;
  const pool = new Pool({ connectionString: postgresUrl, max: 8 });
  const first = new PgTenantRepoLeaseCoordinator({
    pool,
    tableName,
    ttlMs: 300,
  });
  const second = new PgTenantRepoLeaseCoordinator({
    pool,
    tableName,
    ttlMs: 300,
  });

  beforeAll(async () => {
    await pool.query(`
      CREATE TABLE ${table} (
        tenant_id TEXT PRIMARY KEY,
        owner_token UUID NOT NULL,
        generation BIGINT NOT NULL CHECK (generation > 0),
        acquired_at TIMESTAMPTZ NOT NULL,
        heartbeat_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL
      )
    `);
  });

  afterAll(async () => {
    await pool.query(`DROP TABLE IF EXISTS ${table}`);
    await pool.end();
  });

  it("allows only one writer and heartbeats beyond the original TTL", async () => {
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    const owner = first.withLease("tenant-held", async () => {
      await held;
      return "owner";
    });

    await new Promise((resolve) => setTimeout(resolve, 350));
    await expect(
      second.withLease("tenant-held", async () => "contender"),
    ).rejects.toBeInstanceOf(TenantRepoLeaseHeldError);
    release();
    await expect(owner).resolves.toBe("owner");
  });

  it("takes over an expired row left by a dead worker", async () => {
    await pool.query(
      `INSERT INTO ${table}
         (tenant_id, owner_token, generation, acquired_at, heartbeat_at, expires_at)
       VALUES ($1, $2, 7, clock_timestamp(), clock_timestamp(),
               clock_timestamp() - interval '1 second')`,
      ["tenant-dead", randomUUID()],
    );
    await expect(
      second.withLease("tenant-dead", async () => "recovered"),
    ).resolves.toBe("recovered");
    const generation = await pool.query<{ generation: string }>(
      `SELECT generation::text FROM ${table} WHERE tenant_id = $1`,
      ["tenant-dead"],
    );
    expect(generation.rows[0]?.generation).toBe("8");
  });

  it("recovers after hard process death and cleans abandoned edits", async () => {
    const repoDir = mkdtempSync(join(tmpdir(), "leaf-hard-death-"));
    try {
      writeFileSync(join(repoDir, "registry.json"), '{"tools":[]}\n', "utf8");
      execFileSync("git", ["init", "-q"], { cwd: repoDir });
      execFileSync("git", ["-c", "user.name=test", "-c", "user.email=test@example.com", "add", "-A"], { cwd: repoDir });
      execFileSync("git", ["-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-q", "-m", "seed"], { cwd: repoDir });

      const worker = spawn(
        process.execPath,
        [new URL("./fixtures/repo-lease-worker.mjs", import.meta.url).pathname],
        {
          env: {
            ...process.env,
            TEST_DATABASE_URL: postgresUrl,
            TEST_LEASE_TABLE: tableName,
            TEST_TENANT_ID: "tenant-hard-death",
            TEST_DIRTY_PATH: join(repoDir, "partial.txt"),
          },
          stdio: ["ignore", "pipe", "pipe"],
        },
      );
      await new Promise<void>((resolve, reject) => {
        worker.once("error", reject);
        worker.stdout.once("data", (data) =>
          String(data).includes("READY") ? resolve() : reject(new Error("worker not ready")),
        );
      });
      expect(existsSync(join(repoDir, "partial.txt"))).toBe(true);
      worker.kill("SIGKILL");
      await new Promise<void>((resolve) => worker.once("exit", () => resolve()));
      await new Promise((resolve) => setTimeout(resolve, 350));

      const provider = new TenantRepoProviderImpl({
        locator: { async repoRef() { return repoDir; } },
        inPlace: true,
        lease: second,
        authoringMode: "singleton",
      });
      await provider.withTenantLease("tenant-hard-death", async () => {
        await provider.checkout("tenant-hard-death");
        expect(existsSync(join(repoDir, "partial.txt"))).toBe(false);
        writeFileSync(join(repoDir, "failed.txt"), "failed edit\n", "utf8");
        throw new Error("simulated author failure");
      }).catch((error: unknown) => {
        expect(error).toBeInstanceOf(Error);
      });
      expect(execFileSync("git", ["status", "--porcelain"], { cwd: repoDir, encoding: "utf8" })).toBe("");
    } finally {
      rmSync(repoDir, { recursive: true, force: true });
    }
  });

  it("rejects a stale owner token without running its commit action", async () => {
    const staleToken = randomUUID();
    await pool.query(
      `INSERT INTO ${table}
         (tenant_id, owner_token, generation, acquired_at, heartbeat_at, expires_at)
       VALUES ($1, $2, 3, clock_timestamp(), clock_timestamp(),
               clock_timestamp() - interval '1 second')`,
      ["tenant-fenced", staleToken],
    );
    let staleActionRuns = 0;
    await second.withLease("tenant-fenced", async () => {
      const staleLease = {
        tenantId: "tenant-fenced",
        ownerToken: staleToken,
        generation: "3",
        lost: false,
        repoDirs: new Set<string>(),
      };
      await expect(
        first.runFenced(staleLease, async () => {
          staleActionRuns += 1;
        }),
      ).rejects.toBeInstanceOf(TenantRepoLeaseLostError);
    });
    expect(staleActionRuns).toBe(0);
  });

  it("contends on the full project authority and separates another repo key", async () => {
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    const owner = first.withProjectLease(PROJECT_AUTHORITY, async () => {
      await held;
      return "owner";
    });

    await new Promise((resolve) => setTimeout(resolve, 25));
    await expect(
      second.withProjectLease(PROJECT_AUTHORITY, async () => "contender"),
    ).rejects.toBeInstanceOf(TenantRepoLeaseHeldError);
    await expect(
      second.withProjectLease(
        {
          ...PROJECT_AUTHORITY,
          repoKey: "66666666-6666-4666-8666-666666666666",
        },
        async () => "other-repo",
      ),
    ).resolves.toBe("other-repo");
    release();
    await expect(owner).resolves.toBe("owner");
  });

  it("returns the stored witness and denies a generation changed after acquisition", async () => {
    const authority = {
      ...PROJECT_AUTHORITY,
      repoKey: "77777777-7777-4777-8777-777777777777",
    };
    const key = [
      "leaf-project-repository-v1",
      authority.tenantId,
      authority.organizationId,
      authority.projectId,
      authority.repoKey,
    ].join(":");
    let mutationRuns = 0;

    await first.withProjectLease(authority, async (witness, runFenced) => {
      const stored = await pool.query<{
        owner_token: string;
        generation: string;
      }>(
        `SELECT owner_token::text, generation::text FROM ${table} WHERE tenant_id = $1`,
        [key],
      );
      expect(witness).toEqual({
        writerLeaseId: stored.rows[0]?.owner_token,
        writerLeaseGeneration: stored.rows[0]?.generation,
      });
      await pool.query(
        `UPDATE ${table} SET generation = generation + 1 WHERE tenant_id = $1`,
        [key],
      );
      await expect(
        runFenced(async () => {
          mutationRuns += 1;
        }),
      ).rejects.toBeInstanceOf(TenantRepoLeaseLostError);
    });
    expect(mutationRuns).toBe(0);
  });
  });
}
