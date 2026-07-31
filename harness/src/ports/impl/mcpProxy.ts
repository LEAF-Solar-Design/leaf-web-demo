/**
 * The guarded upstream transport for tenant MCP servers.
 *
 * WHY THIS EXISTS. mcpBridge validates a tenant MCP server's HOST — at set time
 * and again by DNS at mount time. Host validation alone cannot make a
 * tenant-controlled URL safe, and PR #360's review proved it twice over:
 *
 *   1. REDIRECTS. Handing the Agent SDK `{ type: "http", url }` means the SDK
 *      builds the transport, and its fetch follows redirects. An approved
 *      public endpoint can answer 307 and send the MCP POST to 169.254.169.254.
 *   2. DNS REBINDING. Validating by resolution and then letting someone else
 *      connect is two lookups; the address can change in between.
 *
 * Neither is fixable while the SDK owns the connection. So this module takes it
 * over: it connects upstream itself, through a transport that refuses redirects
 * outright and re-checks the destination immediately before every request, and
 * exposes the result as an in-process MCP server. Redirects are CLOSED;
 * rebinding is NARROWED to a one-lookup window, not eliminated — see
 * guardedFetch for why pinning the address is not available on node, and do
 * not describe this as a completed rebinding defence. `McpServerConfig` accepts
 * `{ type: "sdk", instance }` alongside the URL form, so the Agent SDK never
 * sees a tenant-controlled URL at all — it talks to a local object.
 *
 * The proxy is deliberately DUMB: it forwards tool listing and tool calls and
 * nothing else. It is a network boundary, not a policy layer; the policy lives
 * in ConverseLoop's deny rules and the app gate, exactly where it did before.
 */

import { lookup as dnsLookup } from "node:dns/promises";
import { isIP } from "node:net";

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import {
  CallToolRequestSchema,
  CallToolResultSchema,
  ListToolsRequestSchema,
  ListToolsResultSchema,
} from "@modelcontextprotocol/sdk/types.js";

import { isForbiddenMcpAddress, type McpServerConfig } from "./mcpBridge.js";

/** How long to wait for the upstream handshake before giving up on a server. */
const CONNECT_TIMEOUT_MS = 10_000;

/**
 * How long an upstream tool call may run.
 *
 * MUST BE SET EXPLICITLY. The SDK applies DEFAULT_REQUEST_TIMEOUT_MSEC = 60_000
 * whenever `options.timeout` is undefined (shared/protocol.js), so leaving it
 * off silently caps every tenant tool at 60s — while the harness itself allows
 * a 120s request (REQUEST_TIMEOUT_MS in server.ts). A tool that legitimately
 * runs 60-120s would fail at the proxy well before the turn deadline, and the
 * default is invisible at this call site, which is what makes it dangerous.
 *
 * Matched to the harness budget rather than guessed: the proxy should not be
 * the component that decides a turn is over.
 */
const UPSTREAM_REQUEST_TIMEOUT_MS = 120_000;

export type ProxiedMcpServer = { name: string; instance: McpServer; close: () => Promise<void> };

/**
 * A fetch that refuses every redirect and re-checks the destination immediately
 * before each request.
 *
 * REDIRECTS ARE FULLY CLOSED. `redirect: "error"` makes fetch throw instead of
 * following, and it is applied here rather than taken from the caller so it
 * cannot be dropped. That is the hole that mattered: without it an approved
 * public host answers 307 and the MCP POST lands on 169.254.169.254.
 *
 * REBINDING IS NARROWED, NOT ELIMINATED, and the difference is worth stating
 * plainly. The obvious pin — rewrite the URL's host to the validated IP and
 * carry the real name in a Host header — DOES NOT WORK on node: `host` is a
 * forbidden header name, so fetch overwrites it with the IP. That breaks
 * virtual hosting and, on https, the certificate name. An earlier version of
 * this function did exactly that, and its own test caught the Host header
 * arriving as `127.0.0.1:PORT`.
 *
 * So this re-resolves the hostname and refuses if any answer is now unsafe,
 * then dials by NAME. The window between that check and the connect is one
 * lookup wide instead of the whole mount-to-turn lifetime. Closing it to zero
 * needs an undici Dispatcher with a custom `connect.lookup`, which keeps the
 * URL intact while choosing the address — the named follow-up on this PR.
 */
