import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type { Pool } from "pg";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LeafStandardServicesHumanApprovalHost } from "../src/ports/impl/leafStandardServicesResolver.js";
import {
  assertTenantBrokerApprovalCatalog,
  createTenantBrokerApprovalStore,
  FileTenantBrokerApprovalStore,
  PgTenantBrokerApprovalStore,
} from "../src/ports/impl/tenantBrokerApprovalStore.js";
import {
  validStandardServiceArtifactIds,
} from "../src/ports/impl/standardServiceArtifactContract.js";
import { tenantBrokerApprovalDigest } from "../src/vendor/mushy-author/index.js";

const directories: string[] = [];
const identity = {
  tenant_id: "tenant-a",
  subject_id: "auth0:alice",
  session_id: "session-a",
  authority_turn_id: "turn-a",
  subscription_mount_id: "mount-a",
  runner_profile_id: "spine" as const,
};
const call = { service_id: "preview", tool_id: "create", arguments: { target: "roof-a" } };

async function store(now: () => number = () => 1_200) {
  const directory = await mkdtemp(join(tmpdir(), "leaf-mcp-approvals-"));
  directories.push(directory);
  return new FileTenantBrokerApprovalStore(directory, now);
}

function binding(overrides: Record<string, unknown> = {}) {
  const argumentDigest = tenantBrokerApprovalDigest(identity, call);
  return {
    approval_id: "approval_12345678",
    identity: structuredClone(identity),
    call: structuredClone(call),
    argument_digest: argumentDigest,
    expires_at: "2099-01-01T00:00:00.000Z",
    ...overrides,
  };
}

function reviewInput(pending = binding(), now_ms = 1_000) {
  return {
    approval_id: pending.approval_id,
    argument_digest: pending.argument_digest,
    tenant_id: pending.identity.tenant_id,
    subject_id: pending.identity.subject_id,
    now_ms,
  };
}

afterEach(async () => {
  await Promise.all(directories.splice(0).map((directory) => rm(directory, { recursive: true, force: true })));
});

