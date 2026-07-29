import { mkdtempSync, readdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import {
  describeConfig,
  FileMcpBridgeStore,
  InMemoryMcpBridgeStore,
  resolveMcpAttachment,
} from "../src/ports/impl/mcpBridge.js";

const SENTINEL = "SENTINEL_BEARER_XYZ";

function config(name: string, token = SENTINEL) {
  return { name, url: `https://${name}.example.test/mcp?secret=not-for-logs`, authToken: token };
}

function scratch(): string {
  return mkdtempSync(join(tmpdir(), "leaf-mcp-bridge-test-"));
}

describe("MCP bridge tenant isolation and redaction", () => {
  it("returns null when a tenant has no attachment", async () => {
    expect(await resolveMcpAttachment(new InMemoryMcpBridgeStore(), "missing-tenant")).toBeNull();
  });

  it("attaches only the requesting tenant's bearer", async () => {
    const store = new InMemoryMcpBridgeStore();
    await store.set("tenant-a", [config("alpha", "BEARER_FOR_A")]);
    await store.set("tenant-b", [config("bravo", "BEARER_FOR_B")]);

    const attachmentA = JSON.stringify(await resolveMcpAttachment(store, "tenant-a"));
    const attachmentB = JSON.stringify(await resolveMcpAttachment(store, "tenant-b"));
    expect(attachmentA).toContain("BEARER_FOR_A");
    expect(attachmentA).not.toContain("BEARER_FOR_B");
    expect(attachmentB).toContain("BEARER_FOR_B");
    expect(attachmentB).not.toContain("BEARER_FOR_A");
  });

  it("redacts bearer tokens from descriptions and thrown errors", async () => {
    const invalid = { ...config("bad name"), authToken: SENTINEL };
    expect(JSON.stringify(describeConfig(invalid))).not.toContain(SENTINEL);
    for (const store of [new InMemoryMcpBridgeStore(), new FileMcpBridgeStore({ dir: scratch() })]) {
      await expect(store.set("tenant", [invalid])).rejects.toSatisfy((error: unknown) => {
        return JSON.stringify(error).includes(SENTINEL) === false && String(error).includes(SENTINEL) === false;
      });
    }
  });
});

describe("MCP bridge validation", () => {
  it.each(["evil),Bash", "../../x", "trailing.", "converse", "CONVERSE"])("refuses hostile server name %s", async (name) => {
    const store = new InMemoryMcpBridgeStore();
    await expect(store.set("tenant", [config(name)])).rejects.toThrow("mcp_bridge_invalid_config");
  });

  it("refuses more than sixteen servers", async () => {
    const store = new InMemoryMcpBridgeStore();
    const configs = Array.from({ length: 17 }, (_, index) => config(`server-${index}`, "token"));
    await expect(store.set("tenant", configs)).rejects.toThrow("mcp_bridge_too_many_configs");
  });

  it("refuses a serialized configuration larger than 64KB", async () => {
    const store = new InMemoryMcpBridgeStore();
    await expect(store.set("tenant", [config("large", "x".repeat(64 * 1024))])).rejects.toThrow(
      "mcp_bridge_config_too_large",
    );
  });

  it.each(["javascript:evil()", "file:///etc/passwd"])("refuses unsafe MCP URL %s at set-time", async (url) => {
    const store = new InMemoryMcpBridgeStore();
    await expect(store.set("tenant", [{ name: "unsafe-url", url }])).rejects.toThrow("mcp_bridge_invalid_config");
  });
});

describe("file-backed MCP bridge store", () => {
  it("skips a stored reserved name without leaking its bearer in diagnostics", async () => {
    const dir = scratch();
    const tenantId = "pre-validation-tenant";
    const store = new FileMcpBridgeStore({ dir });
    await store.set(tenantId, [config("seed", "safe-token")]);
    const [filename] = readdirSync(dir);
    writeFileSync(join(dir, filename), JSON.stringify([config("converse")]) + "\n", "utf8");

    const diagnostics: string[] = [];
    expect(await resolveMcpAttachment(new FileMcpBridgeStore({ dir }), tenantId, (message) => diagnostics.push(message))).toBeNull();
    expect(diagnostics.join("\n")).toContain('authToken="<redacted>"');
    expect(diagnostics.join("\n")).not.toContain(SENTINEL);
  });

  it("round-trips configs without using the raw tenant id as a filename", async () => {
    const dir = scratch();
    const tenantId = "tenant-with-a-private-name";
    const store = new FileMcpBridgeStore({ dir });
    await store.set(tenantId, [config("roundtrip", "file-token")]);

    expect(await new FileMcpBridgeStore({ dir }).get(tenantId)).toEqual([config("roundtrip", "file-token")]);
    expect(readdirSync(dir).some((filename) => filename.includes(tenantId))).toBe(false);
    await store.delete(tenantId);
    expect(await store.get(tenantId)).toBeNull();
  });

  it("keeps each re-opened tenant attachment limited to its own bearer", async () => {
    const dir = scratch();
    const store = new FileMcpBridgeStore({ dir });
    await store.set("tenant-a", [config("alpha", "BEARER_FOR_A")]);
    await store.set("tenant-b", [config("bravo", "BEARER_FOR_B")]);

    const reopened = new FileMcpBridgeStore({ dir });
    const attachmentA = JSON.stringify(await resolveMcpAttachment(reopened, "tenant-a"));
    const attachmentB = JSON.stringify(await resolveMcpAttachment(reopened, "tenant-b"));
    expect(attachmentA).toContain("BEARER_FOR_A");
    expect(attachmentA).not.toContain("BEARER_FOR_B");
    expect(attachmentB).toContain("BEARER_FOR_B");
    expect(attachmentB).not.toContain("BEARER_FOR_A");
  });
});
