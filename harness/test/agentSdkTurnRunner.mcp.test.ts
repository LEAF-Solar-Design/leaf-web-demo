import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import {
  buildTenantMcpOptions,
  buildTurnOptions,
  requiresToolConfirmation,
  resolveEnvMcpAttachment,
  resolveMcpAttachmentSafely,
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

describe("AgentSdkTurnRunner tenant MCP bridge", () => {
  it("wires a non-null attachment into executable SDK query options", async () => {
    const esbuild = await import("esbuild");
    const source = readFileSync(
      new URL("../src/ports/impl/agentSdkTurnRunner.ts", import.meta.url), "utf8",
    );
    const stripped = esbuild.transformSync(source, { loader: "ts" }).code;

    expect(stripped).toMatch(/resolveEnvMcpAttachment\(process\.env\.LEAF_MCP_BRIDGE_DIR, input\.tenant_id\)/);
    expect(stripped).toMatch(/return attachment \? \{ mcpServers: attachment \} : \{\}/);
    expect(stripped).toMatch(/mcpServers: \{ \[MCP_SERVER_NAME\]: input\.server, \.\.\.tenantMcp\.mcpServers \?\? \{\} \}/);
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

  it("routes every MCP tool name through confirmation without adding it to allowedTools", () => {
    const approvals = new Set(["aps_test_run"]);
    expect(requiresToolConfirmation("mcp__tenant_a__dangerous_write", approvals)).toBe(true);
    expect(requiresToolConfirmation("mcp__converse__aps_test_run", approvals)).toBe(true);
    expect(requiresToolConfirmation("Read", approvals)).toBe(false);
    expect(turnOptions(null).allowedTools).toEqual(["mcp__converse__aps_test_run"]);
  });
});