export function guardedFetch(
  originalHost: string,
  // THE DEADLINE BELONGS HERE, at the socket. Round 9: aborting the SDK request
  // cancels its protocol bookkeeping but never reaches `transport.send` — the
  // transport dials with its OWN process-wide controller
  // (client/streamableHttp.js:304), so a timed-out POST or SSE body stays open
  // until the entire proxy closes. Reproduced by the reviewer: the call
  // rejected at 149ms and the socket was still open 250ms later.
  //
  // A per-request timeout signal on the fetch itself is what actually closes
  // the connection, and a tenant that stops answering must not be able to pin
  // sockets open for the life of the proxy.
  requestTimeoutMs: number,
): typeof fetch {
  return (async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
    const url = typeof input === "string" || input instanceof URL ? new URL(input.toString()) : new URL(input.url);
    // An IP literal was already validated and cannot change under us; only a
    // NAME needs re-checking.
    if (!isIP(originalHost)) {
      const answers = await dnsLookup(originalHost, { all: true, verbatim: true });
      if (!answers.length || answers.some((a) => isForbiddenMcpAddress(a.address))) {
        throw new Error("mcp_upstream_host_became_unsafe");
      }
    }
    // Keep whatever signal the transport supplied (that is how close() still
    // tears everything down) and add our own wall on top.
    const signal = init?.signal
      ? AbortSignal.any([init.signal, AbortSignal.timeout(requestTimeoutMs)])
      : AbortSignal.timeout(requestTimeoutMs);
    return fetch(url, { ...init, redirect: "error", signal });
  }) as typeof fetch;
}

/**
 * Connect to one validated tenant MCP server and wrap it as an in-process
 * server the Agent SDK can mount without ever seeing the tenant's URL.
 *
 * Returns null when the upstream cannot be reached or does not complete the
 * handshake in time — a tenant server being down must degrade the turn, never
 * fail it, which is the same posture resolveMcpAttachment already takes for a
 * server it refuses.
 */
