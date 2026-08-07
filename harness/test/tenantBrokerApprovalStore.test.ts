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

async function store() {
  const directory = await mkdtemp(join(tmpdir(), "leaf-mcp-approvals-"));
  directories.push(directory);
  return new FileTenantBrokerApprovalStore(directory);
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

afterEach(async () => {
  await Promise.all(directories.splice(0).map((directory) => rm(directory, { recursive: true, force: true })));
});

describe("durable tenant broker approval store", () => {
  it("fails closed on file storage outside local mode", () => {
    expect(() => createTenantBrokerApprovalStore({
      LEAF_HARNESS_SESSION_STORE: "file",
      LEAF_RUNTIME_ENV: "production",
    })).toThrow("requires PostgreSQL");
  });

  it("requires the complete approval catalog before PostgreSQL startup", () => {
    const required = [
      ["approval_id", "text"], ["tenant_id", "text"], ["subject_id", "text"],
      ["session_id", "text"], ["authority_turn_id", "text"],
      ["subscription_mount_id", "text"], ["runner_profile_id", "text"],
      ["service_id", "text"], ["tool_id", "text"], ["arguments", "jsonb"],
      ["argument_digest", "text"], ["expires_at", "timestamp with time zone"],
      ["created_at", "timestamp with time zone"],
    ].map(([column_name, data_type]) => ({
      table_name: "harness_tenant_mcp_approvals",
      column_name,
      data_type,
      is_nullable: "NO",
    }));
    expect(() => assertTenantBrokerApprovalCatalog({
      columns: required,
      indexes: [{
        indexname: "idx_harness_tenant_mcp_approvals_expiry",
        indexdef: "CREATE INDEX idx_harness_tenant_mcp_approvals_expiry ON public.harness_tenant_mcp_approvals USING btree (expires_at)",
      }],
    })).not.toThrow();
    expect(() => assertTenantBrokerApprovalCatalog({
      columns: required.slice(1),
      indexes: [],
    })).toThrow("schema is incomplete");
  });

  it("uses duplicate-safe creation and an exact atomic delete for PostgreSQL consume", async () => {
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
    };
    const query = vi.fn(async (sql: string) => {
      if (sql.includes("INSERT INTO")) return { rowCount: 1, rows: [] };
      if (sql.includes("DELETE FROM")) return { rowCount: 1, rows: [row] };
      return { rowCount: 1, rows: [row] };
    });
    const approvals = new PgTenantBrokerApprovalStore({ query } as unknown as Pool);

    expect(await approvals.create(pending)).toBe(true);
    expect(await approvals.review({
      approval_id: pending.approval_id,
      argument_digest: pending.argument_digest,
      tenant_id: identity.tenant_id,
      subject_id: identity.subject_id,
      now_ms: 1_000,
    })).toEqual(pending);
    expect(await approvals.consume({
      approval_id: pending.approval_id,
      identity,
      now_ms: 1_000,
    })).toEqual(pending);

    expect(query.mock.calls[0]?.[0]).toContain("ON CONFLICT (approval_id) DO NOTHING");
    const consumeSql = String(query.mock.calls[2]?.[0]);
    for (const field of [
      "tenant_id", "subject_id", "session_id", "authority_turn_id",
      "subscription_mount_id", "runner_profile_id", "expires_at",
    ]) expect(consumeSql).toContain(field);
    expect(consumeSql).toContain("DELETE FROM harness_tenant_mcp_approvals");
    expect(consumeSql).toContain("RETURNING");
  });

  it("rehydrates across instances and consumes only the exact identity once", async () => {
    const first = await store();
    const second = new FileTenantBrokerApprovalStore((first as unknown as { directory: string }).directory);
    const pending = binding();
    expect(await first.create(pending)).toBe(true);
    expect(await first.create(pending)).toBe(false);
    expect(await second.review({
      approval_id: pending.approval_id,
      argument_digest: pending.argument_digest,
      tenant_id: identity.tenant_id,
      subject_id: identity.subject_id,
      now_ms: 1_000,
    })).toMatchObject({ approval_id: pending.approval_id });

    for (const wrong of [
      { ...identity, tenant_id: "tenant-b" },
      { ...identity, subject_id: "auth0:bob" },
      { ...identity, session_id: "session-b" },
      { ...identity, authority_turn_id: "turn-b" },
      { ...identity, subscription_mount_id: "mount-b" },
      { ...identity, runner_profile_id: "author" as const },
    ]) {
      expect(await second.consume({
        approval_id: pending.approval_id,
        identity: wrong,
        now_ms: 1_000,
      })).toBeNull();
    }
    expect(await second.consume({ approval_id: pending.approval_id, identity, now_ms: 1_000 }))
      .toEqual(pending);
    expect(await second.consume({ approval_id: pending.approval_id, identity, now_ms: 1_000 }))
      .toBeNull();
  });

  it("does not review or consume an expired binding", async () => {
    const approvals = await store();
    const pending = binding({ expires_at: "1970-01-01T00:00:01.000Z" });
    expect(await approvals.create(pending)).toBe(true);
    expect(await approvals.review({
      approval_id: pending.approval_id,
      argument_digest: pending.argument_digest,
      tenant_id: identity.tenant_id,
      subject_id: identity.subject_id,
      now_ms: 2_000,
    })).toBeNull();
    expect(await approvals.consume({ approval_id: pending.approval_id, identity, now_ms: 2_000 }))
      .toBeNull();
  });
});

