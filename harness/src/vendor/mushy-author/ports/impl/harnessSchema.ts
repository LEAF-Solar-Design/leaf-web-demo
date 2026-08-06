export interface HarnessColumn {
  table_name: string;
  column_name: string;
  data_type: string;
  is_nullable: "YES" | "NO";
}

export interface HarnessConstraint {
  table_name: string;
  definition: string;
}

export interface HarnessIndex {
  indexname: string;
  indexdef: string;
}

export interface HarnessCatalog {
  columns: HarnessColumn[];
  constraints: HarnessConstraint[];
  indexes: HarnessIndex[];
}

const REQUIRED_COLUMNS: Record<string, Record<string, [string, "YES" | "NO"]>> = {
  harness_sessions: {
    session_id: ["uuid", "NO"],
    tenant_id: ["text", "NO"],
    drawing_id: ["text", "NO"],
    sdk_session_id: ["text", "YES"],
    status: ["text", "NO"],
    summary: ["text", "YES"],
    created_at: ["timestamp with time zone", "NO"],
    updated_at: ["timestamp with time zone", "NO"],
  },
  harness_turns: {
    turn_id: ["text", "NO"],
    session_id: ["uuid", "NO"],
    seq_start: ["bigint", "NO"],
    status: ["text", "NO"],
    stop_reason: ["text", "YES"],
    started_at: ["timestamp with time zone", "NO"],
    ended_at: ["timestamp with time zone", "YES"],
  },
  harness_events: {
    session_id: ["uuid", "NO"],
    seq: ["bigint", "NO"],
    turn_id: ["text", "NO"],
    type: ["text", "NO"],
    data: ["jsonb", "NO"],
    ts: ["timestamp with time zone", "NO"],
  },
  harness_confirmations: {
    confirmation_id: ["text", "NO"],
    session_id: ["uuid", "NO"],
    turn_id: ["text", "NO"],
    action: ["text", "NO"],
    args_json: ["text", "NO"],
    kind: ["text", "NO"],
    status: ["text", "NO"],
    created_at: ["timestamp with time zone", "NO"],
    expires_at: ["timestamp with time zone", "NO"],
    decided_at: ["timestamp with time zone", "YES"],
    decided_by: ["text", "YES"],
  },
  harness_usage: {
    usage_id: ["bigint", "NO"],
    session_id: ["uuid", "NO"],
    turn_id: ["text", "NO"],
    usage: ["jsonb", "NO"],
    ts: ["timestamp with time zone", "NO"],
  },
  harness_tenant_repo_leases: {
    tenant_id: ["text", "NO"],
    owner_token: ["uuid", "NO"],
    generation: ["bigint", "NO"],
    acquired_at: ["timestamp with time zone", "NO"],
    heartbeat_at: ["timestamp with time zone", "NO"],
    expires_at: ["timestamp with time zone", "NO"],
  },
};

const REQUIRED_CONSTRAINT_DEFINITIONS: Record<string, string[]> = {
  harness_sessions: [
    "PRIMARY KEY (session_id)",
    "CHECK ((status = ANY (ARRAY['idle'::text, 'active'::text, 'dormant'::text, 'archived'::text])))",
  ],
  harness_turns: [
    "PRIMARY KEY (session_id, turn_id)",
    "FOREIGN KEY (session_id) REFERENCES harness_sessions(session_id) ON DELETE CASCADE",
    "CHECK ((status = ANY (ARRAY['active'::text, 'complete'::text, 'failed'::text])))",
  ],
  harness_events: [
    "PRIMARY KEY (session_id, seq)",
    "FOREIGN KEY (session_id) REFERENCES harness_sessions(session_id) ON DELETE CASCADE",
  ],
  harness_confirmations: [
    "PRIMARY KEY (confirmation_id)",
    "FOREIGN KEY (session_id) REFERENCES harness_sessions(session_id) ON DELETE CASCADE",
    "CHECK ((status = ANY (ARRAY['pending'::text, 'approved'::text, 'denied'::text, 'expired'::text])))",
  ],
  harness_usage: [
    "PRIMARY KEY (usage_id)",
    "FOREIGN KEY (session_id) REFERENCES harness_sessions(session_id) ON DELETE CASCADE",
  ],
  harness_tenant_repo_leases: [
    "PRIMARY KEY (tenant_id)",
    "CHECK ((generation > 0))",
  ],
};

const REQUIRED_INDEX_DEFINITIONS: Record<string, string> = {
  harness_one_active_session:
    "CREATE UNIQUE INDEX harness_one_active_session ON public.harness_sessions USING btree (tenant_id, drawing_id) WHERE (status <> 'archived'::text)",
  harness_one_active_turn:
    "CREATE UNIQUE INDEX harness_one_active_turn ON public.harness_turns USING btree (session_id) WHERE (status = 'active'::text)",
  idx_harness_events_turn:
    "CREATE INDEX idx_harness_events_turn ON public.harness_events USING btree (session_id, turn_id, seq)",
  idx_harness_confirmations_session:
    "CREATE INDEX idx_harness_confirmations_session ON public.harness_confirmations USING btree (session_id, status, expires_at)",
  idx_harness_usage_session:
    "CREATE INDEX idx_harness_usage_session ON public.harness_usage USING btree (session_id, ts)",
  idx_harness_tenant_repo_leases_expiry:
    "CREATE INDEX idx_harness_tenant_repo_leases_expiry ON public.harness_tenant_repo_leases USING btree (expires_at)",
};

function normalized(value: string): string {
  return value.toLowerCase().replace(/\s+/g, " ").trim();
}

function normalizedIndexDefinition(value: string): string {
  return value
    .replace(
      /\bON (?:"(?:[^"]|"")*"|[A-Z_][A-Z0-9_$]*)\./i,
      "ON ",
    )
    .replace(/\s+/g, " ")
    .trim();
}

export function assertHarnessCatalog(catalog: HarnessCatalog): void {
  const problems: string[] = [];
  const columns = new Map(
    catalog.columns.map((column) => [
      `${column.table_name}.${column.column_name}`,
      [column.data_type.toLowerCase(), column.is_nullable] as const,
    ]),
  );
  for (const [table, required] of Object.entries(REQUIRED_COLUMNS)) {
    for (const [column, expected] of Object.entries(required)) {
      const actual = columns.get(`${table}.${column}`);
      if (!actual || actual[0] !== expected[0] || actual[1] !== expected[1]) {
        problems.push(`${table}.${column}`);
      }
    }
  }

  const constraints = new Map<string, string[]>();
  for (const constraint of catalog.constraints) {
    const entries = constraints.get(constraint.table_name) ?? [];
    entries.push(normalized(constraint.definition));
    constraints.set(constraint.table_name, entries);
  }
  for (const [table, required] of Object.entries(REQUIRED_CONSTRAINT_DEFINITIONS)) {
    const definitions = new Set(constraints.get(table) ?? []);
    for (const definition of required) {
      if (!definitions.has(normalized(definition))) {
        problems.push(`${table}.constraint:${definition}`);
      }
    }
  }

  const indexes = new Map(
    catalog.indexes.map((index) => [
      index.indexname,
      normalizedIndexDefinition(index.indexdef),
    ]),
  );
  for (const [name, expected] of Object.entries(REQUIRED_INDEX_DEFINITIONS)) {
    if ((indexes.get(name) ?? "") !== normalizedIndexDefinition(expected)) {
      problems.push(`${name}:definition`);
    }
  }

  if (problems.length > 0) {
    throw new Error(`PostgreSQL harness schema is incomplete: ${problems.join(", ")}`);
  }
}
