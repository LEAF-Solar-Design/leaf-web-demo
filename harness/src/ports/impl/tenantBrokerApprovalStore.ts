import { randomUUID } from "node:crypto";
import { mkdir, readFile, rename, unlink, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { Pool } from "pg";
import type { QueryResultRow } from "pg";

import type {
  StandardServiceIdentity,
  TenantBrokerApprovalStore,
  TenantBrokerPendingApprovalBinding,
} from "../../vendor/mushy-author/index.js";

const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;
const APPROVAL_ID = /^[A-Za-z0-9_-]{8,256}$/;
const DIGEST = /^[a-f0-9]{64}$/;

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
]);

export function assertTenantBrokerApprovalCatalog(catalog: {
  columns: Array<{ table_name: string; column_name: string; data_type: string; is_nullable: string }>;
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
  if (
    !definition.includes("harness_tenant_mcp_approvals")
    || !definition.includes("(expires_at)")
  ) {
    missing.push("idx_harness_tenant_mcp_approvals_expiry");
  }
  if (missing.length > 0) {
    throw new Error(`PostgreSQL tenant MCP approval schema is incomplete: ${missing.join(", ")}`);
  }
}

export interface LeafTenantBrokerApprovalStore extends TenantBrokerApprovalStore {
  review(input: {
    approval_id: string;
    argument_digest: string;
    tenant_id: string;
    subject_id: string;
    now_ms: number;
  }): Promise<TenantBrokerPendingApprovalBinding | null>;
}

function sameIdentity(a: StandardServiceIdentity, b: StandardServiceIdentity): boolean {
  return a.tenant_id === b.tenant_id
    && a.subject_id === b.subject_id
    && a.session_id === b.session_id
    && a.authority_turn_id === b.authority_turn_id
    && a.subscription_mount_id === b.subscription_mount_id
    && a.runner_profile_id === b.runner_profile_id;
}

function validBinding(value: unknown): value is TenantBrokerPendingApprovalBinding {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const binding = value as TenantBrokerPendingApprovalBinding;
  const identity = binding.identity;
  return APPROVAL_ID.test(binding.approval_id)
    && DIGEST.test(binding.argument_digest)
    && Number.isFinite(Date.parse(binding.expires_at))
    && Boolean(identity)
    && [
      identity.tenant_id,
      identity.subject_id,
      identity.session_id,
      identity.authority_turn_id,
      identity.subscription_mount_id,
      identity.runner_profile_id,
      binding.call?.service_id,
      binding.call?.tool_id,
    ].every((item) => typeof item === "string" && ID.test(item))
    && (identity.runner_profile_id === "author" || identity.runner_profile_id === "spine")
    && Boolean(binding.call?.arguments)
    && typeof binding.call.arguments === "object"
    && !Array.isArray(binding.call.arguments);
}

function reviewMatches(
  binding: TenantBrokerPendingApprovalBinding,
  input: Parameters<LeafTenantBrokerApprovalStore["review"]>[0],
): boolean {
  return binding.approval_id === input.approval_id
    && binding.argument_digest === input.argument_digest
    && binding.identity.tenant_id === input.tenant_id
    && binding.identity.subject_id === input.subject_id
    && Date.parse(binding.expires_at) > input.now_ms;
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
}

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
        binding.identity.tenant_id,
        binding.identity.subject_id,
        binding.identity.session_id,
        binding.identity.authority_turn_id,
        binding.identity.subscription_mount_id,
        binding.identity.runner_profile_id,
        binding.call.service_id,
        binding.call.tool_id,
        JSON.stringify(binding.call.arguments),
        binding.argument_digest,
        binding.expires_at,
      ],
    );
    return result.rowCount === 1;
  }

  async review(
    input: Parameters<LeafTenantBrokerApprovalStore["review"]>[0],
  ): Promise<TenantBrokerPendingApprovalBinding | null> {
    if (
      !APPROVAL_ID.test(input.approval_id)
      || !DIGEST.test(input.argument_digest)
      || !ID.test(input.tenant_id)
      || !ID.test(input.subject_id)
      || !Number.isFinite(input.now_ms)
    ) return null;
    const result = await this.pool.query<ApprovalRow>(
      `SELECT approval_id, tenant_id, subject_id, session_id, authority_turn_id,
              subscription_mount_id, runner_profile_id, service_id, tool_id,
              arguments, argument_digest, expires_at
       FROM harness_tenant_mcp_approvals
       WHERE approval_id=$1 AND argument_digest=$2 AND tenant_id=$3 AND subject_id=$4
         AND expires_at > to_timestamp($5 / 1000.0)`,
      [input.approval_id, input.argument_digest, input.tenant_id, input.subject_id, input.now_ms],
    );
    const row = result.rows[0];
    return row ? fromRow(row) : null;
  }

  async consume(input: {
    approval_id: string;
    identity: StandardServiceIdentity;
    now_ms: number;
  }): Promise<TenantBrokerPendingApprovalBinding | null> {
    if (!APPROVAL_ID.test(input.approval_id) || !Number.isFinite(input.now_ms)) return null;
    const identity = input.identity;
    const result = await this.pool.query<ApprovalRow>(
      `DELETE FROM harness_tenant_mcp_approvals
       WHERE approval_id=$1 AND tenant_id=$2 AND subject_id=$3 AND session_id=$4
         AND authority_turn_id=$5 AND subscription_mount_id=$6 AND runner_profile_id=$7
         AND expires_at > to_timestamp($8 / 1000.0)
       RETURNING approval_id, tenant_id, subject_id, session_id, authority_turn_id,
                 subscription_mount_id, runner_profile_id, service_id, tool_id,
                 arguments, argument_digest, expires_at`,
      [
        input.approval_id,
        identity.tenant_id,
        identity.subject_id,
        identity.session_id,
        identity.authority_turn_id,
        identity.subscription_mount_id,
        identity.runner_profile_id,
        input.now_ms,
      ],
    );
    const row = result.rows[0];
    return row ? fromRow(row) : null;
  }
}

