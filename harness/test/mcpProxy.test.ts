import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { afterEach, describe, expect, it } from "vitest";

import { guardedFetch, proxyTenantMcpServer } from "../src/ports/impl/mcpProxy.js";

const servers: Server[] = [];

async function listen(): Promise<{ url: string; hits: () => number }> {
  let count = 0;
  const server = createServer((_request, response) => {
    count += 1;
    response.writeHead(200, { "content-type": "application/json" }).end("{}");
  });
  servers.push(server);
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;
  return { url: `http://127.0.0.1:${port}/mcp`, hits: () => count };
}

afterEach(() => {
  for (const server of servers.splice(0)) server.close();
});

describe("Leaf MCP transport wrapper", () => {
  it("rejects private destinations before any request reaches them", async () => {
    const privateServer = await listen();

    await expect(guardedFetch("127.0.0.1")(privateServer.url, { method: "POST" }))
      .rejects.toThrow("mcp_upstream_host_unsafe");

    expect(privateServer.hits()).toBe(0);
  });

  it("rejects a host change before DNS or transport", async () => {
    await expect(guardedFetch("broker.example")(
      "https://attacker.example/mcp",
      { method: "POST" },
    )).rejects.toThrow("mcp_upstream_host_changed");
  });

  it("omits an unsafe legacy proxy instead of exposing its URL or credential", async () => {
    const privateServer = await listen();
    const credential = "credential-that-must-not-appear";
    const diagnostics: string[] = [];

    const proxied = await proxyTenantMcpServer(
      { name: "tenant-server", url: privateServer.url, authToken: credential },
      (message) => diagnostics.push(message),
    );

    expect(proxied).toBeNull();
    expect(privateServer.hits()).toBe(0);
    expect(diagnostics.join(" ")).toContain("unsafe");
    expect(diagnostics.join(" ")).not.toContain(privateServer.url);
    expect(diagnostics.join(" ")).not.toContain(credential);
  });
});
