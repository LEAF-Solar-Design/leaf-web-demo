import { describe, expect, it, vi } from "vitest";
import { z } from "zod";
import { ConverseLoop } from "../src/agent/converseLoop.js";
import { SPINE_SYSTEM_PROMPT } from "../src/agent/spineSystemPrompt.js";
import { AuthorStageStatusInput, SPINE_TOOL_NAMES } from "../src/ports/index.js";
import type { AppRunClient, ConverseRunInput } from "../src/ports/index.js";
import { HttpAppRunClient } from "../src/ports/impl/appRunClient.js";
import { ConverseSdkRunner, SPINE_MCP_TOOL_NAMES } from "../src/ports/impl/converseSdkRunner.js";
import { FakeAppRunClient } from "../src/ports/fakes/fakeAppRunClient.js";
import { FakeGateClient } from "../src/ports/fakes/fakeGateClient.js";
import { FakeSessionStore } from "../src/ports/fakes/fakeSessionStore.js";

const TENANT = "11111111-1111-4111-8111-111111111111";
const FOREIGN = "22222222-2222-4222-8222-222222222222";
const ID = "19097a36-1234-4234-8234-123456789abc";
const ARGS = { change_set_id: ID };
type Status = "queued" | "running" | "staged" | "failed";

// CustomizationService.stage_status_change(ChangeSet): updated_at comes from
// SQLite strftime / PostgreSQL _PG_NOW as TEXT, not an epoch timestamp.
function producer(status: Status): Record<string, unknown> {
  return {
    contract: "leaf.customization-stage-job.v1", change_set_id: ID,
    status, change_kind: "revise", target_tool_name: "panel-gap-checker",
    phase: status === "running" ? "authoring" : status,
    attempt: status === "queued" ? 0 : 1, updated_at: "2026-11-02T04:42:17.123Z",
    poll_url: `/api/author/stages/${ID}`, retry_after_ms: 1000,
    ...(status === "staged" ? {
      receipt: { staged_commit: "a".repeat(40), private_data: "receipt-secret" },
      result: { tool: { name: "panel-gap-checker", source: "generated-source" } },
    } : {}),
    ...(status === "failed" ? { error: {
      reason_code: "author_job_failed", retryable: false, message: "Author worker unavailable.",
      internal: { credential: "nested-secret" },
    } } : {}),
  };
}

function http(value: unknown, status = 200) {
  const fetchImpl = vi.fn<typeof fetch>().mockImplementation(async () => new Response(
    JSON.stringify(value), { status, headers: { "content-type": "application/json" } },
  ));
  const client = new HttpAppRunClient({ baseUrl: "https://app.invalid/", dispatchSecret: "dispatch-private-secret", fetchImpl });
  return { client, fetchImpl };
}

type Mounted = {
  name: string; shape: z.ZodRawShape;
  handler: (args: Record<string, unknown>) => Promise<{ content: Array<{ type: "text"; text: string }>; isError?: boolean }>;
};

// Exercise the actual host SDK registration -> permission -> normal dispatcher
// -> AppRun client. Only the external Agent SDK and HTTP transport are fixtures.
async function conversation(appRun: AppRunClient, args: Record<string, unknown> = ARGS, deny = false) {
  const mounted: Mounted[] = [];
  const replies: Awaited<ReturnType<Mounted["handler"]>>[] = [];
  const permissions: unknown[] = [];
  let hook: ConverseRunInput["canUseTool"] | undefined;
  const sdk = {
    tool(name: string, _description: string, shape: z.ZodRawShape, handler: Mounted["handler"]) {
      return { name, shape, handler };
    },
    createSdkMcpServer(options: { name: string; tools: Mounted[] }) {
      if (options.name === "spine") mounted.push(...options.tools);
      return options;
    },
    query({ options }: { options: { canUseTool: ConverseRunInput["canUseTool"] } }) {
      hook = options.canUseTool;
      return (async function* () {
        const tool = mounted.find(item => item.name === "author_stage_status");
        expect(tool).toBeDefined();
        // The invalid-input cases also invoke the handler directly to prove it
        // rejects before dispatch, even if SDK validation is bypassed.
        permissions.push(await options.canUseTool("mcp__spine__author_stage_status", args));
        replies.push(await tool!.handler(args));
        yield { type: "result", subtype: "success", is_error: false, result: "Read existing stage." };
      })();
    },
  };
  const runner = new ConverseSdkRunner({
    grant: { kind: "oauth", oauthToken: "fake-agent-grant" },
    sdkImport: async () => sdk, zodImport: async () => ({ z }),
  });
  const gate = new FakeGateClient();
  if (deny) gate.deny("read_platform_state", "status reads disabled");
  const store = new FakeSessionStore();
  const loop = new ConverseLoop({ runner, appRun, gate, store });
  const session = await loop.createOrGetSession(TENANT, "drawing-a");
  const turn = await loop.handleMessage({
    sessionId: session.session_id, tenantId: TENANT, text: "What is the existing stage status?",
    contextPacket: { catalog: [], drawing: { id: "drawing-a", head_version: 1 }, grant: { kind: "oauth", degraded: false } },
  });
  await turn.done;
  return { mounted, replies, permissions, gate, hook, events: await store.eventsAfter(session.session_id, 0) };
}

