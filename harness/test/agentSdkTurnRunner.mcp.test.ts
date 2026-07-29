import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import type { HarnessTurnEvent } from "../src/ports/index.js";

import {
  buildTenantMcpOptions,
  buildTurnOptions,
  askUserEvent,
  apsTestRunMcpTargetDenial,
  createAskUserHandler,
  executeTenantMcpTool,
  hasFreshTenantMcpResolution,
  requiresToolConfirmation,
  resolveTenantMcpTool,
  resolveEnvMcpAttachment,
  resolveMcpAttachmentSafely,
  TENANT_MCP_EXECUTE_CAPABILITY,
  tenantMcpApprovalEvents,
  tenantMcpToolDenial,
} from "../src/ports/impl/agentSdkTurnRunner.js";
import { InMemoryMcpBridgeStore, type McpBridgeStore } from "../src/ports/impl/mcpBridge.js";

const LOCAL_SERVER = { local: true };
const SENTINEL_A = "TENANT_A_BEARER_SENTINEL";
const SENTINEL_B = "TENANT_B_BEARER_SENTINEL";

function turnOptions(mcpAttachment: Record<string, unknown> | null) {
  const canUseTool = async () => ({ behavior: "allow" });
  return buildTurnOptions({
    childEnv: {} as NodeJS.ProcessEnv,
    model: undefined,
    maxTurns: 24,
    abortController: new AbortController(),
    server: LOCAL_SERVER,
    skillBundle: null,
    mcpAttachment: mcpAttachment as never,
    canUseTool,
  });
}

function storeWith(configs: Array<{ name: string; url: string; authToken?: string }>): McpBridgeStore {
  return { set: async () => undefined, get: async () => configs, delete: async () => undefined };
}

