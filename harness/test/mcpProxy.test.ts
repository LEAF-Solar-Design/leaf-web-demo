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
import { afterEach, describe, expect, it, vi } from "vitest";

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";

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

  // Page two carries description and _meta, not just a name. Round 5 (NIT):
  // the pagination assertion compared `tools.map(t => t.name)`, so a proxy that
  // reached page two and then dropped every other field on it stayed green.
  const PAGE_TWO_TOOL = {
    name: "omega",
    description: "second page",
    inputSchema: { type: "object" as const },
    _meta: { "tenant/page": 2 },
  };

  async function upstreamMcp(tools: Array<Record<string, unknown>>) {
    const calls: string[] = [];
    const params: Array<Record<string, unknown>> = [];
    const server = await listen((req, res) => {
      let body = "";
      req.on("data", (c) => { body += c; });
      req.on("end", () => {
        const message = JSON.parse(body || "{}");
        calls.push(message.method);
        if (message.params) params.push(message.params);
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
          // Paginated: page one carries nextCursor, page two is reached only if
          // the proxy forwards the cursor.
          return message.params?.cursor === "page-2"
            ? send({ tools: [PAGE_TWO_TOOL] })
            : send({ tools, nextCursor: "page-2" });
        }
        if (message.method === "tools/call") {
          if (message.params?.name === "explode") {
            return send({ content: [{ type: "text", text: "upstream refused" }], isError: true });
          }
          return send({
            content: [{ type: "text", text: `called ${message.params?.name}` }],
            structuredContent: { echo: message.params?.arguments },
          });
        }
        res.writeHead(202).end();          // notifications
      });
    });
    return { ...server, calls, params };
  }

  it("connects, and forwards the upstream tool listing verbatim", async () => {
    // ANNOTATIONS AND _meta ARE PART OF THE FIXTURE ON PURPOSE. Round 4: the
    // assertion below used objectContaining and named only the three fields
    // this fixture carried, so a proxy that silently dropped a tool's
    // annotations — the hints a client uses to decide whether a tool is
    // read-only or destructive before running it — would still have passed.
    const alpha = {
      name: "alpha",
      description: "an upstream tool",
      inputSchema: { type: "object", properties: { depth: { type: "number" } }, required: ["depth"] },
      outputSchema: { type: "object", properties: { echo: { type: "object" } } },
      annotations: { title: "Alpha", readOnlyHint: false, destructiveHint: true, idempotentHint: false },
      _meta: { "tenant/origin": "upstream-catalogue" },
    };
    const upstream = await upstreamMcp([alpha]);
    const proxied = await proxyTenantMcpServer(
      { name: "tenant-server", url: upstream.url }, () => {});

    expect(proxied).not.toBeNull();
    expect(proxied!.name).toBe("tenant-server");
    expect(upstream.calls).toContain("initialize");

    // ROUND 2: asserting the proxy merely RETURNS left the forwarding handlers
    // unexecuted — deleting tools/list and tools/call would still have passed.
    // So drive it from the DOWNSTREAM side, the way the Agent SDK would, and
    // make the upstream prove it saw the forwarded calls.
    const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
    await proxied!.instance.server.connect(serverSide);
    const downstream = new Client({ name: "downstream", version: "1.0.0" });
    await downstream.connect(clientSide);

    // The whole tool definition must survive BYTE FOR BYTE, not just its name:
    // a proxy that rebuilt definitions instead of forwarding them would pass a
    // name check while silently changing what the model is told a tool accepts.
    // EXACT equality against the upstream object, not objectContaining — the
    // partial form cannot see a dropped field, which is the failure mode that
    // matters most here (a lost destructiveHint downgrades a dangerous tool to
    // a safe-looking one).
    const listed = await downstream.listTools();
    expect(listed.tools).toEqual([alpha]);
    expect(listed.nextCursor).toBe("page-2");

    // PAGINATION: the cursor must reach upstream, or page two repeats page one.
    // Compared WHOLE, not by name: reaching page two and then flattening it is
    // the same loss as never reaching it.
    const page2 = await downstream.listTools({ cursor: "page-2" });
    expect(page2.tools).toEqual([PAGE_TWO_TOOL]);
    expect(upstream.params.some((p) => p.cursor === "page-2")).toBe(true);

    // ARGUMENTS: nested and non-empty, compared exactly at the upstream, so
    // argument loss or mangling cannot pass.
    const args = { depth: 3, nested: { flag: true, list: [1, "two"] } };
    const called = await downstream.callTool({ name: "alpha", arguments: args });
    expect(JSON.stringify(called.content)).toContain("called alpha");
    expect(called.structuredContent).toEqual({ echo: args });
    const callParams = upstream.params.find((p) => p.name === "alpha");
    expect(callParams?.arguments).toEqual(args);

    // ERRORS: isError must survive rather than being flattened into success —
    // AND so must the message. Round 5: asserting only the boolean let a proxy
    // drop the upstream's explanation, which is the part the model reads to
    // correct itself; a bare "it failed" gives it nothing to act on.
    const failed = await downstream.callTool({ name: "explode", arguments: {} });
    expect(failed.isError).toBe(true);
    expect(failed.content).toEqual([{ type: "text", text: "upstream refused" }]);

    // The upstream really received both, rather than the proxy answering itself.
    expect(upstream.calls).toContain("tools/list");
    expect(upstream.calls).toContain("tools/call");

    await downstream.close();
    await proxied!.close();
  });

  it("hides task-required tools rather than advertising ones it cannot invoke", async () => {
    // ROUND 6 REVERSED ROUND 5's FIX HERE, and the correction is the point.
    //
    // Round 5 found that client.callTool() refuses a tool whose
    // `execution.taskSupport` is "required" with -32600, armed by the proxy's
    // own forwarded listTools. True, and the raw-request switch below fixes it.
    // But the test I wrote to prove task-required tools then WORKED was false:
    // it passed only because my fixture answered a plain `tools/call`, which a
    // spec-compliant upstream is entitled to reject. Task-required tools are
    // callable only through the experimental task methods, which this proxy
    // neither implements nor negotiates as a capability.
    //
    // So the honest surface is to not advertise them. Forwarding one would put
    // a tool in front of the model that fails on every call. The tenant's other
    // tools keep working; nothing silently pretends.
    const upstream = await upstreamMcp([
      { name: "quick", description: "ordinary", inputSchema: { type: "object" as const } },
      {
        name: "slow-render",
        description: "a tool the upstream runs as a task",
        inputSchema: { type: "object" as const },
        execution: { taskSupport: "required" },
      },
    ]);
    const diagnostics: string[] = [];
    const proxied = await proxyTenantMcpServer(
      { name: "tasky", url: upstream.url }, (m) => diagnostics.push(m));
    const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
    await proxied!.instance.server.connect(serverSide);
    const downstream = new Client({ name: "downstream", version: "1.0.0" });
    await downstream.connect(clientSide);

    // The usable tool survives; the un-invokable one is gone.
    const listed = await downstream.listTools();
    expect(listed.tools.map((tool) => tool.name)).toEqual(["quick"]);
    // ...and the omission is REPORTED, not silent — an operator seeing a tool
    // missing from a tenant catalogue needs to know why.
    expect(diagnostics.join(" ")).toContain("task-required");

    await downstream.close();
    await proxied!.close();
  });

  it("sets an explicit request timeout instead of inheriting the SDK's hidden 60s", async () => {
    // A WIRING assertion, which this file otherwise argues against, kept for a
    // specific reason: omitting `timeout` does not mean "no timeout", it means
    // `options?.timeout ?? DEFAULT_REQUEST_TIMEOUT_MSEC` (shared/protocol.js),
    // a 60s cap firing at half the harness's own 120s budget. That default is
    // invisible at the call site, and this PR's history includes removing the
    // option under the belief that it meant no timeout — reintroducing the bug
    // it had already fixed. This is the guard against doing that a third time.
    //
    // A behavioural version would have to outlast 60s of wall clock, which is a
    // worse defect than the one it guards.
    const upstream = await upstreamMcp([
      { name: "alpha", description: "t", inputSchema: { type: "object" as const } },
    ]);
    const proxied = await proxyTenantMcpServer({ name: "opts", url: upstream.url }, () => {});
    const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
    await proxied!.instance.server.connect(serverSide);
    const downstream = new Client({ name: "downstream", version: "1.0.0" });
    await downstream.connect(clientSide);

    // Hold the ORIGINAL before spying — calling the prototype property instead
    // re-enters the mock and recurses forever. Both clients issue a tools/call
    // (downstream's, then the proxy's nested upstream one), so entries are
    // tagged by instance; an earlier version restored the spy on the first call
    // it saw, which was the downstream's, and captured nothing under test.
    const original = Client.prototype.request;
    const seen: Array<{ self: unknown; options?: Record<string, unknown> }> = [];
    const spy = vi.spyOn(Client.prototype, "request").mockImplementation(
      function (this: Client, req: never, schema: never, options?: Record<string, unknown>) {
        seen.push({ self: this, options });
        return original.call(this, req, schema, options as never);
      } as never);

    try {
      await downstream.callTool({ name: "alpha", arguments: {} });
    } finally {
      spy.mockRestore();
    }
    const upstreamCalls = seen.filter((entry) => entry.self !== downstream);
    expect(upstreamCalls.length).toBeGreaterThan(0);
    const options = upstreamCalls.at(-1)!.options!;

    expect(options.timeout).toBe(120_000);
    // resetTimeoutOnProgress is what makes this subsystem subtle: it lets a
    // progress frame reset the timer, so with maxTotalTimeout the real ceiling
    // becomes ~2x the budget at a moment the TENANT picks. Both stay unset.
    expect(options.resetTimeoutOnProgress).toBeUndefined();
    expect(options.maxTotalTimeout).toBeUndefined();
    // Downstream cancellation is still forwarded.
    expect(options.signal).toBeInstanceOf(AbortSignal);

    await downstream.close();
    await proxied!.close();
  });

  it("returns null instead of throwing when the upstream is unreachable", async () => {
    const dead = await listen((_req, res) => { res.destroy(); });
    const diagnostics: string[] = [];
    // AN authToken IS SUPPLIED ON PURPOSE. Round 6 (NIT): this test claimed to
    // prove the diagnostic leaks no token while passing a config that had none,
    // so the assertion could never have failed. A secret the test never creates
    // is a secret the test cannot catch.
    const secret = "tenant-secret-do-not-log-3f9a2c";
    const proxied = await proxyTenantMcpServer(
      { name: "down", url: dead.url, authToken: secret }, (m) => diagnostics.push(m));

    // A tenant server being down must degrade the turn, never fail it.
    expect(proxied).toBeNull();
    expect(diagnostics.join(" ")).toContain("did not connect");
    // ...and the diagnostic carries the name only, never the URL or the token.
    expect(diagnostics.join(" ")).not.toContain(dead.url);
    expect(diagnostics.join(" ")).not.toContain(secret);
  });
});
