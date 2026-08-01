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
import { CallToolResultSchema } from "@modelcontextprotocol/sdk/types.js";

import { guardedFetch, proxyTenantMcpServer } from "../src/ports/impl/mcpProxy.js";

// Round 12: counting HTTP hits could not distinguish "resolves before every
// request" from "resolved once and cached", and the reviewer's cache mutation
// survived the old test. Counting the actual dns.lookup calls is the only
// thing that separates them. vi.hoisted because vi.mock is lifted above the
// imports, so a plain outer binding would not exist yet when the factory runs.
const dnsProbe = vi.hoisted(() => ({ hosts: [] as string[] }));
vi.mock("node:dns/promises", async (importOriginal) => {
  const actual = await importOriginal<typeof import("node:dns/promises")>();
  return {
    ...actual,
    lookup: (host: string, options?: unknown) => {
      dnsProbe.hosts.push(host);
      return (actual.lookup as (h: string, o?: unknown) => Promise<unknown>)(host, options);
    },
  };
});

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

  it("re-checks on EVERY request, not just the first", async () => {
    // Round 11 (WARN): the test below fires a single request, so it could not
    // distinguish "re-checks before each request" from "checked once at mount".
    // The whole point of this guard is the SECOND and later calls — a name that
    // was safe at mount time is exactly the one that turns hostile later.
    const upstream = await listen((_req, res) => {
      res.writeHead(200, { "content-type": "application/json" }).end('{"ok":true}');
    });

    // A NAME, so the DNS branch actually runs, and the refusal must repeat on
    // every attempt — a name that was safe at mount time is precisely the one
    // that turns hostile later, so a once-at-mount check protects nothing.
    const named = upstream.url.replace("127.0.0.1", "localhost");
    const guardedByName = guardedFetch("localhost");
    dnsProbe.hosts.length = 0;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      await expect(guardedByName(named, { method: "POST" })).rejects.toThrow(/became_unsafe/);
    }

    // THE assertion, and the reason it counts lookups rather than HTTP hits:
    // a cached or hoisted single resolution passes every behavioural check
    // here, because the refusal would repeat from the cached answer. Only the
    // call count separates "re-resolves each time" from "resolved once".
    expect(dnsProbe.hosts.filter((host) => host === "localhost")).toEqual(
      ["localhost", "localhost", "localhost"]);
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
          // Declares an outputSchema but answers with content only. The SDK's
          // TYPED callTool helper rejects this locally; a raw request forwards
          // it. That asymmetry is what the raw-forwarding test below detects.
          if (message.params?.name === "schematic") {
            return send({ content: [{ type: "text", text: "no structured content here" }] });
          }
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
        name: "maybe-task",
        description: "task-capable but callable normally",
        inputSchema: { type: "object" as const },
        execution: { taskSupport: "optional" },
      },
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

    // The usable tools survive; only the un-invokable one is gone. Round 11
    // (WARN): without an "optional" tool in the fixture, an OVERBROAD filter
    // that hid every task-aware tool would have passed this just as happily.
    // "optional" means a plain tools/call is valid, so hiding it would silently
    // shrink a tenant's catalogue.
    const listed = await downstream.listTools();
    expect(listed.tools.map((tool) => tool.name)).toEqual(["quick", "maybe-task"]);
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

  it("forwards raw, so a helper-only refusal never fires at the proxy", async () => {
    // ROUND 11 (WARN): every other e2e assertion here would pass unchanged if
    // both handlers were swapped back to the SDK's typed helpers, because none
    // of them trigger a helper-ONLY refusal. The raw-request choice — the whole
    // "this module adds no client-side policy" claim — was therefore untested.
    //
    // This is the asymmetry: `client.callTool()` throws InvalidRequest when a
    // tool declares an outputSchema and the response carries no
    // structuredContent. A raw `client.request()` forwards the upstream's
    // answer as-is and lets the real client decide. A tenant server is entitled
    // to answer this way, and the model should see what it said.
    //
    // THE TWO HANDLERS ARE COUPLED — the same trap as the task-required test,
    // and it caught me a second time. Swapping callTool alone leaves this
    // GREEN, because the outputSchema validator is populated by the TYPED
    // listTools; with a raw tools/list there is no cached validator to fire.
    // A falsification must swap BOTH, or it proves nothing.
    const upstream = await upstreamMcp([{
      name: "schematic",
      description: "declares an output schema",
      inputSchema: { type: "object" as const },
      outputSchema: { type: "object" as const, properties: { value: { type: "number" as const } } },
    }]);
    const proxied = await proxyTenantMcpServer({ name: "raw", url: upstream.url }, () => {});
    const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
    await proxied!.instance.server.connect(serverSide);
    const downstream = new Client({ name: "downstream", version: "1.0.0" });
    await downstream.connect(clientSide);

    // listTools first: that is what arms the helper's outputSchema validator,
    // exactly as it arms the task refusal.
    await downstream.listTools();

    // Raw on the downstream side too, or the SAME rule fires in the test
    // process and masks what the proxy did.
    const result = await downstream.request(
      { method: "tools/call", params: { name: "schematic", arguments: {} } },
      CallToolResultSchema,
    );
    expect(JSON.stringify(result.content)).toContain("no structured content here");
    expect(result.isError).toBeUndefined();

    await downstream.close();
    await proxied!.close();
  });

  it("fails fast when the upstream drops a call stream, and does not loop", async () => {
    // THE ATTACK THE PER-FETCH DESIGN COULD NOT STOP. When the SDK's SSE stream
    // drops it reconnects, and a per-fetch AbortSignal.timeout gives each
    // reconnect a fresh clock. The tenant also controls the retry counter:
    // `_scheduleReconnection` defaults `attemptCount = 0` and only increments on
    // FAILURE, so accept-then-drop cycles never converge. Together those made
    // the call outlive any per-fetch bound.
    //
    // The fix is one deadline per CALL, carried through AsyncLocalStorage so
    // every fetch that call makes — original POST, reconnects, DELETE — sees
    // the SAME signal, still counting down from when the call began.
    const budgetMs = 800;
    const hits: string[] = [];
    const upstream = await listen((req, res) => {
      let body = "";
      req.on("data", (c) => { body += c; });
      req.on("end", () => {
        const message = JSON.parse(body || "{}");
        hits.push(`${req.method}:${message.method ?? "-"}`);
        if (message.method === "initialize") {
          return res.writeHead(200, { "content-type": "application/json" }).end(rpc(message.id, {
            protocolVersion: "2025-06-18",
            capabilities: { tools: {} },
            serverInfo: { name: "flap", version: "1.0.0" },
          }));
        }
        if (message.method === "tools/call" || req.method === "GET") {
          // Accept the stream, then drop it. Repeat forever. Headers alone are
          // enough — the SDK treats the stream as open once they arrive, so no
          // SSE frame is needed to make it reconnect on the drop.
          res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
          setTimeout(() => res.destroy(), budgetMs / 8);
          return;
        }
        res.writeHead(202).end();
      });
    });

    const proxied = await proxyTenantMcpServer(
      { name: "flap", url: upstream.url }, () => {}, budgetMs);
    const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
    await proxied!.instance.server.connect(serverSide);
    const downstream = new Client({ name: "downstream", version: "1.0.0" });
    await downstream.connect(clientSide);

    const started = Date.now();
    await expect(downstream.callTool(
      { name: "x", arguments: {} }, undefined, { timeout: budgetMs * 30 },
    )).rejects.toThrow();
    const elapsed = Date.now() - started;

    // The call dies on the drop rather than being retried into a long stall.
    // Well under the budget, so it is the drop ending it and not our deadline.
    expect(elapsed).toBeLessThan(budgetMs * 0.6);
    expect(hits.filter((hit) => hit.endsWith("tools/call"))).toHaveLength(1);

    // Then sit IDLE. If accept-then-drop reset the retry counter the way the
    // retracted note claimed, connection-level GETs would climb on their own.
    // They do not: this is the assertion that keeps the retraction honest, and
    // it FAILS if a future SDK bump introduces the loop the note used to
    // describe — which is exactly when someone needs to know.
    const getsAfterCall = hits.filter((hit) => hit.startsWith("GET")).length;
    await new Promise((resolve) => setTimeout(resolve, 2000));
    const getsAfterIdle = hits.filter((hit) => hit.startsWith("GET")).length;
    expect(getsAfterIdle - getsAfterCall).toBeLessThanOrEqual(1);

    await downstream.close();
    await proxied!.close();
  }, 60_000);

  it("closes the upstream socket when the call deadline expires", async () => {
    // THE REAL DEFECT, and the only reason this module reaches into
    // AsyncLocalStorage. Aborting an SDK request rejects the pending promise
    // but the transport dials with its OWN controller, so the abandoned socket
    // survived the rejection — measured in review as a call rejecting at 149ms
    // with its socket still alive 250ms later. A tenant that stops answering
    // could accumulate sockets for the life of the proxy.
    //
    // Rejecting is not the property. The socket closing is.
    const budgetMs = 400;
    let callSocket: import("node:net").Socket | undefined;
    const upstream = await listen((req, res) => {
      let body = "";
      req.on("data", (c) => { body += c; });
      req.on("end", () => {
        const message = JSON.parse(body || "{}");
        if (message.method === "initialize") {
          return res.writeHead(200, { "content-type": "application/json" }).end(rpc(message.id, {
            protocolVersion: "2025-06-18",
            capabilities: { tools: {} },
            serverInfo: { name: "silent", version: "1.0.0" },
          }));
        }
        // Hold the call open and never answer.
        if (message.method === "tools/call") { callSocket = req.socket; return; }
        res.writeHead(202).end();
      });
    });

    const proxied = await proxyTenantMcpServer(
      { name: "silent", url: upstream.url }, () => {}, budgetMs);
    const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
    await proxied!.instance.server.connect(serverSide);
    const downstream = new Client({ name: "downstream", version: "1.0.0" });
    await downstream.connect(clientSide);

    await expect(downstream.callTool(
      { name: "forever", arguments: {} }, undefined, { timeout: budgetMs * 25 },
    )).rejects.toThrow();

    // Checked BEFORE closing anything: closing the proxy would reclaim the
    // socket anyway and prove nothing about the deadline.
    await new Promise((resolve) => setTimeout(resolve, 250));
    expect(callSocket).toBeDefined();
    expect(callSocket!.destroyed).toBe(true);

    await downstream.close();
    await proxied!.close();
  }, 30_000);

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