/** Durable local-only store. Production uses the PostgreSQL implementation. */
export class FileTenantBrokerApprovalStore implements LeafTenantBrokerApprovalStore {
  private gate: Promise<void> = Promise.resolve();

  constructor(private readonly directory: string) {}

  private path(approvalId: string): string {
    if (!APPROVAL_ID.test(approvalId)) throw new Error("invalid tenant MCP approval id");
    return join(this.directory, `${approvalId}.json`);
  }

  private async exclusive<T>(action: () => Promise<T>): Promise<T> {
    const previous = this.gate;
    let release!: () => void;
    this.gate = new Promise<void>((resolve) => { release = resolve; });
    await previous;
    try {
      return await action();
    } finally {
      release();
    }
  }

  async create(binding: TenantBrokerPendingApprovalBinding): Promise<boolean> {
    if (!validBinding(binding)) return false;
    await mkdir(this.directory, { recursive: true });
    try {
      await writeFile(this.path(binding.approval_id), JSON.stringify(binding), { flag: "wx", mode: 0o600 });
      return true;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "EEXIST") return false;
      throw error;
    }
  }

  private async read(approvalId: string): Promise<TenantBrokerPendingApprovalBinding | null> {
    try {
      const value: unknown = JSON.parse(await readFile(this.path(approvalId), "utf8"));
      return validBinding(value) ? structuredClone(value) : null;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
      throw error;
    }
  }

  review(input: Parameters<LeafTenantBrokerApprovalStore["review"]>[0]) {
    return this.exclusive(async () => {
      const binding = await this.read(input.approval_id);
      return binding && reviewMatches(binding, input) ? binding : null;
    });
  }

  consume(input: {
    approval_id: string;
    identity: StandardServiceIdentity;
    now_ms: number;
  }) {
    return this.exclusive(async () => {
      const binding = await this.read(input.approval_id);
      if (
        !binding
        || Date.parse(binding.expires_at) <= input.now_ms
        || !sameIdentity(binding.identity, input.identity)
      ) return null;
      const claim = join(this.directory, `.${input.approval_id}.${randomUUID()}.claimed`);
      try {
        await rename(this.path(input.approval_id), claim);
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
        throw error;
      }
      await unlink(claim).catch(() => {});
      return binding;
    });
  }
}

export interface TenantBrokerApprovalStoreHandle {
  store: LeafTenantBrokerApprovalStore;
  close(): Promise<void>;
}

export function createTenantBrokerApprovalStore(
  env: NodeJS.ProcessEnv = process.env,
): TenantBrokerApprovalStoreHandle {
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