describe("AgentSdkTurnRunner tenant MCP bridge", () => {
  it("wires a non-null attachment into executable SDK query options", async () => {
    const esbuild = await import("esbuild");
    const source = readFileSync(
      new URL("../src/ports/impl/agentSdkTurnRunner.ts", import.meta.url), "utf8",
    );
    const stripped = esbuild.transformSync(source, { loader: "ts" }).code;

    expect(stripped).toMatch(/const mcpStore = this\.bridgeStore\(\)/);
    expect(stripped).toMatch(/await resolveMcpAttachmentSafely\(mcpStore, input\.tenant_id\)/);
    expect(stripped).toMatch(/return attachment \? \{ mcpServers: attachment \} : \{\}/);
    expect(stripped).toMatch(/mcpServers: \{ \.\.\.tenantMcp\.mcpServers \?\? \{\}, \[MCP_SERVER_NAME\]: input\.server \}/);
    expect(stripped).toMatch(/const tenantTool = resolveTenantMcpTool\(toolName, mcpAttachment\);/);
    expect(stripped).toMatch(/tenantMcpApprovalEvents\(toolName, inp,/);
    expect(stripped).toMatch(/executeTenantMcpTool\(store, input\.tenant_id, proposal\.tool, proposal\.params \?\? \{\}\)/);
    expect(stripped).toMatch(/apsTestRunMcpTargetDenial\(proposalTool\)/);
    expect(stripped).toMatch(/hasFreshTenantMcpResolution\(store, input\.tenant_id, proposal\)/);
  });

  it("is off by default and adds no tenant mcpServers option", async () => {
    expect(await resolveEnvMcpAttachment(undefined, "tenant-a")).toBeNull();
    expect(buildTenantMcpOptions(null)).not.toHaveProperty("mcpServers");

    // The runner's pre-existing local converse server stays mounted. With the bridge
    // disabled it is the only key in the final SDK mcpServers map.
    expect(turnOptions(null).mcpServers).toEqual({ converse: LOCAL_SERVER });
  });

  it("keeps each tenant's attachment isolated in turn options", async () => {
    const store = new InMemoryMcpBridgeStore();
    await store.set("tenant-a", [{ name: "alpha", url: "https://alpha.example.test/mcp", authToken: SENTINEL_A }]);
    await store.set("tenant-b", [{ name: "bravo", url: "https://bravo.example.test/mcp", authToken: SENTINEL_B }]);

    const optionsA = turnOptions(await resolveMcpAttachmentSafely(store, "tenant-a"));
    const optionsB = turnOptions(await resolveMcpAttachmentSafely(store, "tenant-b"));
    const renderedA = JSON.stringify(optionsA.mcpServers);
    const renderedB = JSON.stringify(optionsB.mcpServers);

    expect(renderedA).toContain(SENTINEL_A);
    expect(renderedA).not.toContain(SENTINEL_B);
    expect(renderedB).toContain(SENTINEL_B);
    expect(renderedB).not.toContain(SENTINEL_A);
  });

  it("keeps the local server when a tenant attachment collides with converse", () => {
    expect(turnOptions({ converse: { tenant: true } }).mcpServers).toEqual({ converse: LOCAL_SERVER });
  });

  it("skips a broken bridge and leaves the local turn options usable without leaking its bearer", async () => {
    const diagnostics: string[] = [];
    const brokenStore: McpBridgeStore = {
      set: async () => undefined,
      get: async () => { throw new Error(`corrupt bridge data ${SENTINEL_A}`); },
      delete: async () => undefined,
    };

    const attachment = await resolveMcpAttachmentSafely(brokenStore, "tenant-a", (message) => diagnostics.push(message));
    expect(attachment).toBeNull();
    expect(turnOptions(attachment).mcpServers).toEqual({ converse: LOCAL_SERVER });
    expect(diagnostics.join("\n")).not.toContain(SENTINEL_A);
  });

  it("requires approval only for a tenant MCP tool mounted for this turn", () => {
    const approvals = new Set(["aps_test_run"]);
    const attachment = { tenant_a: { type: "http", url: "https://tenant.example.test/mcp" } } as never;
    expect(resolveTenantMcpTool("mcp__tenant_a__dangerous_write", attachment)).toEqual({
      serverName: "tenant_a",
      bareToolName: "dangerous_write",
    });
    expect(tenantMcpToolDenial("mcp__tenant_a__dangerous_write", attachment)).toBeNull();
    expect(tenantMcpToolDenial("mcp__unknown__dangerous_write", attachment)).toEqual({
      behavior: "deny",
      message: "tenant MCP tool is not attached for this tenant",
    });
    expect(tenantMcpToolDenial("mcp__converse__aps_test_run")).toBeNull();
    expect(tenantMcpToolDenial("mcp__converse__ask_user")).toBeNull();
    expect(tenantMcpToolDenial("mcp__tenantsrv__ask_user", null)).toEqual({ behavior: "deny", message: "tenant MCP tool is not attached for this tenant" });
    expect(requiresToolConfirmation("mcp__converse__aps_test_run", approvals)).toBe(true);
    expect(requiresToolConfirmation("mcp__converse__ask_user", approvals)).toBe(false);
    expect(requiresToolConfirmation("Read", approvals)).toBe(false);
    expect(turnOptions(null).allowedTools).toEqual(["mcp__converse__aps_test_run"]);
  });

  it("creates an approval event that names only the MCP host, never its path, query, or bearer", () => {
    const params = { layer: "Walls" };
    const events = tenantMcpApprovalEvents("mcp__tenant_a__edit_layer", params, "confirm-1", "drawing-1", "tenant.example.test:8443");
    expect(events).toEqual([
      expect.objectContaining({
        type: "proposed_run",
        data: expect.objectContaining({
          confirmation_id: "confirm-1",
          tool: "mcp__tenant_a__edit_layer",
          params: { ...params, _leaf_mcp_host: "tenant.example.test:8443" },
          capability: TENANT_MCP_EXECUTE_CAPABILITY,
          dwg: "drawing-1",
        }),
      }),
      expect.objectContaining({ type: "confirmation_required", data: expect.objectContaining({ confirmation_id: "confirm-1" }) }),
    ]);
    const rendered = JSON.stringify(events);
    expect(rendered).toContain("tenant.example.test:8443");
    expect(rendered).not.toContain(SENTINEL_A);
    expect(rendered).not.toContain("/mcp");
    expect(rendered).not.toContain("secret=");
  });

  it("executes an approved public tenant MCP proposal once, redacts before truncation, and keeps approval-only host data out of tool args", async () => {
    const store = storeWith([{ name: "alpha", url: "https://tenant.example.test/mcp?secret=not-for-logs", authToken: SENTINEL_A }]);
    let calls = 0;
    const outcome = await executeTenantMcpTool(store, "tenant-a", "mcp__alpha__edit_layer", { layer: "Walls", _leaf_mcp_host: "tenant.example.test" }, {
      resolver: async () => "203.0.113.8",
      fetchImpl: async (_url, init) => {
        calls += 1;
        expect(init?.redirect).toBe("manual");
        const body = JSON.parse(String(init?.body)) as Record<string, any>;
        expect(body.params.arguments).toEqual({ layer: "Walls" });
        return new Response(JSON.stringify({ result: { content: [{ type: "text", text: `${"x".repeat(495)}${SENTINEL_A}${"y".repeat(30)}` }] } }));
      },
    });
    expect(calls).toBe(1);
    expect(outcome.ok).toBe(true);
    expect(outcome.summary).toContain("(truncated)");
    expect(outcome.summary).not.toContain(SENTINEL_A.slice(0, 12));
  });

  it("refuses execute-time private DNS answers before fetch and rejects redirects without a second request", async () => {
    const store = storeWith([{ name: "alpha", url: "https://tenant.example.test/mcp", authToken: SENTINEL_A }]);
    let calls = 0;
    const privateOutcome = await executeTenantMcpTool(store, "tenant-a", "mcp__alpha__edit_layer", {}, {
      resolver: async () => "169.254.169.254",
      fetchImpl: async () => { calls += 1; return new Response("unexpected"); },
    });
    expect(privateOutcome.summary).toContain("host is not allowed");
    expect(calls).toBe(0);

    const redirectOutcome = await executeTenantMcpTool(store, "tenant-a", "mcp__alpha__edit_layer", {}, {
      resolver: async () => "203.0.113.8",
      fetchImpl: async () => { calls += 1; return new Response("", { status: 302, headers: { location: "http://127.0.0.1/" } }); },
    });
    expect(redirectOutcome.summary).toContain("redirected");
    expect(calls).toBe(1);
  });

  it("cancels an over-cap response body before returning the refusal", async () => {
    const store = storeWith([{ name: "alpha", url: "https://tenant.example.test/mcp" }]);
    let cancelled = false;
    const body = new ReadableStream({ cancel: () => { cancelled = true; } });
    const outcome = await executeTenantMcpTool(store, "tenant-a", "mcp__alpha__edit_layer", {}, {
      resolver: async () => "203.0.113.8",
      fetchImpl: async () => new Response(body, { headers: { "content-length": String(256 * 1024 + 1) } }),
    });
    expect(outcome.summary).toContain("exceeds 256 KB");
    expect(cancelled).toBe(true);
  });

  it("rejects MCP targets in the APS wrapper and requires capability plus a fresh mounted server at confirm time", async () => {
    expect(apsTestRunMcpTargetDenial("mcp__missing__x")).toContain("must be approved");
    const store = storeWith([{ name: "alpha", url: "https://tenant.example.test/mcp" }]);
    await expect(hasFreshTenantMcpResolution(store, "tenant-a", { tool: "mcp__alpha__x", capability: "drawing.write" })).resolves.toBe(false);
    await expect(hasFreshTenantMcpResolution(store, "tenant-a", { tool: "mcp__missing__x", capability: "mcp.execute" })).resolves.toBe(false);
    await expect(hasFreshTenantMcpResolution(store, "tenant-a", { tool: "mcp__alpha__x", capability: "mcp.execute" })).resolves.toBe(true);
  });

  it("emits a bounded question_required event and refuses invalid or oversized payloads", async () => {
    const options = ["One", "Two"].map((label) => ({ label, description: `${label} detail` }));
    expect(askUserEvent({ question: "Which plan?", options }, "question-1")).toEqual({
      type: "question_required",
      data: { question_id: "question-1", question: "Which plan?", options },
    });
    expect(askUserEvent({ question: "Too many?", options: Array.from({ length: 7 }, (_, i) => ({ label: String(i) })) }, "question-2")).toBeNull();
    expect(askUserEvent({ question: "x".repeat(501), options }, "question-3")).toBeNull();
    expect(askUserEvent({ question: "Which?", options: [{ label: "x".repeat(121) }, { label: "Two" }] }, "question-4")).toBeNull();
    expect(askUserEvent({ question: "Which?", options: [{ label: "One", description: "x".repeat(301) }, { label: "Two" }] }, "question-5")).toBeNull();
    expect(askUserEvent({ question: "Which?", options: [{ label: " One " }, { label: "Two" }] }, "question-6")).toBeNull();

    const pending: HarnessTurnEvent[] = [];
    const askUser = createAskUserHandler(pending, () => "question-7");
    await expect(askUser({ question: "x".repeat(501), options })).resolves.toMatchObject({ isError: true });
    expect(pending).toEqual([]);
  });

  it("emits one question per scripted turn and returns the second tool error to the model", async () => {
    const pending: HarnessTurnEvent[] = [];
    const askUser = createAskUserHandler(pending, () => "question-1");
    const first = await askUser({ question: "Which plan?", options: [{ label: "Standard" }, { label: "Premium" }] });
    const second = await askUser({ question: "And which region?", options: [{ label: "US" }, { label: "EU" }] });

    expect(first.isError).toBeUndefined();
    expect(second).toEqual({
      content: [{ type: "text", text: "one question per turn; end your turn and wait for the answer" }],
      isError: true,
    });
    expect(pending).toEqual([{
      type: "question_required",
      data: { question_id: "question-1", question: "Which plan?", options: [{ label: "Standard" }, { label: "Premium" }] },
    }]);
  });
});
