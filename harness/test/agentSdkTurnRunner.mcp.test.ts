import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import type { HarnessTurnEvent } from "../src/ports/index.js";

import {
  buildTenantMcpOptions,
  buildTurnOptions,
  askUserEvent,
  createAskUserHandler,
  requiresToolConfirmation,
  resolveEnvMcpAttachment,
  resolveMcpAttachmentSafely,
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

    mcpAttachment: mcpAttachment as never,
    canUseTool,
  });
}

describe("AgentSdkTurnRunner tenant MCP bridge", () => {
  it("wires a non-null attachment into executable SDK query options", async () => {
    const esbuild = await import("esbuild");
    const source = readFileSync(
      new URL("../src/ports/impl/agentSdkTurnRunner.ts", import.meta.url), "utf8",
    );
    const stripped = esbuild.transformSync(source, { loader: "ts" }).code;

    expect(stripped).toMatch(/resolveEnvMcpAttachment\(process\.env\.LEAF_MCP_BRIDGE_DIR, input\.tenant_id\)/);
    expect(stripped).toMatch(/return attachment \? \{ mcpServers: attachment \} : \{\}/);
    expect(stripped).toMatch(/mcpServers: \{ \.\.\.tenantMcp\.mcpServers \?\? \{\}, \[MCP_SERVER_NAME\]: input\.server \}/);
    expect(stripped).toMatch(/const tenantMcpDenial = tenantMcpToolDenial\(toolName\);\s*if \(tenantMcpDenial\) return tenantMcpDenial;\s*const bare/);
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

    const optionsA = turnOptions(await resolveMcpAttachmentSafely(store, "tenant-a", console.error, async () => "203.0.113.10"));
    const optionsB = turnOptions(await resolveMcpAttachmentSafely(store, "tenant-b", console.error, async () => "203.0.113.10"));
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

  it("denies every non-allowlisted MCP tool before the APS confirmation parser", () => {
    const approvals = new Set(["aps_test_run"]);
    expect(tenantMcpToolDenial("mcp__tenant_a__dangerous_write")).toEqual({
      behavior: "deny",
      message: "tenant MCP tools are mounted read-only in this release; tool execution approval ships separately",
    });
    expect(tenantMcpToolDenial("mcp__converse__evil__dangerous_write")).toEqual({
      behavior: "deny",
      message: "tenant MCP tools are mounted read-only in this release; tool execution approval ships separately",
    });
    expect(tenantMcpToolDenial("mcp__converse__aps_test_run")).toBeNull();
    expect(tenantMcpToolDenial("mcp__converse__ask_user")).toBeNull();
    expect(tenantMcpToolDenial("mcp__tenantsrv__ask_user")).toEqual({
      behavior: "deny",
      message: "tenant MCP tools are mounted read-only in this release; tool execution approval ships separately",
    });
    expect(requiresToolConfirmation("mcp__converse__aps_test_run", approvals)).toBe(true);
    expect(requiresToolConfirmation("mcp__converse__ask_user", approvals)).toBe(false);
    expect(requiresToolConfirmation("Read", approvals)).toBe(false);
    expect(turnOptions(null).allowedTools).toEqual(["mcp__converse__aps_test_run"]);
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
