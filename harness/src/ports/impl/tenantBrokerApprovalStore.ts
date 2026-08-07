import { randomUUID } from "node:crypto";
import { mkdir, readFile, rename, unlink, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { Pool } from "pg";
import type { QueryResultRow } from "pg";

import type {
  StandardServiceIdentity,
  TenantBrokerApprovalClaimResult,
  TenantBrokerApprovalStore,
  TenantBrokerCompletionReceipt,
  TenantBrokerPendingApprovalBinding,
} from "../../vendor/mushy-author/index.js";
import {
  validStandardServiceArtifactIds,
} from "./standardServiceArtifactContract.js";

const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;
const APPROVAL_ID = /^[A-Za-z0-9_-]{8,256}$/;
const EXECUTION_CLAIM_ID = /^[A-Za-z0-9_-]{16,256}$/;
const DIGEST = /^[a-f0-9]{64}$/;
const CANONICAL_EXPIRY = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;
const MAX_RECEIPT_BYTES = 1_048_576;

function canonicalExpiryMillis(value: unknown): number | null {
  if (typeof value !== "string" || value.length !== 24 || !CANONICAL_EXPIRY.test(value)) return null;
  const parsed = Date.parse(value);
  if (!Number.isSafeInteger(parsed)) return null;
  try {
    return new Date(parsed).toISOString() === value ? parsed : null;
  } catch {
    return null;
  }
}

const REQUIRED_APPROVAL_COLUMNS = new Map<string, readonly [string, string]>([
  ["approval_id", ["text", "NO"]],
  ["tenant_id", ["text", "NO"]],
  ["subject_id", ["text", "NO"]],
  ["session_id", ["text", "NO"]],
  ["authority_turn_id", ["text", "NO"]],
  ["subscription_mount_id", ["text", "NO"]],
  ["runner_profile_id", ["text", "NO"]],
  ["service_id", ["text", "NO"]],
  ["tool_id", ["text", "NO"]],
  ["arguments", ["jsonb", "NO"]],
  ["argument_digest", ["text", "NO"]],
  ["expires_at", ["timestamp with time zone", "NO"]],
  ["created_at", ["timestamp with time zone", "NO"]],
  ["execution_state", ["text", "NO"]],
  ["approved_at", ["timestamp with time zone", "YES"]],
  ["execution_claim_id", ["text", "YES"]],
  ["execution_started_at", ["timestamp with time zone", "YES"]],
  ["execution_deadline_at", ["timestamp with time zone", "YES"]],
  ["result", ["jsonb", "YES"]],
  ["completed_at", ["timestamp with time zone", "YES"]],
  ["uncertain_at", ["timestamp with time zone", "YES"]],
]);

const REQUIRED_APPROVAL_CONSTRAINTS = new Map<string, string>([
  ["harness_tenant_mcp_approvals_pkey", "primarykeyapproval_id"],
  ["harness_tenant_mcp_approvals_id_check", "approval_id~'^[a-za-z0-9_-]{8,256}$'"],
  ["harness_tenant_mcp_approvals_profile_check", "runner_profile_id=anyarray['author','spine']"],
  ["harness_tenant_mcp_approvals_arguments_check", "jsonb_typeofarguments='object'"],
  ["harness_tenant_mcp_approvals_digest_check", "argument_digest~'^[a-f0-9]{64}$'"],
  ["harness_tenant_mcp_approvals_state_check", "execution_state=anyarray['pending','approved','executing','completed','uncertain']"],
  ["harness_tenant_mcp_approvals_claim_check", "execution_claim_idisnullorexecution_claim_id~'^[a-za-z0-9_-]{16,256}$'"],
  ["harness_tenant_mcp_approvals_result_check", "jsonb_typeofresult='object'"],
  ["harness_tenant_mcp_approvals_shape_check", "execution_state='pending'"],
]);

function normalizeConstraint(value: string): string {
  return value.toLowerCase()
    .replace(/::(?:text|jsonb)/g, "")
    .replace(/[()\s]/g, "");
}

export function assertTenantBrokerApprovalCatalog(catalog: {
  columns: Array<{ table_name: string; column_name: string; data_type: string; is_nullable: string }>;
  constraints: Array<{ table_name: string; constraint_name?: string; definition: string }>;
  indexes: Array<{ indexname: string; indexdef: string }>;
}): void {
  const columns = new Map(
    catalog.columns
      .filter((column) => column.table_name === "harness_tenant_mcp_approvals")
      .map((column) => [column.column_name, [column.data_type.toLowerCase(), column.is_nullable] as const]),
  );
  const missing = [...REQUIRED_APPROVAL_COLUMNS].filter(([name, expected]) => {
    const actual = columns.get(name);
    return !actual || actual[0] !== expected[0] || actual[1] !== expected[1];
  }).map(([name]) => `harness_tenant_mcp_approvals.${name}`);
  const expiryIndex = catalog.indexes.find(
    (index) => index.indexname === "idx_harness_tenant_mcp_approvals_expiry",
  );
  const definition = expiryIndex?.indexdef.toLowerCase().replace(/\s+/g, " ") ?? "";
  if (!definition.includes("harness_tenant_mcp_approvals") || !definition.includes("(expires_at)")) {
    missing.push("idx_harness_tenant_mcp_approvals_expiry");
  }
  const constraints = new Map(
    catalog.constraints
      .filter((constraint) => constraint.table_name === "harness_tenant_mcp_approvals")
      .map((constraint) => [constraint.constraint_name ?? "", normalizeConstraint(constraint.definition)]),
  );
  for (const [name, fragment] of REQUIRED_APPROVAL_CONSTRAINTS) {
    const actual = constraints.get(name);
    if (!actual || !actual.includes(fragment)) missing.push(`harness_tenant_mcp_approvals.${name}`);
  }
  const shape = constraints.get("harness_tenant_mcp_approvals_shape_check") ?? "";
  const stateShapes = new Map<string, string>([
    ["pending", "execution_state='pending'andexecution_claim_idisnullandexecution_started_atisnullandexecution_deadline_atisnullandapproved_atisnullandresultisnullandcompleted_atisnullanduncertain_atisnull"],
    ["approved", "execution_state='approved'andexecution_claim_idisnullandapproved_atisnotnullandexecution_started_atisnullandexecution_deadline_atisnullandresultisnullandcompleted_atisnullanduncertain_atisnull"],
    ["executing", "execution_state='executing'andexecution_claim_idisnotnullandapproved_atisnotnullandexecution_started_atisnotnullandexecution_deadline_atisnotnullandresultisnullandcompleted_atisnullanduncertain_atisnull"],
    ["completed", "execution_state='completed'andexecution_claim_idisnotnullandapproved_atisnotnullandexecution_started_atisnotnullandexecution_deadline_atisnotnullandresultisnotnullandcompleted_atisnotnullanduncertain_atisnull"],
    ["uncertain", "execution_state='uncertain'andexecution_claim_idisnotnullandapproved_atisnotnullandexecution_started_atisnotnullandexecution_deadline_atisnotnullandresultisnullandcompleted_atisnullanduncertain_atisnotnull"],
  ]);
  for (const [state, fragment] of stateShapes) {
    if (!shape.includes(fragment)) missing.push(`harness_tenant_mcp_approvals.shape.${state}`);
  }
  for (const fragment of [
    "result-'content'-'artifact_ids'-'receipt_id'='{}'",
    "jsonb_typeofresult->'content'='string'",
    "notresult?'artifact_ids'orjsonb_typeofresult->'artifact_ids'='array'",
    "notresult?'receipt_id'orjsonb_typeofresult->'receipt_id'='string'",
  ]) {
    const actual = constraints.get("harness_tenant_mcp_approvals_result_check") ?? "";
    if (!actual.includes(fragment)) missing.push(`harness_tenant_mcp_approvals.result.${fragment}`);
  }
  if (missing.length > 0) {
    throw new Error(`PostgreSQL tenant MCP approval schema is incomplete: ${missing.join(", ")}`);
  }
}

export type LeafTenantBrokerApprovalReview =
  | { state: "pending" | "approved" | "executing" | "uncertain"; binding: TenantBrokerPendingApprovalBinding }
  | { state: "completed"; binding: TenantBrokerPendingApprovalBinding; receipt: TenantBrokerCompletionReceipt };

export interface LeafTenantBrokerApprovalStore extends TenantBrokerApprovalStore {
  review(input: {
    approval_id: string;
    argument_digest: string;
    tenant_id: string;
    subject_id: string;
    now_ms: number;
  }): Promise<LeafTenantBrokerApprovalReview | null>;
}

function sameIdentity(a: StandardServiceIdentity, b: StandardServiceIdentity): boolean {
  return a.tenant_id === b.tenant_id
    && a.subject_id === b.subject_id
    && a.session_id === b.session_id
    && a.authority_turn_id === b.authority_turn_id
    && a.subscription_mount_id === b.subscription_mount_id
    && a.runner_profile_id === b.runner_profile_id;
}

function validIdentity(value: unknown): value is StandardServiceIdentity {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const identity = value as StandardServiceIdentity;
  return [
    identity.tenant_id,
    identity.subject_id,
    identity.session_id,
    identity.authority_turn_id,
    identity.subscription_mount_id,
    identity.runner_profile_id,
  ].every((item) => typeof item === "string" && ID.test(item))
    && (identity.runner_profile_id === "author" || identity.runner_profile_id === "spine");
}

function validBinding(value: unknown): value is TenantBrokerPendingApprovalBinding {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const binding = value as TenantBrokerPendingApprovalBinding;
  return APPROVAL_ID.test(binding.approval_id)
    && DIGEST.test(binding.argument_digest)
    && canonicalExpiryMillis(binding.expires_at) !== null
    && validIdentity(binding.identity)
    && typeof binding.call?.service_id === "string"
    && ID.test(binding.call.service_id)
    && typeof binding.call?.tool_id === "string"
    && ID.test(binding.call.tool_id)
    && Boolean(binding.call.arguments)
    && typeof binding.call.arguments === "object"
    && !Array.isArray(binding.call.arguments);
}

function validReceipt(value: unknown): value is TenantBrokerCompletionReceipt {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const result = value as Record<string, unknown>;
  if (Object.keys(result).some((key) => !["content", "artifact_ids", "receipt_id"].includes(key))) return false;
  if (typeof result.content !== "string" || Buffer.byteLength(result.content, "utf8") > MAX_RECEIPT_BYTES) return false;
  if (result.receipt_id !== undefined && (typeof result.receipt_id !== "string" || !APPROVAL_ID.test(result.receipt_id))) return false;
  return result.artifact_ids === undefined
    || validStandardServiceArtifactIds(result.artifact_ids);
}

function validReviewInput(input: Parameters<LeafTenantBrokerApprovalStore["review"]>[0]): boolean {
  return APPROVAL_ID.test(input.approval_id)
    && DIGEST.test(input.argument_digest)
    && ID.test(input.tenant_id)
    && ID.test(input.subject_id)
    && Number.isFinite(input.now_ms);
}

function reviewMatches(binding: TenantBrokerPendingApprovalBinding, input: Parameters<LeafTenantBrokerApprovalStore["review"]>[0]): boolean {
  return binding.approval_id === input.approval_id
    && binding.argument_digest === input.argument_digest
    && binding.identity.tenant_id === input.tenant_id
    && binding.identity.subject_id === input.subject_id;
}

interface ApprovalRow extends QueryResultRow {
  approval_id: string;
  tenant_id: string;
  subject_id: string;
  session_id: string;
  authority_turn_id: string;
  subscription_mount_id: string;
  runner_profile_id: "author" | "spine";
  service_id: string;
  tool_id: string;
  arguments: Record<string, unknown>;
  argument_digest: string;
  expires_at: Date | string;
  execution_state: "pending" | "approved" | "executing" | "completed" | "uncertain";
  execution_deadline_at: Date | string | null;
  result: TenantBrokerCompletionReceipt | null;
  claim_outcome?: "claimed" | "pending" | "executing" | "completed" | "uncertain";
}

const ROW_COLUMNS = `approval_id, tenant_id, subject_id, session_id, authority_turn_id,
  subscription_mount_id, runner_profile_id, service_id, tool_id, arguments,
  argument_digest, expires_at, execution_state, execution_deadline_at, result`;

function fromRow(row: ApprovalRow): TenantBrokerPendingApprovalBinding {
  return {
    approval_id: row.approval_id,
    identity: {
      tenant_id: row.tenant_id,
      subject_id: row.subject_id,
      session_id: row.session_id,
      authority_turn_id: row.authority_turn_id,
      subscription_mount_id: row.subscription_mount_id,
      runner_profile_id: row.runner_profile_id,
    },
    call: {
      service_id: row.service_id,
      tool_id: row.tool_id,
      arguments: structuredClone(row.arguments),
    },
    argument_digest: row.argument_digest,
    expires_at: new Date(row.expires_at).toISOString(),
  };
}

function reviewFromRow(row: ApprovalRow): LeafTenantBrokerApprovalReview | null {
  const binding = fromRow(row);
  if (!validBinding(binding)) return null;
  if (row.execution_state === "completed") {
    return validReceipt(row.result) ? { state: "completed", binding, receipt: structuredClone(row.result) } : null;
  }
  return { state: row.execution_state, binding };
}

function claimFromRow(row: ApprovalRow): TenantBrokerApprovalClaimResult | null {
  const binding = fromRow(row);
  if (!validBinding(binding)) return null;
  const state = row.claim_outcome ?? row.execution_state;
  if (state === "claimed") {
    const claimId = (row as ApprovalRow & { execution_claim_id?: string }).execution_claim_id;
    const executionDeadline = Date.parse(String(row.execution_deadline_at ?? ""));
    return typeof claimId === "string" && EXECUTION_CLAIM_ID.test(claimId)
      && Number.isSafeInteger(executionDeadline)
      ? { state, execution_claim_id: claimId, execution_deadline_ms: executionDeadline, binding }
      : null;
  }
  if (state === "completed") {
    return validReceipt(row.result) ? { state, binding, receipt: structuredClone(row.result) } : null;
  }
  if (state === "approved") return null;
  return { state, binding };
}

function identityValues(identity: StandardServiceIdentity): unknown[] {
  return [
    identity.tenant_id,
    identity.subject_id,
    identity.session_id,
    identity.authority_turn_id,
    identity.subscription_mount_id,
    identity.runner_profile_id,
  ];
}

export class PgTenantBrokerApprovalStore implements LeafTenantBrokerApprovalStore {
  constructor(private readonly pool: Pool) {}

  async create(binding: TenantBrokerPendingApprovalBinding): Promise<boolean> {
    if (!validBinding(binding)) return false;
    const result = await this.pool.query(
      `INSERT INTO harness_tenant_mcp_approvals (
         approval_id, tenant_id, subject_id, session_id, authority_turn_id,
         subscription_mount_id, runner_profile_id, service_id, tool_id,
         arguments, argument_digest, expires_at
       ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,$12::timestamptz)
       ON CONFLICT (approval_id) DO NOTHING`,
      [
        binding.approval_id,
        ...identityValues(binding.identity),
        binding.call.service_id,
        binding.call.tool_id,
        JSON.stringify(binding.call.arguments),
        binding.argument_digest,
        binding.expires_at,
      ],
    );
    return result.rowCount === 1;
  }

  async review(input: Parameters<LeafTenantBrokerApprovalStore["review"]>[0]): Promise<LeafTenantBrokerApprovalReview | null> {
    if (!validReviewInput(input)) return null;
    const result = await this.pool.query<ApprovalRow>(
      `WITH stale AS (
         UPDATE harness_tenant_mcp_approvals
         SET execution_state='uncertain', uncertain_at=to_timestamp($5 / 1000.0)
         WHERE approval_id=$1 AND argument_digest=$2 AND tenant_id=$3 AND subject_id=$4
           AND execution_state='executing'
           AND execution_deadline_at <= to_timestamp($5 / 1000.0)
         RETURNING ${ROW_COLUMNS}
       ), selected AS (
         SELECT ${ROW_COLUMNS} FROM stale
         UNION ALL
         SELECT ${ROW_COLUMNS} FROM harness_tenant_mcp_approvals
         WHERE approval_id=$1 AND argument_digest=$2 AND tenant_id=$3 AND subject_id=$4
           AND NOT EXISTS (SELECT 1 FROM stale)
           AND (execution_state IN ('executing','completed','uncertain')
             OR (execution_state IN ('pending','approved') AND expires_at > to_timestamp($5 / 1000.0)))
       ) SELECT * FROM selected LIMIT 1`,
      [input.approval_id, input.argument_digest, input.tenant_id, input.subject_id, input.now_ms],
    );
    return result.rows[0] ? reviewFromRow(result.rows[0]) : null;
  }

  async approve(input: Parameters<TenantBrokerApprovalStore["approve"]>[0]): Promise<boolean> {
    if (!APPROVAL_ID.test(input.approval_id) || !DIGEST.test(input.argument_digest)
      || !validIdentity(input.identity) || !Number.isFinite(input.approved_at_ms)) return false;
    const result = await this.pool.query(
      `WITH updated AS (
         UPDATE harness_tenant_mcp_approvals
         SET execution_state='approved', approved_at=to_timestamp($9 / 1000.0)
         WHERE approval_id=$1 AND tenant_id=$2 AND subject_id=$3 AND session_id=$4
           AND authority_turn_id=$5 AND subscription_mount_id=$6 AND runner_profile_id=$7
           AND argument_digest=$8 AND execution_state='pending'
           AND expires_at > to_timestamp($9 / 1000.0)
         RETURNING 1
       )
       SELECT 1 FROM updated
       UNION ALL
       SELECT 1 FROM harness_tenant_mcp_approvals
       WHERE approval_id=$1 AND tenant_id=$2 AND subject_id=$3 AND session_id=$4
         AND authority_turn_id=$5 AND subscription_mount_id=$6 AND runner_profile_id=$7
         AND argument_digest=$8 AND execution_state IN ('approved','executing','completed','uncertain')
         AND NOT EXISTS (SELECT 1 FROM updated)
       LIMIT 1`,
      [input.approval_id, ...identityValues(input.identity), input.argument_digest, input.approved_at_ms],
    );
    return result.rowCount === 1;
  }

  async claim(input: Parameters<TenantBrokerApprovalStore["claim"]>[0]): Promise<TenantBrokerApprovalClaimResult | null> {
    if (!APPROVAL_ID.test(input.approval_id) || !validIdentity(input.identity)
      || !Number.isFinite(input.now_ms) || !Number.isFinite(input.execution_deadline_ms)
      || input.execution_deadline_ms <= input.now_ms) return null;
    const executionClaimId = randomUUID();
    const result = await this.pool.query<ApprovalRow & { execution_claim_id: string | null }>(
      `WITH stale AS (
         UPDATE harness_tenant_mcp_approvals
         SET execution_state='uncertain', uncertain_at=to_timestamp($8 / 1000.0)
         WHERE approval_id=$1 AND tenant_id=$2 AND subject_id=$3 AND session_id=$4
           AND authority_turn_id=$5 AND subscription_mount_id=$6 AND runner_profile_id=$7
           AND execution_state='executing'
           AND execution_deadline_at <= to_timestamp($8 / 1000.0)
         RETURNING ${ROW_COLUMNS}, execution_claim_id
       ), acquired AS (
         UPDATE harness_tenant_mcp_approvals
         SET execution_state='executing', execution_claim_id=$10,
             execution_started_at=to_timestamp($8 / 1000.0),
             execution_deadline_at=to_timestamp($9 / 1000.0)
         WHERE approval_id=$1 AND tenant_id=$2 AND subject_id=$3 AND session_id=$4
           AND authority_turn_id=$5 AND subscription_mount_id=$6 AND runner_profile_id=$7
           AND execution_state='approved' AND expires_at > to_timestamp($8 / 1000.0)
         RETURNING ${ROW_COLUMNS}, execution_claim_id
       )
       SELECT stale.*, 'uncertain'::text AS claim_outcome FROM stale
       UNION ALL
       SELECT acquired.*, 'claimed'::text AS claim_outcome FROM acquired
       UNION ALL
       SELECT ${ROW_COLUMNS}, execution_claim_id, execution_state::text AS claim_outcome
       FROM harness_tenant_mcp_approvals
       WHERE approval_id=$1 AND tenant_id=$2 AND subject_id=$3 AND session_id=$4
         AND authority_turn_id=$5 AND subscription_mount_id=$6 AND runner_profile_id=$7
         AND execution_state IN ('pending','executing','completed','uncertain')
         AND NOT EXISTS (SELECT 1 FROM stale) AND NOT EXISTS (SELECT 1 FROM acquired)
       LIMIT 1`,
      [input.approval_id, ...identityValues(input.identity), input.now_ms, input.execution_deadline_ms, executionClaimId],
    );
    return result.rows[0] ? claimFromRow(result.rows[0]) : null;
  }

  async complete(input: Parameters<TenantBrokerApprovalStore["complete"]>[0]): Promise<boolean> {
    if (!APPROVAL_ID.test(input.approval_id) || !validIdentity(input.identity)
      || !EXECUTION_CLAIM_ID.test(input.execution_claim_id) || !validReceipt(input.receipt)) return false;
    const result = await this.pool.query(
      `WITH store_clock AS (SELECT clock_timestamp() AS completed_at)
       UPDATE harness_tenant_mcp_approvals
       SET execution_state='completed', result=$9::jsonb, completed_at=store_clock.completed_at
       FROM store_clock
       WHERE approval_id=$1 AND tenant_id=$2 AND subject_id=$3 AND session_id=$4
         AND authority_turn_id=$5 AND subscription_mount_id=$6 AND runner_profile_id=$7
         AND execution_state='executing' AND execution_claim_id=$8
         AND store_clock.completed_at < execution_deadline_at`,
      [input.approval_id, ...identityValues(input.identity), input.execution_claim_id, JSON.stringify(input.receipt)],
    );
    return result.rowCount === 1;
  }

  async markUncertain(input: Parameters<TenantBrokerApprovalStore["markUncertain"]>[0]): Promise<boolean> {
    if (!APPROVAL_ID.test(input.approval_id) || !validIdentity(input.identity)
      || !EXECUTION_CLAIM_ID.test(input.execution_claim_id) || !Number.isFinite(input.failed_at_ms)) return false;
    const result = await this.pool.query(
      `UPDATE harness_tenant_mcp_approvals
       SET execution_state='uncertain', uncertain_at=to_timestamp($9 / 1000.0)
       WHERE approval_id=$1 AND tenant_id=$2 AND subject_id=$3 AND session_id=$4
         AND authority_turn_id=$5 AND subscription_mount_id=$6 AND runner_profile_id=$7
         AND execution_state='executing' AND execution_claim_id=$8`,
      [input.approval_id, ...identityValues(input.identity), input.execution_claim_id, input.failed_at_ms],
    );
    return result.rowCount === 1;
  }
}

interface FileApprovalRecord {
  binding: TenantBrokerPendingApprovalBinding;
  state: "pending" | "approved" | "executing" | "completed" | "uncertain";
  approved_at?: string;
  execution_claim_id?: string;
  execution_started_at?: string;
  execution_deadline_at?: string;
  receipt?: TenantBrokerCompletionReceipt;
  completed_at?: string;
  uncertain_at?: string;
}

function validFileRecord(value: unknown): value is FileApprovalRecord {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const record = value as FileApprovalRecord;
  if (!validBinding(record.binding) || !["pending", "approved", "executing", "completed", "uncertain"].includes(record.state)) return false;
  const hasClaim = typeof record.execution_claim_id === "string" && EXECUTION_CLAIM_ID.test(record.execution_claim_id)
    && Number.isFinite(Date.parse(record.execution_started_at ?? ""))
    && Number.isFinite(Date.parse(record.execution_deadline_at ?? ""));
  if (record.state === "pending") {
    return record.approved_at === undefined && !hasClaim && record.receipt === undefined
      && record.completed_at === undefined && record.uncertain_at === undefined;
  }
  if (!Number.isFinite(Date.parse(record.approved_at ?? ""))) return false;
  if (record.state === "approved") {
    return !hasClaim && record.receipt === undefined
      && record.completed_at === undefined && record.uncertain_at === undefined;
  }
  if (!hasClaim) return false;
  if (record.state === "completed") {
    return validReceipt(record.receipt) && Number.isFinite(Date.parse(record.completed_at ?? ""))
      && record.uncertain_at === undefined;
  }
  if (record.state === "uncertain") {
    return record.receipt === undefined && record.completed_at === undefined
      && Number.isFinite(Date.parse(record.uncertain_at ?? ""));
  }
  return record.receipt === undefined
    && record.completed_at === undefined
    && record.uncertain_at === undefined;
}

/** Durable local-only store. Production uses the PostgreSQL implementation. */
export class FileTenantBrokerApprovalStore implements LeafTenantBrokerApprovalStore {
  constructor(
    private readonly directory: string,
    private readonly now: () => number = Date.now,
  ) {}

  private path(approvalId: string): string {
    if (!APPROVAL_ID.test(approvalId)) throw new Error("invalid tenant MCP approval id");
    return join(this.directory, `${approvalId}.json`);
  }

  private async locked<T>(approvalId: string, action: () => Promise<T>): Promise<T> {
    await mkdir(this.directory, { recursive: true });
    const lock = `${this.path(approvalId)}.lock`;
    let acquired = false;
    for (let attempt = 0; attempt < 25; attempt += 1) {
      try {
        await writeFile(lock, randomUUID(), { flag: "wx", mode: 0o600 });
        acquired = true;
        break;
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
        await delay(4);
      }
    }
    if (!acquired) throw new Error("tenant MCP approval store is unavailable");
    try {
      return await action();
    } finally {
      await unlink(lock).catch(() => {});
    }
  }

  private async read(approvalId: string): Promise<FileApprovalRecord | null> {
    try {
      const value: unknown = JSON.parse(await readFile(this.path(approvalId), "utf8"));
      return validFileRecord(value) ? structuredClone(value) : null;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
      throw error;
    }
  }

  private async write(approvalId: string, record: FileApprovalRecord): Promise<void> {
    if (!validFileRecord(record)) throw new Error("invalid tenant MCP approval record");
    const temporary = join(this.directory, `.${approvalId}.${randomUUID()}.tmp`);
    await writeFile(temporary, JSON.stringify(record), { flag: "wx", mode: 0o600 });
    try {
      await rename(temporary, this.path(approvalId));
    } finally {
      await unlink(temporary).catch(() => {});
    }
  }

  async create(binding: TenantBrokerPendingApprovalBinding): Promise<boolean> {
    if (!validBinding(binding)) return false;
    await mkdir(this.directory, { recursive: true });
    try {
      await writeFile(this.path(binding.approval_id), JSON.stringify({ binding, state: "pending" }), { flag: "wx", mode: 0o600 });
      return true;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "EEXIST") return false;
      throw error;
    }
  }

  review(input: Parameters<LeafTenantBrokerApprovalStore["review"]>[0]) {
    if (!validReviewInput(input)) return Promise.resolve(null);
    return this.locked(input.approval_id, async () => {
      const record = await this.read(input.approval_id);
      if (!record || !reviewMatches(record.binding, input)) return null;
      if (record.state === "executing" && Date.parse(record.execution_deadline_at ?? "") <= input.now_ms) {
        record.state = "uncertain";
        record.uncertain_at = new Date(input.now_ms).toISOString();
        await this.write(input.approval_id, record);
      }
      if (["pending", "approved"].includes(record.state) && Date.parse(record.binding.expires_at) <= input.now_ms) return null;
      return record.state === "completed"
        ? { state: "completed" as const, binding: structuredClone(record.binding), receipt: structuredClone(record.receipt!) }
        : { state: record.state, binding: structuredClone(record.binding) };
    });
  }

  approve(input: Parameters<TenantBrokerApprovalStore["approve"]>[0]) {
    if (!APPROVAL_ID.test(input.approval_id) || !DIGEST.test(input.argument_digest)
      || !validIdentity(input.identity) || !Number.isFinite(input.approved_at_ms)) return Promise.resolve(false);
    return this.locked(input.approval_id, async () => {
      const record = await this.read(input.approval_id);
      if (!record || !sameIdentity(record.binding.identity, input.identity)
        || record.binding.argument_digest !== input.argument_digest) return false;
      if (record.state === "pending") {
        if (Date.parse(record.binding.expires_at) <= input.approved_at_ms) return false;
        record.state = "approved";
        record.approved_at = new Date(input.approved_at_ms).toISOString();
        await this.write(input.approval_id, record);
      }
      return true;
    });
  }

  claim(input: Parameters<TenantBrokerApprovalStore["claim"]>[0]) {
    if (!APPROVAL_ID.test(input.approval_id) || !validIdentity(input.identity)
      || !Number.isFinite(input.now_ms) || !Number.isFinite(input.execution_deadline_ms)
      || input.execution_deadline_ms <= input.now_ms) return Promise.resolve(null);
    return this.locked(input.approval_id, async (): Promise<TenantBrokerApprovalClaimResult | null> => {
      const record = await this.read(input.approval_id);
      if (!record || !sameIdentity(record.binding.identity, input.identity)) return null;
      if (record.state === "executing" && Date.parse(record.execution_deadline_at ?? "") <= input.now_ms) {
        record.state = "uncertain";
        record.uncertain_at = new Date(input.now_ms).toISOString();
        await this.write(input.approval_id, record);
      }
      if (record.state === "approved") {
        if (Date.parse(record.binding.expires_at) <= input.now_ms) return null;
        record.state = "executing";
        record.execution_claim_id = randomUUID();
        record.execution_started_at = new Date(input.now_ms).toISOString();
        record.execution_deadline_at = new Date(input.execution_deadline_ms).toISOString();
        await this.write(input.approval_id, record);
        return {
          state: "claimed",
          execution_claim_id: record.execution_claim_id,
          execution_deadline_ms: input.execution_deadline_ms,
          binding: structuredClone(record.binding),
        };
      }
      if (record.state === "completed") {
        return { state: "completed", binding: structuredClone(record.binding), receipt: structuredClone(record.receipt!) };
      }
      return { state: record.state, binding: structuredClone(record.binding) };
    });
  }

  complete(input: Parameters<TenantBrokerApprovalStore["complete"]>[0]) {
    if (!APPROVAL_ID.test(input.approval_id) || !validIdentity(input.identity)
      || !EXECUTION_CLAIM_ID.test(input.execution_claim_id) || !validReceipt(input.receipt)) return Promise.resolve(false);
    return this.locked(input.approval_id, async () => {
      const record = await this.read(input.approval_id);
      const completedAtMs = this.now();
      if (!record || record.state !== "executing" || !sameIdentity(record.binding.identity, input.identity)
        || record.execution_claim_id !== input.execution_claim_id
        || !Number.isFinite(completedAtMs)
        || completedAtMs >= Date.parse(record.execution_deadline_at ?? "")) return false;
      record.state = "completed";
      record.receipt = structuredClone(input.receipt);
      record.completed_at = new Date(completedAtMs).toISOString();
      await this.write(input.approval_id, record);
      return true;
    });
  }

  markUncertain(input: Parameters<TenantBrokerApprovalStore["markUncertain"]>[0]) {
    if (!APPROVAL_ID.test(input.approval_id) || !validIdentity(input.identity)
      || !EXECUTION_CLAIM_ID.test(input.execution_claim_id) || !Number.isFinite(input.failed_at_ms)) return Promise.resolve(false);
    return this.locked(input.approval_id, async () => {
      const record = await this.read(input.approval_id);
      if (!record || record.state !== "executing" || !sameIdentity(record.binding.identity, input.identity)
        || record.execution_claim_id !== input.execution_claim_id) return false;
      record.state = "uncertain";
      record.uncertain_at = new Date(input.failed_at_ms).toISOString();
      await this.write(input.approval_id, record);
      return true;
    });
  }
}