export async function proxyTenantMcpServer(
  config: McpServerConfig,
  report: (message: string) => void = console.error,
  // Internal, NOT read from `config`: the tenant must not get to choose how
  // long we will hold a connection open for them. Exists so the deadline can be
  // tested in milliseconds instead of minutes.
  requestTimeoutMs: number = UPSTREAM_REQUEST_TIMEOUT_MS,
): Promise<ProxiedMcpServer | null> {
  const url = new URL(config.url);
  const transport = new StreamableHTTPClientTransport(url, {
    fetch: guardedFetch(url.hostname, requestTimeoutMs),
    requestInit: config.authToken ? { headers: { Authorization: `Bearer ${config.authToken}` } } : {},
  });

  const client = new Client({ name: "leaf-mcp-proxy", version: "1.0.0" });
  const timer = setTimeout(() => { void client.close().catch(() => {}); }, CONNECT_TIMEOUT_MS);
  try {
    await client.connect(transport);
  } catch {
    clearTimeout(timer);
    // The URL and token never enter a diagnostic; describeConfig is the only
    // formatter allowed to render a tenant config, and the caller logs that.
    report(`[leaf-mcp] tenant MCP server did not connect: ${JSON.stringify(config.name)}`);
    return null;
  }
  clearTimeout(timer);

  // `capabilities: { tools: {} }` is REQUIRED, not decorative: without it the
  // first setRequestHandler below throws "Server does not support tools".
  // Round 1 found that this function therefore never worked at all — my tests
  // covered guardedFetch and never once called proxyTenantMcpServer, so a
  // fatal runtime failure shipped behind four passing tests. The end-to-end
  // test added alongside this fix is the actual remedy; the capability is just
  // the line it exposed.
  const instance = new McpServer(
    { name: config.name, version: "1.0.0" },
    { capabilities: { tools: {} } },
  );
  // RAW forwarding through the low-level server, deliberately. Registering
  // tools through McpServer's typed helper would require translating each
  // upstream JSON Schema into zod and back, and every translation is a chance
  // to change what the model is told a tool accepts. Passing the upstream's own
  // listing through untouched cannot drift from it.
  try {
    // `client.request(...)`, NOT `client.listTools()` / `client.callTool()`.
    // Those are typed helpers that apply CLIENT-SIDE POLICY on top of the wire
    // call, and policy is the one thing this module must not add:
    //
    //   - callTool REFUSES a tool whose `execution.taskSupport` is "required",
    //     throwing InvalidRequest (-32600) locally. The refusal is armed by the
    //     listTools we forward just above (it populates the client's cache), so
    //     a tenant server offering a task-required tool would list perfectly
    //     and then fail EVERY call, without one byte reaching upstream.
    //   - callTool also re-validates `structuredContent` against the tool's
    //     outputSchema and throws when a tool declaring one returns none. That
    //     turns an upstream RESPONSE into a proxy-side EXCEPTION, so the model
    //     never sees what the tenant server actually said.
    //
    // The real downstream client applies both of those itself, where they
    // belong. Doing it here too is double enforcement that can only subtract.
    //
    // FORWARDING IS NOT BYTE-LOSSLESS, and an earlier version of this comment
    // wrongly claimed it was. Measured against SDK 1.29.0: result-LEVEL unknown
    // fields survive (those schemas are z.looseObject), but unknown fields
    // nested inside a tool DEFINITION or a content BLOCK are stripped. Round 6
    // caught the false claim.
    //
    // The proxy cannot fix that, and it is worth knowing why before someone
    // tries: the same stripping happens at the DOWNSTREAM client's own parse.
    // A handler returning `{name, inputSchema, vendorX}` directly, with no
    // proxy in the path at all, still reaches a downstream `listTools()` with
    // vendorX gone. Making this side permissive would buy nothing end to end,
    // so the honest thing is to state the limit rather than add machinery that
    // does not move it.
    //
    // FORWARD THE PARAMS, including the pagination cursor. Calling listTools()
    // bare made every page request return page one, so a downstream client
    // paging through a large tenant catalogue would loop on the first page
    // forever. Round 3 caught this; it is a product bug, not a test gap.
    instance.server.setRequestHandler(ListToolsRequestSchema, async (request, extra) => {
      const listed = await client.request(
        { method: "tools/list", params: request.params },
        ListToolsResultSchema,
        { signal: extra.signal, timeout: UPSTREAM_REQUEST_TIMEOUT_MS },
      );
      // DO NOT ADVERTISE WHAT WE CANNOT INVOKE. A tool declaring
      // `execution.taskSupport: "required"` is callable only through the
      // experimental task methods (tasks/create and friends), which this proxy
      // does not implement and does not negotiate as a capability. Forwarding
      // it would put a tool in front of the model that fails on every call, and
      // a spec-compliant upstream is entitled to reject the plain tools/call we
      // would send.
      //
      // Round 6 caught this: an earlier test appeared to prove task-required
      // tools worked, but only because the FIXTURE answered a plain call that a
      // compliant server would refuse. Dropping them is the honest surface —
      // the tenant's other tools keep working, and nothing silently pretends.
      // Supporting them properly means implementing task forwarding, which is a
      // separate change, not a comment.
      const usable = listed.tools.filter((tool) => tool.execution?.taskSupport !== "required");
      if (usable.length !== listed.tools.length) {
        report(`[leaf-mcp] ${JSON.stringify(config.name)}: hid ${listed.tools.length - usable.length} task-required tool(s); task execution is not proxied`);
      }
      return { ...listed, tools: usable };
    });
    instance.server.setRequestHandler(CallToolRequestSchema, async (request, extra) => {
      // ONLY RELAY PROGRESS THE DOWNSTREAM ASKED FOR. Round 7: falling back to
      // `extra.requestId` when no token was supplied emits notifications for a
      // token the downstream never registered, which is an "unknown token"
      // protocol error rather than a helpful extra. No token means no progress
      // subscription, so there is nothing to forward.
      const progressToken = request.params?._meta?.progressToken;

      // ONE DEADLINE, OURS. The SDK's timeout trio does not bound what it looks
      // like it bounds: `_resetTimeout` runs ONLY from the progress handler
      // (shared/protocol.js:434-436) and compares elapsed time at that instant,
      // so a progress notification arriving just under `maxTotalTimeout` grants
      // a FULL fresh `timeout` window. With both set to 120s the real ceiling
      // is ~240s, and a tenant server emitting progress on a timer picks the
      // moment. Round 8 caught that; round 7's fix (setting maxTotalTimeout)
      // did not actually fix it.
      //
      // Rather than tune three interacting knobs until they happen to compose,
      // this drops all of them and keeps a single wall clock: an AbortSignal
      // driven by setTimeout, progress-independent by construction. One
      // authority for "how long may a tenant hold this connection", which is
      // also the only version of this that can be tested without racing the
      // SDK's internals.
      const deadline = new AbortController();
      const timer = setTimeout(
        () => deadline.abort(new Error("mcp_upstream_call_timeout")),
        requestTimeoutMs,
      );
      // Either the downstream giving up or our wall expiring ends the call.
      const signal = AbortSignal.any([extra.signal, deadline.signal]);

      try {
        return await client.request(
          { method: "tools/call", params: request.params },
          CallToolResultSchema,
          {
            // Cancellation must reach upstream, and this signal carries BOTH
            // reasons: the downstream giving up, and our deadline expiring.
            // Without it a tenant call runs on with nobody reading the answer.
            //
            signal,
            // SET THIS EXPLICITLY. Omitting it does not mean "no timeout" — it
            // means `options?.timeout ?? DEFAULT_REQUEST_TIMEOUT_MSEC`
            // (shared/protocol.js:712), i.e. the hidden 60s cap round 6 found,
            // firing at half the budget. Round 9 caught me removing the option
            // and reintroducing exactly the bug I had fixed.
            //
            // What made round 8's version unsound was `resetTimeoutOnProgress`,
            // not this. Left off (the default), this is a plain timer that no
            // amount of upstream progress can push back, so it agrees with the
            // wall above instead of fighting it. `maxTotalTimeout` is then
            // meaningless and stays unset.
            timeout: requestTimeoutMs,
            // Progress notifications are the only feedback a long tool gives.
            // Swallowing them makes a working call look hung.
            onprogress: progressToken === undefined ? undefined : (progress) => {
              void extra.sendNotification({
                method: "notifications/progress",
                params: { ...progress, progressToken },
              }).catch(() => {});
            },
          },
        );
      } finally {
        clearTimeout(timer);
      }
    });
  } catch (error) {
    // The connection already succeeded, so anything failing here would leave a
    // live upstream client with no owner. Close it before giving up.
    await client.close().catch(() => {});
    report(`[leaf-mcp] tenant MCP server could not be proxied: ${JSON.stringify(config.name)}`);
    return null;
  }

  return { name: config.name, instance, close: () => client.close() };
}
