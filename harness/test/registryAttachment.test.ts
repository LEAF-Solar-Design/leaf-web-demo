// Registry MCP attachment: OFF unless fully configured, read-only pair only,
// bearer confined to the per-server headers, and the author-tool security
// envelope (exactly three mutating tools) pinned as an invariant.
import { describe, expect, it } from "vitest";
import { registryMcpAttachment, REGISTRY_TOOL_NAMES } from "../src/ports/impl/agentSdkRunner.js";

const FULL = {
  LEAF_REGISTRY_MCP_URL: "https://studio.leafdesign.ai/api/mcp",
  LEAF_REGISTRY_MCP_TOKEN: "tok-abc",
  LEAF_REGISTRY_MCP_TENANT: "018f0000-0000-7000-8000-0000000d0e10",
} as NodeJS.ProcessEnv;

describe("registryMcpAttachment", () => {
  it("is OFF (null) when any of the three env vars is missing or blank", () => {
    expect(registryMcpAttachment({} as NodeJS.ProcessEnv)).toBeNull();
    for (const key of Object.keys(FULL)) {
      const partial = { ...FULL, [key]: undefined } as NodeJS.ProcessEnv;
      expect(registryMcpAttachment(partial)).toBeNull();
      const blank = { ...FULL, [key]: "   " } as NodeJS.ProcessEnv;
      expect(registryMcpAttachment(blank)).toBeNull();
    }
  });

  it("builds the http server config with bearer + tenant cookie when fully configured", () => {
    const attachment = registryMcpAttachment(FULL);
    expect(attachment).not.toBeNull();
    expect(attachment?.serverConfig).toEqual({
      type: "http",
      url: "https://studio.leafdesign.ai/api/mcp",
      headers: {
        Authorization: "Bearer tok-abc",
        Cookie: "tenant_id=018f0000-0000-7000-8000-0000000d0e10",
      },
    });
  });

  it("exposes exactly the two read-only registry tools", () => {
    const attachment = registryMcpAttachment(FULL);
    expect(attachment?.toolNames).toEqual([
      "mcp__registry__registry_list",
      "mcp__registry__registry_get",
    ]);
    expect(REGISTRY_TOOL_NAMES).toHaveLength(2);
    for (const name of REGISTRY_TOOL_NAMES) {
      expect(name).toMatch(/^mcp__registry__registry_(list|get)$/);
    }
  });

  it("keeps the bearer out of everything except the Authorization header", () => {
    const attachment = registryMcpAttachment(FULL);
    const withoutHeaders = JSON.stringify({ ...attachment, serverConfig: { ...attachment?.serverConfig, headers: {} } });
    expect(withoutHeaders).not.toContain("tok-abc");
  });

  it("trims configured values", () => {
    const padded = {
      LEAF_REGISTRY_MCP_URL: "  https://studio.leafdesign.ai/api/mcp  ",
      LEAF_REGISTRY_MCP_TOKEN: " tok-abc ",
      LEAF_REGISTRY_MCP_TENANT: " tenant-1 ",
    } as NodeJS.ProcessEnv;
    const attachment = registryMcpAttachment(padded);
    expect(attachment?.serverConfig.url).toBe("https://studio.leafdesign.ai/api/mcp");
    expect(attachment?.serverConfig.headers.Authorization).toBe("Bearer tok-abc");
    expect(attachment?.serverConfig.headers.Cookie).toBe("tenant_id=tenant-1");
  });
});

describe("author-tool security envelope", () => {
  it("the author session's mutating surface stays exactly three in-process tools", async () => {
    const source = await import("node:fs/promises").then((fs) =>
      fs.readFile(new URL("../src/vendor/mushy-author/ports/impl/agentSdkRunner.ts", import.meta.url), "utf8"),
    );
    const block = source.match(/const AUTHOR_TOOL_NAMES = \[([\s\S]*?)\];/)?.[1] ?? "";
    const names = [...block.matchAll(/"([^"]+)"/g)].map((m) => m[1]);
    expect(names).toEqual([
      "mcp__author__fs_tenant_repo",
      "mcp__author__validate_tool",
      "mcp__author__aps_test_run",
    ]);
  });
});
