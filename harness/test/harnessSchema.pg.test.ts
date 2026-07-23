import { afterAll, describe, expect, it } from "vitest";
import { Pool } from "pg";

import {
  assertHarnessCatalog,
  type HarnessColumn,
  type HarnessConstraint,
  type HarnessIndex,
} from "../src/ports/impl/harnessSchema.js";

const databaseUrl = process.env.LEAF_HARNESS_DATABASE_URL ?? process.env.DATABASE_URL;
const suite = databaseUrl ? describe : describe.skip;
const pool = databaseUrl ? new Pool({ connectionString: databaseUrl, max: 1 }) : null;

afterAll(async () => {
  await pool?.end();
});

suite("harness 0017 catalog contract", () => {
  it("matches exact column, constraint, index, and predicate definitions", async () => {
    const tableNames = [
      "harness_sessions",
      "harness_turns",
      "harness_events",
      "harness_confirmations",
      "harness_usage",
      "harness_tenant_repo_leases",
    ];
    const columns = await pool!.query<HarnessColumn>(
      `SELECT table_name, column_name, data_type, is_nullable
       FROM information_schema.columns
       WHERE table_schema = current_schema()
         AND table_name = ANY($1::text[])`,
      [tableNames],
    );
    const constraints = await pool!.query<HarnessConstraint>(
      `SELECT c.relname AS table_name, pg_get_constraintdef(pc.oid) AS definition
       FROM pg_constraint pc
       JOIN pg_class c ON c.oid = pc.conrelid
       JOIN pg_namespace n ON n.oid = c.relnamespace
       WHERE n.nspname = current_schema()
         AND c.relname = ANY($1::text[])`,
      [tableNames],
    );
    const indexes = await pool!.query<HarnessIndex>(
      `SELECT indexname, indexdef
       FROM pg_indexes
       WHERE schemaname = current_schema()
         AND indexname = ANY($1::text[])`,
      [[
        "harness_one_active_session",
        "harness_one_active_turn",
        "idx_harness_events_turn",
        "idx_harness_confirmations_session",
        "idx_harness_usage_session",
        "idx_harness_tenant_repo_leases_expiry",
      ]],
    );
    expect(() =>
      assertHarnessCatalog({
        columns: columns.rows,
        constraints: constraints.rows,
        indexes: indexes.rows,
      }),
    ).not.toThrow();
  });
});
