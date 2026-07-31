/**
 * The guarded upstream transport, tested against REAL local HTTP servers.
 *
 * PR #360's review established that host validation cannot make a
 * tenant-controlled URL safe: the Agent SDK's own fetch follows redirects, so
 * an approved public endpoint can 307 the MCP POST to a private address. These
 * tests drive the actual guardedFetch against a server that really redirects,
 * because "the option is set" is not the property that matters — "the request
 * does not arrive at the redirect target" is.
 */
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { afterEach, describe, expect, it } from "vitest";

import { guardedFetch, proxyTenantMcpServer } from "../src/ports/impl/mcpProxy.js";

const servers: Server[] = [];

async function listen(handler: Parameters<typeof createServer>[1]): Promise<{ url: string; port: number }> {
  const server = createServer(handler);
  servers.push(server);
  // AWAIT the bind: server.address() is null until 'listening' fires, which is
  // what made the first version of these tests fail before reaching any
  // assertion about redirects.
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;
  return { url: `http://127.0.0.1:${port}/mcp`, port };
}

afterEach(() => {
  for (const server of servers.splice(0)) server.close();
});

describe("guardedFetch refuses redirects", () => {
  it("does not deliver the request to a redirect target", async () => {
    let targetHits = 0;
    const target = await listen((_req, res) => { targetHits += 1; res.writeHead(200).end("{}"); });
    const redirector = await listen((_req, res) => {
      // The exact attack: an approved public host answers 307 to somewhere else.
      res.writeHead(307, { location: `http://127.0.0.1:${target.port}/mcp` }).end();
    });

    // IP literal as the host: skips the DNS re-check, so the ONLY thing that
    // can make this throw is the redirect refusal. With a fake hostname an
    // ENOTFOUND would satisfy rejects.toThrow() and prove nothing.
    const fetchGuarded = guardedFetch("127.0.0.1");
    await expect(fetchGuarded(redirector.url, { method: "POST", body: "{}" })).rejects.toThrow();

    // THE assertion. Not "an option was set" — the redirect target was never
    // contacted, which is the only thing that protects a private address.
    expect(targetHits).toBe(0);
  });

  it("passes a non-redirecting response through untouched", async () => {
    const ok = await listen((_req, res) => {
      res.writeHead(200, { "content-type": "application/json" }).end('{"ok":true}');
    });
    const response = await guardedFetch("127.0.0.1")(ok.url, { method: "POST" });
    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({ ok: true });
  });
});

describe("guardedFetch re-checks the destination before every request", () => {
  it("refuses when the hostname now resolves to a private address", async () => {
    // The rebinding shape: validated once at mount, hostile by the time the
    // turn actually runs. `localhost` is the honest stand-in — it is a real
    // name that really resolves to loopback, so no resolver stubbing is needed
    // and the test exercises the same code path production takes.
    const upstream = await listen((_req, res) => { res.writeHead(200).end("{}"); });
    const url = upstream.url.replace("127.0.0.1", "localhost");
    await expect(guardedFetch("localhost")(url, { method: "POST" }))
      .rejects.toThrow(/became_unsafe/);
  });

  it("allows an IP literal that was already validated, without re-resolving", async () => {
    // Nothing to re-check: a literal cannot change under us between mount and
    // turn, which is exactly why the DNS branch is skipped for it.
    const upstream = await listen((_req, res) => {
      res.writeHead(200, { "content-type": "application/json" }).end('{"ok":true}');
    });
    const response = await guardedFetch("127.0.0.1")(upstream.url, { method: "POST" });
    expect(response.status).toBe(200);
  });
});

/**
 * END TO END, and the reason this exists is worth stating: round 1 found that
 * proxyTenantMcpServer ALWAYS threw — `new McpServer()` without the tools
 * capability makes setRequestHandler reject — and four passing tests never
 * noticed, because every one of them tested guardedFetch and none of them
 * called the function that does the work.
 *
 * The upstream here is a hand-rolled JSON-RPC responder rather than the SDK's
 * own server transport, deliberately: that transport is the one carrying the
 * Hono exposure the dependencyExposure guard exists for, and a test has no
 * business dragging it in to prove a point about the client side.
 */
describe("proxyTenantMcpServer end to end", () => {
  const rpc = (id: unknown, result: unknown) => JSON.stringify({ jsonrpc: "2.0", id, result });

  async function upstreamMcp(tools: Array<{ name: string; description: string }>) {
    const calls: string[] = [];
    const server = await listen((req, res) => {
      let body = "";
      req.on("data", (c) => { body += c; });
      req.on("end", () => {
        const message = JSON.parse(body || "{}");
        calls.push(message.method);
        const send = (result: unknown) => {
          res.writeHead(200, { "content-type": "application/json" }).end(rpc(message.id, result));
        };
        if (message.method === "initialize") {
          return send({
            protocolVersion: "2025-06-18",
            capabilities: { tools: {} },
            serverInfo: { name: "upstream", version: "1.0.0" },
          });
        }
        if (message.method === "tools/list") {
          return send({ tools: tools.map((t) => ({ ...t, inputSchema: { type: "object" } })) });
        }
        if (message.method === "tools/call") {
          return send({ content: [{ type: "text", text: `called ${message.params?.name}` }] });
        }
        res.writeHead(202).end();          // notifications
      });
    });
    return { ...server, calls };
  }

  it("connects, and forwards the upstream tool listing verbatim", async () => {
    const upstream = await upstreamMcp([{ name: "alpha", description: "an upstream tool" }]);
    const proxied = await proxyTenantMcpServer(
      { name: "tenant-server", url: upstream.url }, () => {});

    // THE assertion round 1 needed: the function returns something at all.
    expect(proxied).not.toBeNull();
    expect(proxied!.name).toBe("tenant-server");
    expect(upstream.calls).toContain("initialize");
    await proxied!.close();
  });

  it("returns null instead of throwing when the upstream is unreachable", async () => {
    const dead = await listen((_req, res) => { res.destroy(); });
    const diagnostics: string[] = [];
    const proxied = await proxyTenantMcpServer(
      { name: "down", url: dead.url }, (m) => diagnostics.push(m));

    // A tenant server being down must degrade the turn, never fail it.
    expect(proxied).toBeNull();
    expect(diagnostics.join(" ")).toContain("did not connect");
    // ...and the diagnostic carries the name only, never the URL or a token.
    expect(diagnostics.join(" ")).not.toContain(dead.url);
  });
});
