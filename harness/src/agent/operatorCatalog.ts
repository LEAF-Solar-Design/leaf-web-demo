// Sealed read-only operator tool catalog (contract/OPERATOR.md section 4,
// Lane C). The FIRST ENABLED CATALOG IS READ-ONLY: every tool here is an O1
// inspection surface. Write-classed actions (worker submit, repo propose,
// tenant pause, ...) are deliberately absent from this registry and a freeze
// test pins the exact name list. sealCatalog() runs at mount time: an
// unknown tool, missing schema, or unmapped executor throws, so a
// misconfigured operator surface never boots (startup-fails-closed).

export interface OperatorToolSpec {
  name: string;
  description: string;
  // JSON-schema-shaped input contract (validated by the executor layer).
  inputSchema: Record<string, unknown>;
  // The app API the executor may touch. Read-only by contract.
  appRoute: string;
}

export const OPERATOR_READONLY_TOOLS: readonly OperatorToolSpec[] = [
  {
    name: "operator_read_fleet_state",
    description: "Read fleet-level state: tenants, kill switches, spend.",
    inputSchema: { type: "object", additionalProperties: false },
    appRoute: "GET /api/ops/tenants",
  },
  {
    name: "operator_read_tenant_state",
    description: "Read one tenant's state (catalog, entitlements, spend).",
    inputSchema: {
      type: "object",
      properties: { tenant_id: { type: "string" } },
      required: ["tenant_id"],
      additionalProperties: false,
    },
    appRoute: "GET /api/ops/agent/tenants",
  },
  {
    name: "operator_read_jobs",
    description: "Read recent job state.",
    inputSchema: {
      type: "object",
      properties: { limit: { type: "integer", maximum: 200 } },
      additionalProperties: false,
    },
    appRoute: "GET /api/jobs",
  },
  {
    name: "operator_read_sessions",
    description: "Read the operator's own session list.",
    inputSchema: { type: "object", additionalProperties: false },
    appRoute: "GET /api/operator/sessions",
  },
  {
    name: "operator_read_audit",
    description: "Read the operator's own security-audit trail.",
    inputSchema: {
      type: "object",
      properties: { limit: { type: "integer", maximum: 500 } },
      additionalProperties: false,
    },
    appRoute: "GET /api/operator/audit",
  },
  {
    name: "operator_read_worker_status",
    description: "Read disposable worker job status and receipts.",
    inputSchema: {
      type: "object",
      properties: { job_id: { type: "string" } },
      additionalProperties: false,
    },
    appRoute: "local worker-manager read",
  },
] as const;

export type OperatorExecutor = (
  args: Record<string, unknown>,
) => Promise<{ ok: boolean; summary: string; data?: unknown }>;

/** Startup seal: every catalog tool maps to exactly one executor and every
 * executor maps back to a catalog tool. Any mismatch throws — the caller
 * must let that abort the mount, never continue with a partial surface. */
export function sealCatalog(
  executors: Map<string, OperatorExecutor>,
): void {
  const names = new Set(OPERATOR_READONLY_TOOLS.map((t) => t.name));
  if (names.size !== OPERATOR_READONLY_TOOLS.length) {
    throw new Error("operator catalog: duplicate tool name");
  }
  for (const spec of OPERATOR_READONLY_TOOLS) {
    if (!spec.inputSchema || typeof spec.inputSchema !== "object") {
      throw new Error(`operator catalog: ${spec.name} missing schema`);
    }
    if (!executors.has(spec.name)) {
      throw new Error(`operator catalog: ${spec.name} has no executor`);
    }
  }
  for (const key of executors.keys()) {
    if (!names.has(key)) {
      throw new Error(`operator catalog: executor ${key} not in catalog`);
    }
    if (/write|submit|deploy|delete|create|rotate|pause/i.test(key)) {
      throw new Error(
        `operator catalog: ${key} is write-shaped; the first enabled ` +
        "catalog is read-only by contract",
      );
    }
  }
}
