import { describe, expect, it } from "vitest";

import {
  assertHarnessCatalog,
  type HarnessCatalog,
  type HarnessColumn,
} from "../src/ports/impl/harnessSchema.js";

function columns(
  table_name: string,
  specs: Record<string, [string, "YES" | "NO"]>,
): HarnessColumn[] {
  return Object.entries(specs).map(([column_name, [data_type, is_nullable]]) => ({
    table_name,
    column_name,
    data_type,
    is_nullable,
  }));
}

function validCatalog(): HarnessCatalog {
  return {
    columns: [
      ...columns("harness_sessions", {
        session_id: ["uuid", "NO"], tenant_id: ["text", "NO"],
        drawing_id: ["text", "NO"], sdk_session_id: ["text", "YES"],
        status: ["text", "NO"], summary: ["text", "YES"],
        created_at: ["timestamp with time zone", "NO"],
        updated_at: ["timestamp with time zone", "NO"],
      }),
      ...columns("harness_turns", {
        turn_id: ["text", "NO"], session_id: ["uuid", "NO"],
        seq_start: ["bigint", "NO"], status: ["text", "NO"],
        stop_reason: ["text", "YES"], started_at: ["timestamp with time zone", "NO"],
        ended_at: ["timestamp with time zone", "YES"],
      }),
      ...columns("harness_events", {
        session_id: ["uuid", "NO"], seq: ["bigint", "NO"], turn_id: ["text", "NO"],
        type: ["text", "NO"], data: ["jsonb", "NO"], ts: ["timestamp with time zone", "NO"],
      }),
      ...columns("harness_confirmations", {
        confirmation_id: ["text", "NO"], session_id: ["uuid", "NO"],
        turn_id: ["text", "NO"], action: ["text", "NO"], args_json: ["text", "NO"],
        kind: ["text", "NO"], status: ["text", "NO"],
        created_at: ["timestamp with time zone", "NO"],
        expires_at: ["timestamp with time zone", "NO"],
        decided_at: ["timestamp with time zone", "YES"], decided_by: ["text", "YES"],
      }),
      ...columns("harness_usage", {
        usage_id: ["bigint", "NO"], session_id: ["uuid", "NO"],
        turn_id: ["text", "NO"], usage: ["jsonb", "NO"],
        ts: ["timestamp with time zone", "NO"],
      }),
      ...columns("harness_tenant_repo_leases", {
        tenant_id: ["text", "NO"], owner_token: ["uuid", "NO"],
        generation: ["bigint", "NO"], acquired_at: ["timestamp with time zone", "NO"],
        heartbeat_at: ["timestamp with time zone", "NO"],
        expires_at: ["timestamp with time zone", "NO"],
      }),
    ],
    constraints: [
      { table_name: "harness_sessions", definition: "PRIMARY KEY (session_id)" },
      { table_name: "harness_sessions", definition: "CHECK ((status = ANY (ARRAY['idle'::text, 'active'::text, 'dormant'::text, 'archived'::text])))" },
      { table_name: "harness_turns", definition: "PRIMARY KEY (session_id, turn_id)" },
      { table_name: "harness_turns", definition: "FOREIGN KEY (session_id) REFERENCES harness_sessions(session_id) ON DELETE CASCADE" },
      { table_name: "harness_turns", definition: "CHECK ((status = ANY (ARRAY['active'::text, 'complete'::text, 'failed'::text])))" },
      { table_name: "harness_events", definition: "PRIMARY KEY (session_id, seq)" },
      { table_name: "harness_events", definition: "FOREIGN KEY (session_id) REFERENCES harness_sessions(session_id) ON DELETE CASCADE" },
      { table_name: "harness_confirmations", definition: "PRIMARY KEY (confirmation_id)" },
      { table_name: "harness_confirmations", definition: "FOREIGN KEY (session_id) REFERENCES harness_sessions(session_id) ON DELETE CASCADE" },
      { table_name: "harness_confirmations", definition: "CHECK ((status = ANY (ARRAY['pending'::text, 'approved'::text, 'denied'::text, 'expired'::text])))" },
      { table_name: "harness_usage", definition: "PRIMARY KEY (usage_id)" },
      { table_name: "harness_usage", definition: "FOREIGN KEY (session_id) REFERENCES harness_sessions(session_id) ON DELETE CASCADE" },
      { table_name: "harness_tenant_repo_leases", definition: "PRIMARY KEY (tenant_id)" },
      { table_name: "harness_tenant_repo_leases", definition: "CHECK ((generation > 0))" },
    ],
    indexes: [
      { indexname: "harness_one_active_session", indexdef: "CREATE UNIQUE INDEX harness_one_active_session ON public.harness_sessions USING btree (tenant_id, drawing_id) WHERE (status <> 'archived'::text)" },
      { indexname: "harness_one_active_turn", indexdef: "CREATE UNIQUE INDEX harness_one_active_turn ON public.harness_turns USING btree (session_id) WHERE (status = 'active'::text)" },
      { indexname: "idx_harness_events_turn", indexdef: "CREATE INDEX idx_harness_events_turn ON public.harness_events USING btree (session_id, turn_id, seq)" },
      { indexname: "idx_harness_confirmations_session", indexdef: "CREATE INDEX idx_harness_confirmations_session ON public.harness_confirmations USING btree (session_id, status, expires_at)" },
      { indexname: "idx_harness_usage_session", indexdef: "CREATE INDEX idx_harness_usage_session ON public.harness_usage USING btree (session_id, ts)" },
      { indexname: "idx_harness_tenant_repo_leases_expiry", indexdef: "CREATE INDEX idx_harness_tenant_repo_leases_expiry ON public.harness_tenant_repo_leases USING btree (expires_at)" },
    ],
  };
}

describe("assertHarnessCatalog", () => {
  it("accepts the checked-in 0017 schema contract", () => {
    expect(() => assertHarnessCatalog(validCatalog())).not.toThrow();
  });

  it("rejects a stale single-active-turn index predicate", () => {
    const catalog = validCatalog();
    const index = catalog.indexes.find((entry) => entry.indexname === "harness_one_active_turn")!;
    index.indexdef = index.indexdef.replace("'active'", "'complete'");
    expect(() => assertHarnessCatalog(catalog)).toThrow(/harness_one_active_turn/);
  });

  it("rejects an appended single-active-turn predicate", () => {
    const catalog = validCatalog();
    const index = catalog.indexes.find((entry) => entry.indexname === "harness_one_active_turn")!;
    index.indexdef += " AND (seq_start > 0)";
    expect(() => assertHarnessCatalog(catalog)).toThrow(/harness_one_active_turn/);
  });

  it("rejects a wrong lease fencing type", () => {
    const catalog = validCatalog();
    const generation = catalog.columns.find(
      (entry) =>
        entry.table_name === "harness_tenant_repo_leases" &&
        entry.column_name === "generation",
    )!;
    generation.data_type = "integer";
    expect(() => assertHarnessCatalog(catalog)).toThrow(/generation/);
  });
});