describe("durable tenant broker approval store", () => {
  it("matches the provider artifact id and count contract exactly", () => {
    const leadingIds = ["-aaaaaaaaaaaaaaa", "_bbbbbbbbbbbbbbb"];
    expect(leadingIds.every((artifactId) => artifactId.length === 16)).toBe(true);
    expect(validStandardServiceArtifactIds(leadingIds)).toBe(true);
    expect(validStandardServiceArtifactIds(
      Array.from({ length: 64 }, (_value, index) => `artifact_${String(index).padStart(8, "0")}`),
    )).toBe(true);
    expect(validStandardServiceArtifactIds(["artifact-short"])).toBe(false);
    expect(validStandardServiceArtifactIds([".artifact00000000"])).toBe(false);
    expect(validStandardServiceArtifactIds(
      Array.from({ length: 65 }, (_value, index) => `artifact_${String(index).padStart(8, "0")}`),
    )).toBe(false);
  });

  it("fails closed on file storage outside local mode", () => {
    expect(() => createTenantBrokerApprovalStore({
      LEAF_HARNESS_SESSION_STORE: "file",
      LEAF_RUNTIME_ENV: "production",
    })).toThrow("requires PostgreSQL");
  });

  it("requires every state column, named check, exact primary key, and expiry index", () => {
    const nonNullable = [
      ["approval_id", "text"], ["tenant_id", "text"], ["subject_id", "text"],
      ["session_id", "text"], ["authority_turn_id", "text"],
      ["subscription_mount_id", "text"], ["runner_profile_id", "text"],
      ["service_id", "text"], ["tool_id", "text"], ["arguments", "jsonb"],
      ["argument_digest", "text"], ["expires_at", "timestamp with time zone"],
      ["created_at", "timestamp with time zone"], ["execution_state", "text"],
    ];
    const nullable = [
      ["approved_at", "timestamp with time zone"], ["execution_claim_id", "text"],
      ["execution_started_at", "timestamp with time zone"],
      ["execution_deadline_at", "timestamp with time zone"], ["result", "jsonb"],
      ["completed_at", "timestamp with time zone"], ["uncertain_at", "timestamp with time zone"],
    ];
    const columns = [...nonNullable.map(([column_name, data_type]) => ({
      table_name: "harness_tenant_mcp_approvals", column_name, data_type, is_nullable: "NO",
    })), ...nullable.map(([column_name, data_type]) => ({
      table_name: "harness_tenant_mcp_approvals", column_name, data_type, is_nullable: "YES",
    }))];
    const constraints = [
      ["harness_tenant_mcp_approvals_pkey", "PRIMARY KEY (approval_id)"],
      ["harness_tenant_mcp_approvals_id_check", "CHECK (approval_id ~ '^[A-Za-z0-9_-]{8,256}$'::text)"],
      ["harness_tenant_mcp_approvals_profile_check", "CHECK (runner_profile_id = ANY (ARRAY['author'::text, 'spine'::text]))"],
      ["harness_tenant_mcp_approvals_arguments_check", "CHECK (jsonb_typeof(arguments) = 'object'::text)"],
      ["harness_tenant_mcp_approvals_digest_check", "CHECK (argument_digest ~ '^[a-f0-9]{64}$'::text)"],
      ["harness_tenant_mcp_approvals_state_check", "CHECK (execution_state = ANY (ARRAY['pending'::text, 'approved'::text, 'executing'::text, 'completed'::text, 'uncertain'::text]))"],
      ["harness_tenant_mcp_approvals_claim_check", "CHECK (execution_claim_id IS NULL OR execution_claim_id ~ '^[A-Za-z0-9_-]{16,256}$'::text)"],
      ["harness_tenant_mcp_approvals_result_check", "CHECK (result IS NULL OR (jsonb_typeof(result) = 'object'::text AND jsonb_typeof(result -> 'content'::text) = 'string'::text AND result - 'content'::text - 'artifact_ids'::text - 'receipt_id'::text = '{}'::jsonb AND (NOT result ? 'artifact_ids'::text OR jsonb_typeof(result -> 'artifact_ids'::text) = 'array'::text) AND (NOT result ? 'receipt_id'::text OR jsonb_typeof(result -> 'receipt_id'::text) = 'string'::text)))"],
      ["harness_tenant_mcp_approvals_shape_check", "CHECK ((execution_state = 'pending'::text AND execution_claim_id IS NULL AND execution_started_at IS NULL AND execution_deadline_at IS NULL AND approved_at IS NULL AND result IS NULL AND completed_at IS NULL AND uncertain_at IS NULL) OR (execution_state = 'approved'::text AND execution_claim_id IS NULL AND approved_at IS NOT NULL AND execution_started_at IS NULL AND execution_deadline_at IS NULL AND result IS NULL AND completed_at IS NULL AND uncertain_at IS NULL) OR (execution_state = 'executing'::text AND execution_claim_id IS NOT NULL AND approved_at IS NOT NULL AND execution_started_at IS NOT NULL AND execution_deadline_at IS NOT NULL AND result IS NULL AND completed_at IS NULL AND uncertain_at IS NULL) OR (execution_state = 'completed'::text AND execution_claim_id IS NOT NULL AND approved_at IS NOT NULL AND execution_started_at IS NOT NULL AND execution_deadline_at IS NOT NULL AND result IS NOT NULL AND completed_at IS NOT NULL AND uncertain_at IS NULL) OR (execution_state = 'uncertain'::text AND execution_claim_id IS NOT NULL AND approved_at IS NOT NULL AND execution_started_at IS NOT NULL AND execution_deadline_at IS NOT NULL AND result IS NULL AND completed_at IS NULL AND uncertain_at IS NOT NULL))"],
    ].map(([constraint_name, definition]) => ({
      table_name: "harness_tenant_mcp_approvals", constraint_name, definition,
    }));
    const indexes = [{
      indexname: "idx_harness_tenant_mcp_approvals_expiry",
      indexdef: "CREATE INDEX idx_harness_tenant_mcp_approvals_expiry ON public.harness_tenant_mcp_approvals USING btree (expires_at)",
    }];
    const catalog = { columns, constraints, indexes };

    expect(() => assertTenantBrokerApprovalCatalog(catalog)).not.toThrow();
    expect(() => assertTenantBrokerApprovalCatalog({ ...catalog, columns: columns.slice(1) })).toThrow("schema is incomplete");
    expect(() => assertTenantBrokerApprovalCatalog({ ...catalog, indexes: [] })).toThrow("schema is incomplete");
    for (let index = 0; index < constraints.length; index += 1) {
      expect(() => assertTenantBrokerApprovalCatalog({
        ...catalog,
        constraints: constraints.filter((_value, candidate) => candidate !== index),
      })).toThrow("schema is incomplete");
    }
    expect(() => assertTenantBrokerApprovalCatalog({
      ...catalog,
      constraints: constraints.map((constraint) => constraint.constraint_name === "harness_tenant_mcp_approvals_pkey"
        ? { ...constraint, definition: "UNIQUE (approval_id, tenant_id)" }
        : constraint),
    })).toThrow("pkey");
  });

  it("uses atomic PostgreSQL transitions and persists the returned claim lease and receipt", async () => {
    const pending = binding();
    const row = {
      approval_id: pending.approval_id,
      tenant_id: identity.tenant_id,
      subject_id: identity.subject_id,
      session_id: identity.session_id,
      authority_turn_id: identity.authority_turn_id,
      subscription_mount_id: identity.subscription_mount_id,
      runner_profile_id: identity.runner_profile_id,
      service_id: call.service_id,
      tool_id: call.tool_id,
      arguments: call.arguments,
      argument_digest: pending.argument_digest,
      expires_at: pending.expires_at,
      execution_state: "pending",
      execution_deadline_at: null,
      result: null,
    };
    const query = vi.fn(async (sql: string) => {
      if (sql.includes("INSERT INTO")) return { rowCount: 1, rows: [] };
      if (sql.includes("WITH stale AS") && sql.includes("argument_digest=$2")) return { rowCount: 1, rows: [row] };
      if (sql.includes("SET execution_state='approved'")) return { rowCount: 1, rows: [{ ok: 1 }] };
      if (sql.includes("'claimed'::text AS claim_outcome")) return {
        rowCount: 1,
        rows: [{ ...row, execution_state: "executing", execution_deadline_at: "1970-01-01T00:02:01.100Z", execution_claim_id: "claim_1234567890123456", claim_outcome: "claimed" }],
      };
      if (sql.includes("SET execution_state='completed'")) return { rowCount: 1, rows: [] };
      if (sql.includes("SET execution_state='uncertain'")) return { rowCount: 1, rows: [] };
      return { rowCount: 1, rows: [row] };
    });
    const approvals = new PgTenantBrokerApprovalStore({ query } as unknown as Pool);
    const receipt = { content: "{\"safe\":true}" };

    expect(await approvals.create(pending)).toBe(true);
    expect(await approvals.review(reviewInput(pending))).toEqual({ state: "pending", binding: pending });
    expect(await approvals.approve({
      approval_id: pending.approval_id, identity, argument_digest: pending.argument_digest, approved_at_ms: 1_000,
    })).toBe(true);
    const claimed = await approvals.claim({
      approval_id: pending.approval_id, identity, now_ms: 1_100, execution_deadline_ms: 121_100,
    });
    expect(claimed).toMatchObject({
      state: "claimed",
      execution_claim_id: "claim_1234567890123456",
      execution_deadline_ms: 121_100,
    });
    expect(await approvals.complete({
      approval_id: pending.approval_id, identity, execution_claim_id: "claim_1234567890123456",
      receipt,
    })).toBe(true);
    expect(await approvals.markUncertain({
      approval_id: pending.approval_id, identity, execution_claim_id: "claim_1234567890123456", failed_at_ms: 1_300,
    })).toBe(true);

    const sql = query.mock.calls.map((callValue) => String(callValue[0])).join("\n");
    expect(sql).toContain("ON CONFLICT (approval_id) DO NOTHING");
    expect(sql).toContain("execution_state='approved'");
    expect(sql).toContain("execution_state='executing'");
    expect(sql).toContain("execution_deadline_at=to_timestamp($9 / 1000.0)");
    expect(sql).toContain("execution_claim_id=$8");
    expect(sql).toContain("clock_timestamp() AS completed_at");
    expect(sql).toContain("store_clock.completed_at < execution_deadline_at");
    expect(sql).not.toContain("DELETE FROM harness_tenant_mcp_approvals");
  });

  it("rehydrates across instances, claims once, and returns the durable completed receipt", async () => {
    let storeNow = 1_200;
    const first = await store(() => storeNow);
    const second = new FileTenantBrokerApprovalStore(
      (first as unknown as { directory: string }).directory,
      () => storeNow,
    );
    const pending = binding();
    const receipt = {
      content: "{\"safe\":true}",
      artifact_ids: ["-aaaaaaaaaaaaaaa", "_bbbbbbbbbbbbbbb"],
    };
    expect(await first.create(pending)).toBe(true);
    expect(await first.create(pending)).toBe(false);
    expect(await second.review(reviewInput(pending))).toEqual({ state: "pending", binding: pending });

    expect(await first.claim({
      approval_id: pending.approval_id, identity, now_ms: 1_000, execution_deadline_ms: 121_000,
    })).toMatchObject({ state: "pending" });
    expect(await first.approve({
      approval_id: pending.approval_id, identity, argument_digest: pending.argument_digest, approved_at_ms: 1_000,
    })).toBe(true);
    expect(await second.approve({
      approval_id: pending.approval_id, identity, argument_digest: pending.argument_digest, approved_at_ms: 1_001,
    })).toBe(true);

    const claims = await Promise.all([first, second].map((approvals) => approvals.claim({
      approval_id: pending.approval_id, identity, now_ms: 1_100, execution_deadline_ms: 121_100,
    })));
    const claimed = claims.find((candidate) => candidate?.state === "claimed");
    expect(claims.filter((candidate) => candidate?.state === "claimed")).toHaveLength(1);
    expect(claims.filter((candidate) => candidate?.state === "executing")).toHaveLength(1);
    expect(claimed?.state).toBe("claimed");
    if (!claimed || claimed.state !== "claimed") throw new Error("missing test claim");
    expect(claimed.execution_deadline_ms).toBe(121_100);

    expect(await second.complete({
      approval_id: pending.approval_id, identity, execution_claim_id: "wrong_claim_12345678", receipt,
    })).toBe(false);
    storeNow = claimed.execution_deadline_ms;
    expect(await second.complete({
      approval_id: pending.approval_id, identity, execution_claim_id: claimed.execution_claim_id,
      receipt,
    })).toBe(false);
    storeNow = 1_200;
    expect(await second.complete({
      approval_id: pending.approval_id, identity, execution_claim_id: claimed.execution_claim_id, receipt,
    })).toBe(true);
    expect(await first.claim({
      approval_id: pending.approval_id, identity, now_ms: 2_000, execution_deadline_ms: 122_000,
    })).toEqual({ state: "completed", binding: pending, receipt });
    expect(await second.review(reviewInput(pending, 2_000))).toEqual({ state: "completed", binding: pending, receipt });
  });

  it("does not let identity swaps consume authority and makes a stale execution permanently uncertain", async () => {
    const approvals = await store();
    const pending = binding();
    await approvals.create(pending);
    for (const wrong of [
      { ...identity, tenant_id: "tenant-b" },
      { ...identity, subject_id: "auth0:bob" },
      { ...identity, session_id: "session-b" },
      { ...identity, authority_turn_id: "turn-b" },
      { ...identity, subscription_mount_id: "mount-b" },
      { ...identity, runner_profile_id: "author" as const },
    ]) {
      expect(await approvals.approve({
        approval_id: pending.approval_id, identity: wrong, argument_digest: pending.argument_digest, approved_at_ms: 1_000,
      })).toBe(false);
    }
    await approvals.approve({
      approval_id: pending.approval_id, identity, argument_digest: pending.argument_digest, approved_at_ms: 1_000,
    });
    expect(await approvals.claim({
      approval_id: pending.approval_id, identity, now_ms: 1_100, execution_deadline_ms: 1_500,
    })).toMatchObject({ state: "claimed" });
    expect(await approvals.claim({
      approval_id: pending.approval_id, identity, now_ms: 1_500, execution_deadline_ms: 2_000,
    })).toEqual({ state: "uncertain", binding: pending });
    expect(await approvals.claim({
      approval_id: pending.approval_id, identity, now_ms: 5_000, execution_deadline_ms: 6_000,
    })).toEqual({ state: "uncertain", binding: pending });
  });

  it("does not review or approve an expired pending binding", async () => {
    const approvals = await store();
    const pending = binding({ expires_at: "1970-01-01T00:00:01.000Z" });
    expect(await approvals.create(pending)).toBe(true);
    expect(await approvals.review(reviewInput(pending, 2_000))).toBeNull();
    expect(await approvals.approve({
      approval_id: pending.approval_id, identity, argument_digest: pending.argument_digest, approved_at_ms: 2_000,
    })).toBe(false);
  });

  it.each([
    "2099-01-01T00:00:00Z",
    "2099-01-01T00:00:00.000+00:00",
    "2099-02-30T00:00:00.000Z",
    "2099-01-01T00:00:00.000Zextra",
  ])("rejects a non-canonical stored approval expiry %s", async (expires_at) => {
    const approvals = await store();
    expect(await approvals.create(binding({ expires_at }))).toBe(false);
  });
});