describe("author_stage_status joined read capability", () => {
  it.each<Status>(["queued", "running", "staged", "failed"])("reads %s through SDK, dispatch and exact authenticated HTTP", async status => {
    const raw = producer(status);
    const before = JSON.stringify(raw);
    const { client, fetchImpl } = http(raw);
    const author = vi.spyOn(client, "authorTool");
    const publication = vi.spyOn(client, "requestPublication");
    const run = vi.spyOn(client, "submitRun");
    const result = await conversation(client);
    expect(SPINE_TOOL_NAMES).toContain("author_stage_status");
    expect(SPINE_MCP_TOOL_NAMES).toContain("mcp__spine__author_stage_status");
    const tool = result.mounted.filter(item => item.name === "author_stage_status");
    expect(tool).toHaveLength(1);
    expect(Object.keys(tool[0]!.shape)).toEqual(["change_set_id"]);
    expect(z.strictObject(tool[0]!.shape).parse(ARGS)).toEqual(ARGS);
    expect(result.permissions).toEqual([expect.objectContaining({ behavior: "allow" })]);
    expect(result.replies).toHaveLength(1);
    expect(result.replies[0]!.isError).toBeUndefined();
    const projected = JSON.parse(result.replies[0]!.content[0]!.text);
    expect(projected).toEqual({
      contract: raw.contract, change_set_id: ID, status, change_kind: "revise",
      target_tool_name: "panel-gap-checker", attempt: raw.attempt, phase: raw.phase, updated_at: raw.updated_at,
      ...(status === "failed" ? { error: { reason_code: "author_job_failed", retryable: false, message: "Author worker unavailable." } } : {}),
    });
    expect(JSON.stringify(result.events)).not.toMatch(/generated-source|receipt-secret|nested-secret|dispatch-private-secret/);
    expect(fetchImpl).toHaveBeenCalledOnce();
    expect(fetchImpl).toHaveBeenCalledWith(`https://app.invalid/api/author/stages/${ID}`, expect.objectContaining({
      method: "GET", redirect: "error", headers: {
        "content-type": "application/json", "x-tenant-id": TENANT, "x-dispatch-secret": "dispatch-private-secret",
      },
    }));
    expect(fetchImpl.mock.calls[0]![1]).not.toHaveProperty("body");
    expect(result.gate.checks.length).toBeGreaterThan(0);
    expect(result.gate.checks.every(check => check.action === "read_platform_state" && check.args.what === "jobs")).toBe(true);
    expect(author).not.toHaveBeenCalled();
    expect(publication).not.toHaveBeenCalled();
    expect(run).not.toHaveBeenCalled();
    expect(JSON.stringify(raw)).toBe(before);
    expect(await result.hook!("mcp__other__author_stage_status", ARGS)).toMatchObject({ behavior: "deny" });
  });

  it.each([
    {}, { change_set_id: "not-a-uuid" }, { change_set_id: ID.toUpperCase() },
    { change_set_id: `${ID}/publish` }, { change_set_id: `${ID}\n` },
    { change_set_id: ID.replace(/-/g, "") }, { change_set_id: 1 },
    { ...ARGS, tenant_id: FOREIGN }, { ...ARGS, project_id: FOREIGN },
    { ...ARGS, authorization: "Bearer forged" }, { ...ARGS, retry: true },
  ])("rejects closed/UUID args before gate and network: %j", async args => {
    const { client, fetchImpl } = http(producer("queued"));
    expect(AuthorStageStatusInput.safeParse(args).success).toBe(false);
    const result = await conversation(client, args);
    expect(result.permissions).toEqual([expect.objectContaining({ behavior: "deny" })]);
    expect(result.replies[0]?.isError).toBe(true);
    expect(result.gate.checks).toEqual([]);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("preserves a denied read without HTTP or mutation", async () => {
    const { client, fetchImpl } = http(producer("queued"));
    const result = await conversation(client, ARGS, true);
    // Permission admits a known tool; the normal executor enforces the gate.
    expect(result.permissions).toEqual([expect.objectContaining({ behavior: "allow" })]);
    expect(result.gate.checks).toContainEqual(expect.objectContaining({ action: "read_platform_state" }));
    expect(result.replies[0]?.isError).toBe(true);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it.each([
    { change_set_id: FOREIGN }, { contract: "unknown" }, { status: "published" },
    { status: "running", phase: "running" }, { attempt: -1 }, { attempt: 0.5 },
    { attempt: true }, { updated_at: 1793594537123 }, { updated_at: "yesterday" },
    { change_kind: "remove" }, { target_tool_name: "x".repeat(65) },
    { error: { reason_code: "failed", retryable: false } },
  ])("refuses invalid producer metadata: %j", async patch => {
    const { client } = http({ ...producer("queued"), ...patch });
    const result = await conversation(client);
    expect(result.replies[0]?.isError).toBe(true);
    expect(result.replies[0]?.content[0]?.text).not.toContain("receipt");
  });

  it.each([
    undefined, { reason_code: "x", retryable: "yes" },
    { reason_code: "x".repeat(201), retryable: false },
    { reason_code: "x", retryable: false, message: "x".repeat(501) },
    { reason_code: "x", retryable: false, message: "dispatch-private-secret" },
    { reason_code: "x", retryable: false, message: "Authorization: Bearer private-credential" },
  ])("refuses invalid or credential-reflecting failure: %j", async error => {
    const { client } = http({ ...producer("failed"), error });
    await expect(client.getAuthorStageStatus(TENANT, ID)).rejects.toThrow("Author stage status unavailable.");
  });

  it.each([401, 404, 500, 202])("does not turn HTTP %s into status success", async status => {
    const { client } = http(producer("queued"), status);
    await expect(client.getAuthorStageStatus(TENANT, ID)).rejects.toThrow("Author stage status unavailable.");
  });

  it.each([null, [], "not status", {}, { status: "queued" }])("rejects missing or unknown producer shapes: %j", async value => {
    const { client } = http(value);
    await expect(client.getAuthorStageStatus(TENANT, ID)).rejects.toThrow("Author stage status unavailable.");
  });

  it("preserves producer-optional fields without inventing them", async () => {
    const value = producer("failed");
    delete value.target_tool_name;
    value.error = { reason_code: "customization_stage_failed", retryable: true };
    const { client } = http(value);
    const result = await client.getAuthorStageStatus(TENANT, ID);
    expect(result).not.toHaveProperty("target_tool_name");
    expect(result.error).toEqual({ reason_code: "customization_stage_failed", retryable: true });
  });

  it("validates the HTTP path before network and refuses absent dispatch authentication", async () => {
    const { client, fetchImpl } = http(producer("queued"));
    for (const id of ["bad", `${ID}/suffix`, `${ID}%2fextra`, ID.toUpperCase()]) {
      await expect(client.getAuthorStageStatus(TENANT, id)).rejects.toThrow();
    }
    const unauthenticated = new HttpAppRunClient({ baseUrl: "https://app.invalid", dispatchSecret: "", fetchImpl });
    await expect(unauthenticated.getAuthorStageStatus(TENANT, ID)).rejects.toThrow();
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("fake reads explicit tenant-owned existing rows without changing them", async () => {
    const fake = new FakeAppRunClient();
    const raw = producer("failed");
    fake.authorStages.set(TENANT, new Map([[ID, raw]]));
    const before = JSON.stringify(raw);
    const result = await conversation(fake);
    expect(result.replies[0]?.isError).toBeUndefined();
    expect(fake.authorStageStatusCalls).toEqual([{ tenantId: TENANT, changeSetId: ID }]);
    expect(fake.methodLog).toEqual(["getAuthorStageStatus"]);
    expect(fake.authorCalls).toEqual([]);
    expect(fake.publicationCalls).toEqual([]);
    expect(fake.submitCalls).toEqual([]);
    await expect(fake.getAuthorStageStatus(FOREIGN, ID)).rejects.toMatchObject({ status: 404 });
    expect(JSON.stringify(raw)).toBe(before);
  });

  it.each([
    { ...ARGS, tenant_id: FOREIGN }, { ...ARGS, retry: true },
    { change_set_id: ID.toUpperCase() }, { change_set_id: `${ID}/suffix` },
  ])("normal dispatcher independently refuses invalid args: %j", async args => {
    const fake = new FakeAppRunClient();
    const gate = new FakeGateClient();
    const store = new FakeSessionStore();
    const results: Array<{ content: string; isError?: boolean }> = [];
    const loop = new ConverseLoop({
      appRun: fake, gate, store,
      runner: { async *run(input) {
        results.push(await input.tools.execute("author_stage_status", args));
        yield { type: "done", stopReason: "end_turn", sdkSessionId: null };
      } },
    });
    const session = await loop.createOrGetSession(TENANT, "drawing-a");
    const turn = await loop.handleMessage({
      sessionId: session.session_id, tenantId: TENANT, text: "Read existing status",
      contextPacket: { catalog: [], drawing: { id: "drawing-a", head_version: 1 }, grant: { kind: "oauth", degraded: false } },
    });
    await turn.done;
    expect(results[0]?.isError).toBe(true);
    expect(gate.checks).toEqual([]);
    expect(fake.methodLog).toEqual([]);
  });

  it("keeps status-only guidance separate from publication", () => {
    expect(SPINE_SYSTEM_PROMPT).toContain("author_stage_status");
    expect(SPINE_SYSTEM_PROMPT).toContain("A queued or running/authoring stage");
    expect(SPINE_SYSTEM_PROMPT).toContain("Do not call author_tool or request_publication");
    expect(SPINE_SYSTEM_PROMPT).toContain("Never ask the user to copy a bearer token");
  });
});