export interface TenantBrokerApprovalStoreHandle {
  store: LeafTenantBrokerApprovalStore;
  close(): Promise<void>;
}

export function createTenantBrokerApprovalStore(env: NodeJS.ProcessEnv = process.env): TenantBrokerApprovalStoreHandle {
  const kind = (env.LEAF_HARNESS_SESSION_STORE ?? "file").trim().toLowerCase();
  if (kind === "postgres") {
    const connectionString = (env.LEAF_HARNESS_DATABASE_URL ?? env.DATABASE_URL ?? "").trim();
    if (!connectionString) throw new Error("tenant MCP approval store requires the harness PostgreSQL URL");
    const pool = new Pool({ connectionString, application_name: "leaf-tenant-mcp-approvals" });
    return { store: new PgTenantBrokerApprovalStore(pool), close: () => pool.end() };
  }
  const runtime = (env.LEAF_RUNTIME_ENV ?? "local").trim().toLowerCase();
  if (!["local", "development", "test"].includes(runtime)) {
    throw new Error("tenant MCP approval store requires PostgreSQL outside local mode");
  }
  const directory = (env.LEAF_TENANT_MCP_APPROVALS_DIR ?? "C:/tmp/leaf-tenant-mcp-approvals").trim();
  if (!directory) throw new Error("LEAF_TENANT_MCP_APPROVALS_DIR is invalid");
  return { store: new FileTenantBrokerApprovalStore(directory), close: async () => {} };
}