describe("human approval host", () => {
  it("forbids provider replacement in deployed environments", async () => {
    const approvals = await store();
    expect(() => new LeafStandardServicesHumanApprovalHost({
      brokerEndpoint: "https://staging-api.leafdesign.ai",
      environment: "staging",
      approvalStore: approvals,
      providerFactory: () => ({
        async recordHumanAuthenticatedApproval() {},
        async confirm() { return { content: "{}" }; },
      }),
    })).toThrow("provider_override_forbidden");
  });

  it("executes only after the human broker action and returns the stored receipt on retry", async () => {
    const approvals = await store();
    const pending = binding();
    await approvals.create(pending);
    const brokerRequests: Array<{ url: string; init?: RequestInit }> = [];
    const toolCalls: Array<{ name: string; args: Record<string, unknown> }> = [];
    const host = new LeafStandardServicesHumanApprovalHost({
      brokerEndpoint: "http://127.0.0.1:18901",
      environment: "local",
      approvalStore: approvals,
      fetchImpl: (async (url, init) => {
        brokerRequests.push({ url: String(url), init });
        return new Response(JSON.stringify({ status: "approved" }), {
          status: 200, headers: { "content-type": "application/json" },
        });
      }) as typeof fetch,
      providerFactory: (options) => ({
        async recordHumanAuthenticatedApproval(approvedIdentity, approvalId, argumentDigest) {
          const approved = await options.approvalStore.approve({
            approval_id: approvalId,
            identity: approvedIdentity,
            argument_digest: argumentDigest,
            approved_at_ms: 1_000,
          });
          if (!approved) throw new Error("binding_invalid");
        },
        async confirm(confirmedIdentity, approvalId) {
          const claimed = await options.approvalStore.claim({
            approval_id: approvalId,
            identity: confirmedIdentity,
            now_ms: 1_000,
            execution_deadline_ms: 121_000,
          });
          if (!claimed || claimed.state !== "claimed") throw new Error("binding_invalid");
          toolCalls.push({ name: "services_confirm", args: { approval_id: approvalId } });
          const result = {
            content: "{\"safe\":true}",
            artifact_ids: ["-aaaaaaaaaaaaaaa", "_bbbbbbbbbbbbbbb"],
          };
          if (!await options.approvalStore.complete({
            approval_id: approvalId,
            identity: confirmedIdentity,
            execution_claim_id: claimed.execution_claim_id,
            receipt: result,
          })) throw new Error("uncertain");
          return result;
        },
      }),
      now: () => 1_000,
    });
    const request = {
      approval_id: pending.approval_id,
      argument_digest: pending.argument_digest,
      identity,
      human_bearer: "human.approval.token.value.1234567890",
      attachment: {
        bearer_token: "attachment.token.value.1234567890",
        channel_secret: "channel-secret-value-1234567890-abcd",
        expires_at: "2099-01-01T00:00:00.000Z",
      },
    };

    for (const hostile of [
      { ...request, human_bearer: { secret: "human" } as unknown as string },
      {
        ...request,
        attachment: {
          ...request.attachment,
          bearer_token: { secret: "attachment" } as unknown as string,
        },
      },
    ]) {
      await expect(host.execute(hostile)).rejects.toThrow("credential_invalid");
    }
    expect(brokerRequests).toHaveLength(0);
    expect(toolCalls).toHaveLength(0);
    await expect(host.execute({ ...request, identity: { ...identity, authority_turn_id: "turn-other" } }))
      .rejects.toThrow("binding_invalid");
    const receipt = await host.execute(request);
    const retry = await host.execute({
      ...request,
      human_bearer: "expired",
      attachment: { bearer_token: "expired", channel_secret: "expired", expires_at: "1970-01-01T00:00:00.000Z" },
    });

    expect(receipt).toMatchObject({ status: "completed", receipt_id: expect.stringMatching(/^[a-f0-9]{64}$/) });
    expect(receipt.artifact_ids).toEqual(["-aaaaaaaaaaaaaaa", "_bbbbbbbbbbbbbbb"]);
    expect(retry).toEqual(receipt);
    expect(brokerRequests).toHaveLength(1);
    expect(brokerRequests[0]?.url).toBe("http://127.0.0.1:18901/mcp/approvals/approval_12345678");
    expect(brokerRequests[0]?.init).toMatchObject({ method: "POST", redirect: "error" });
    expect(toolCalls).toEqual([{ name: "services_confirm", args: { approval_id: pending.approval_id } }]);
  });

  it("returns a safe uncertain state without any approval or execution retry", async () => {
    const approvals = await store();
    const pending = binding();
    await approvals.create(pending);
    await approvals.approve({
      approval_id: pending.approval_id, identity, argument_digest: pending.argument_digest, approved_at_ms: 1_000,
    });
    await approvals.claim({
      approval_id: pending.approval_id, identity, now_ms: 1_100, execution_deadline_ms: 1_500,
    });
    const fetchImpl = vi.fn();
    const providerFactory = vi.fn();
    const host = new LeafStandardServicesHumanApprovalHost({
      brokerEndpoint: "http://127.0.0.1:18901",
      environment: "local",
      approvalStore: approvals,
      fetchImpl: fetchImpl as typeof fetch,
      providerFactory,
      now: () => 2_000,
    });

    await expect(host.review(reviewInput(pending))).resolves.toMatchObject({ status: "uncertain", identity });
    await expect(host.execute({
      approval_id: pending.approval_id,
      argument_digest: pending.argument_digest,
      identity,
      human_bearer: "invalid",
      attachment: { bearer_token: "invalid", channel_secret: "invalid", expires_at: "1970-01-01T00:00:00.000Z" },
    })).resolves.toEqual({ status: "uncertain" });
    expect(fetchImpl).not.toHaveBeenCalled();
    expect(providerFactory).not.toHaveBeenCalled();
  });
});
