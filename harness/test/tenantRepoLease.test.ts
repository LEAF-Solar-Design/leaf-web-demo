import { randomUUID } from "node:crypto";
import { spawn, execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Pool } from "pg";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import {
  assertAuthoringModeSafe,
  resolveAuthoringMode,
  PgTenantRepoLeaseCoordinator,
  TenantRepoLeaseHeldError,
  TenantRepoLeaseLostError,
  TenantRepoProviderImpl,
} from "../src/ports/impl/tenantRepoProvider.js";

const testUrl =
  process.env.PG_REPO_LEASE_TEST_URL ??
  process.env.PG_SESSION_STORE_TEST_URL;
const describeWithPostgres = testUrl ? describe : describe.skip;

describe("tenant repo lease production gate", () => {
  it("keeps legacy single-task authoring enabled by default", () => {
    expect(resolveAuthoringMode({})).toBe("disabled");
    expect(() => assertAuthoringModeSafe("disabled")).toThrow(/explicitly/);
  });

  it("fails closed when multi-task authoring is requested before vault wiring", () => {
    expect(() =>
      assertAuthoringModeSafe("fleet"),
    ).toThrow(/disabled until .* real Secrets Manager backend/);
  });
});

describeWithPostgres("PostgreSQL tenant repo lease", () => {
  const suffix = randomUUID().replaceAll("-", "");
  const tableName = `harness_repo_lease_${suffix}`;
  const table = `"${tableName}"`;
  const pool = new Pool({ connectionString: testUrl, max: 8 });
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
            TEST_DATABASE_URL: testUrl,
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
});
