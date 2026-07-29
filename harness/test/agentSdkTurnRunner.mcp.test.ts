import { readFileSync } from "node:fs";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { describe, expect, it } from "vitest";
import type { HarnessTurnEvent } from "../src/ports/index.js";

import {
  buildTenantMcpOptions,
  buildTurnOptions,
  askUserEvent,
  createAskUserHandler,
  executeTenantMcpTool,
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

async function withMcpServer(
  handler: (request: IncomingMessage, response: ServerResponse) => void,
  run: (url: string) => Promise<void>,
): Promise<void> {
  const server = createServer(handler);
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("test MCP server did not bind a TCP port");
  try {
    await run(`http://127.0.0.1:${address.port}/mcp`);
  } finally {
    await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
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

  it("creates the APS-shaped approval events for a mounted tenant tool without executing it", () => {
    const params = { layer: "Walls" };
    const events = tenantMcpApprovalEvents("mcp__tenant_a__edit_layer", params, "confirm-1", "drawing-1");
    expect(events).toEqual([
      expect.objectContaining({
        type: "proposed_run",
        data: expect.objectContaining({
          confirmation_id: "confirm-1",
          tool: "mcp__tenant_a__edit_layer",
          params,
          capability: TENANT_MCP_EXECUTE_CAPABILITY,
          dwg: "drawing-1",
        }),
      }),
      expect.objectContaining({ type: "confirmation_required", data: expect.objectContaining({ confirmation_id: "confirm-1" }) }),
    ]);
    expect(JSON.stringify(events)).not.toContain(SENTINEL_A);
  });

  it("executes an approved tenant MCP proposal as one stateless tools/call request", async () => {
    const store = new InMemoryMcpBridgeStore();
    await withMcpServer((request, response) => {
      const chunks: Buffer[] = [];
      request.on("data", (chunk: Buffer) => chunks.push(chunk));
      request.on("end", () => {
        const requestBody = JSON.parse(Buffer.concat(chunks).toString("utf8")) as Record<string, unknown>;
        expect(request.method).toBe("POST");
        expect(request.headers.authorization).toBe(`Bearer ${SENTINEL_A}`);
        expect(request.headers["mcp-protocol-version"]).toBe("2026-07-28");
        expect(request.headers["mcp-method"]).toBe("tools/call");
        expect(request.headers["mcp-name"]).toBe("edit_layer");
        expect(requestBody).toMatchObject({
          jsonrpc: "2.0",
          method: "tools/call",
          params: {
            name: "edit_layer",
            arguments: { layer: "Walls" },
            _meta: { "io.modelcontextprotocol/protocolVersion": "2026-07-28" },
          },
        });
        response.setHeader("content-type", "application/json");
        response.end(JSON.stringify({ jsonrpc: "2.0", id: requestBody.id, result: { content: [{ type: "text", text: `${SENTINEL_A} ${"x".repeat(700)}` }] } }));
      });
    }, async (url) => {
      await store.set("tenant-a", [{ name: "alpha", url, authToken: SENTINEL_A }]);
      const outcome = await executeTenantMcpTool(store, "tenant-a", "mcp__alpha__edit_layer", { layer: "Walls" });
      expect(outcome.ok).toBe(true);
      expect(outcome.summary).toContain("(truncated)");
      expect(outcome.summary.length).toBeLessThan(550);
      expect(JSON.stringify(outcome)).not.toContain(SENTINEL_A);
    });
  });

  it("refuses oversized MCP responses and re-resolves deleted server configuration at confirm time", async () => {
    const store = new InMemoryMcpBridgeStore();
    let calls = 0;
    await withMcpServer((_request, response) => {
      calls += 1;
      response.writeHead(200, { "content-type": "application/json", "content-length": String(256 * 1024 + 1) });
      response.end("{}");
    }, async (url) => {
      await store.set("tenant-a", [{ name: "alpha", url, authToken: SENTINEL_A }]);
      const oversized = await executeTenantMcpTool(store, "tenant-a", "mcp__alpha__edit_layer", {});
      expect(oversized).toEqual(expect.objectContaining({ ok: false, summary: expect.stringContaining("exceeds 256 KB") }));
      expect(JSON.stringify(oversized)).not.toContain(SENTINEL_A);
      expect(calls).toBe(1);

      await store.delete("tenant-a");
      const deleted = await executeTenantMcpTool(store, "tenant-a", "mcp__alpha__edit_layer", {});
      expect(deleted).toEqual(expect.objectContaining({ ok: false, summary: expect.stringContaining('server "alpha"') }));
      expect(deleted.summary).toContain("no longer attached");
      expect(JSON.stringify(deleted)).not.toContain(SENTINEL_A);
      expect(calls).toBe(1);
    });
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