describe("human approval host", () => {
  it("approves with the human token then executes the stored call once", async () => {
    const approvals = await store();
    const pending = binding();
    await approvals.create(pending);
    const brokerRequests: Array<{ url: string; init?: RequestInit }> = [];
    const toolCalls: Array<{ name: string; args: Record<string, unknown> }> = [];
    const host = new LeafStandardServicesHumanApprovalHost({
      brokerEndpoint: "https://staging-api.leafdesign.ai",
      environment: "staging",
      approvalStore: approvals,
      fetchImpl: (async (url, init) => {
        brokerRequests.push({ url: String(url), init });
        return new Response(JSON.stringify({ status: "approved" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }) as typeof fetch,
      clientFactory: async () => ({
        async connect() {},
        async callTool(name, args) {
          toolCalls.push({ name, args });
          return { structuredContent: { status: "completed", result: { safe: true } } };
        },
        async close() {},
      }),
      now: () => 1_000,
    });

    await expect(host.execute({
      approval_id: pending.approval_id,
      argument_digest: pending.argument_digest,
      identity: { ...identity, authority_turn_id: "turn-other" },
      human_bearer: "human.approval.token.value.1234567890",
      attachment: {
        bearer_token: "attachment.token.value.1234567890",
        channel_secret: "channel-secret-value-1234567890-abcd",
        expires_at: "2099-01-01T00:00:00.000Z",
      },
    })).rejects.toThrow("binding_invalid");
    expect(brokerRequests).toHaveLength(0);

    const receipt = await host.execute({
      approval_id: pending.approval_id,
      argument_digest: pending.argument_digest,
      identity,
      human_bearer: "human.approval.token.value.1234567890",
      attachment: {
        bearer_token: "attachment.token.value.1234567890",
        channel_secret: "channel-secret-value-1234567890-abcd",
        expires_at: "2099-01-01T00:00:00.000Z",
      },
    });

    expect(receipt).toMatchObject({ status: "completed" });
    expect(receipt.receipt_id).toMatch(/^[a-f0-9]{64}$/);
    expect(brokerRequests).toHaveLength(1);
    expect(brokerRequests[0]?.url).toBe("https://staging-api.leafdesign.ai/mcp/approvals/approval_12345678");
    expect(brokerRequests[0]?.init).toMatchObject({ method: "POST", redirect: "error" });
    expect(String((brokerRequests[0]?.init?.headers as Record<string, string>).authorization))
      .toContain("human.approval.token");
    expect(toolCalls).toEqual([{ name: "services_confirm", args: { approval_id: pending.approval_id } }]);
    await expect(host.execute({
      approval_id: pending.approval_id,
      argument_digest: pending.argument_digest,
      identity,
      human_bearer: "human.approval.token.value.1234567890",
      attachment: {
        bearer_token: "attachment.token.value.1234567890",
        channel_secret: "channel-secret-value-1234567890-abcd",
        expires_at: "2099-01-01T00:00:00.000Z",
      },
    })).rejects.toThrow("binding_invalid");
    expect(brokerRequests).toHaveLength(1);
  });
});
