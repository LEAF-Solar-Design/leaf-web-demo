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
 *
 * CONNECTION LIFETIME, where five review rounds each found a fresh defect.
 * Items 1 and 2 below are FIXED IN THIS MODULE and regression-tested; item 3
 * is a real limitation that is NOT fixed here:
 *
 *   1. FIXED HERE — cancelling or timing out a call now closes the upstream
 *      socket promptly. The transport dials with its own TRANSPORT-wide
 *      controller (client/streamableHttp.js), so aborting a request used to
 *      reject the promise and leave the socket alive (measured: rejection at
 *      149ms, socket alive 250ms later). The call's deadline now rides
 *      AsyncLocalStorage into guardedFetch, which composes it into every fetch
 *      the call makes; see the socket regression test.
 *   2. SSE RECONNECTION IS TWO-SIDED, and both a claim and its retraction got
 *      it wrong by measuring only half. MEASURED, both halves:
 *
 *      - UNPRIMED (no SSE event id ever arrived): a dropped call stream
 *        REJECTS in ~180ms with no reconnect at all, and the connection-level
 *        GET stream sits at one request over three idle seconds. No loop.
 *      - PRIMED (any event carrying `id:` arrived before the drop): the stream
 *        is resumable, and `_scheduleReconnection` (client/streamableHttp.js)
 *        then loops forever on an upstream that accepts and drops —
 *        `attemptCount` resets to 0 on every ACCEPTED dial, so `maxRetries: 2`
 *        never converges. Measured at one dial per second (the initial delay,
 *        which never grows because it never "fails") until the proxy closes.
 *
 *      So the original "unbounded reconnection" note was right FOR PRIMED
 *      STREAMS, and the retraction that replaced it was overbroad — its
 *      fixture sent headers with no event id, which never arms resumption.
 *      Round 1 of this PR's review caught that. The bound is now explicit:
 *      call-stream resumption inherits the CALL deadline via
 *      AsyncLocalStorage, and every stream dial spends from a per-proxy
 *      STREAM_GET_BUDGET. A budget refusal is a FAILURE, which increments the
 *      SDK's retry counter — turning accept-then-drop back into something
 *      `maxRetries` terminates. Both are regression-tested.
 *
 *      Five drafts of this note were wrong in five different ways — scope,
 *      clocks, maxRetries semantics, the premise, and then the retraction
 *      itself. The lesson compounds: a limitation asserted from code-reading
 *      is a hypothesis, and so is one asserted from a single experiment that
 *      exercised only one path.
 *   3. Upstream progress notifications are not relayed downstream, so a long
 *      tool call gives the model no intermediate feedback.
 *
 * None of these are SSRF holes: redirects stay closed and the DNS re-check
 * still runs on every request, which is what this module exists for. They are
 * resource-lifetime and ergonomics problems.
 */

import { AsyncLocalStorage } from "node:async_hooks";
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

/**
 * The deadline for the tool call currently in flight, readable from inside
 * guardedFetch.
 *
 * WHY AMBIENT STATE, which is normally a smell: there is no other seam. The
 * problem being solved is that aborting an SDK request rejects the pending
 * promise but leaves the upstream SOCKET open — measured previously as a call
 * rejecting at 149ms with its socket still alive 250ms later. Closing it
 * requires the deadline to reach the fetch, and the transport owns its own
 * fetch calls, exposing no per-request hook to pass anything through.
 * AsyncLocalStorage is the only way to get the call's signal down to
 * guardedFetch, which does hold the socket.
 *
 * SSE reconnection justifies it TOO, for the primed half only — an earlier
 * draft claimed reconnection wholesale, its retraction denied it wholesale,
 * and the measured truth is split: an UNPRIMED call stream that drops rejects
 * in ~180ms without reconnecting, but a PRIMED one (an event id arrived) is
 * resumable and re-dials — and those re-dials run inside the call's context,
 * so this signal is what stops them at the call's own deadline.
 */
const activeCallDeadline = new AsyncLocalStorage<AbortSignal>();

/**
 * How many SSE stream dials (HTTP GETs) one proxy instance may spend, total.
 *
 * WHY A LIFETIME CAP AND NOT A RATE. A primed accept-then-drop upstream defeats
 * `maxRetries` structurally — every ACCEPTED dial resets the SDK's attempt
 * counter — so any bound that refills hands the tenant a slower version of the
 * same forever-loop. A cap does not refill. The budget only meters METHOD:GET
 * (the standalone notification stream and stream resumption); tool calls are
 * POSTs and never touch it.
 *
 * The number is generous for what the GET stream is FOR here: this proxy
 * forwards tool listing and calls only, does not relay upstream notifications
 * (header, limitation 3), and a proxy instance lives for one mount, not
 * forever. One initial dial plus seven blip-recoveries is a healthy
 * connection's whole story; a tenant server that flaps more than that has
 * degraded itself, which is the posture this module already takes for a server
 * that is down. A refused dial throws, the SDK counts a FAILURE, and
 * `maxRetries` finally gets to do its job.
 */
const STREAM_GET_BUDGET = 8;

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
  // Shared mutable counter, one per proxy instance — see STREAM_GET_BUDGET.
  // Optional so the redirect/DNS tests (and any caller that makes no SSE
  // streams) keep their existing shape.
  streamBudget?: { remaining: number },
  // Test probe: fires on every metered GET ATTEMPT, including dials the budget
  // refuses. A refused dial never reaches the server, so a fixture counting
  // accepted requests cannot distinguish "the SDK stopped dialling" from "the
  // SDK dials forever and the budget refuses forever" — this counter can.
  onStreamGetAttempt?: () => void,
): typeof fetch {
  return (async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
    const url = typeof input === "string" || input instanceof URL ? new URL(input.toString()) : new URL(input.url);
    // Spend the stream budget BEFORE the DNS re-check: a dial we are refusing
    // anyway should not cost a lookup. GET is precise here — this transport
    // uses GET only to open or resume an SSE stream; messages are POSTs and
    // session teardown is DELETE, and neither is metered.
    const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
    // The probe observes the ATTEMPT, before the budget can refuse it.
    if (method === "GET") onStreamGetAttempt?.();
    if (streamBudget && method === "GET") {
      if (streamBudget.remaining <= 0) throw new Error("mcp_upstream_stream_budget_exhausted");
      streamBudget.remaining -= 1;
    }
    // An IP literal was already validated and cannot change under us; only a
    // NAME needs re-checking.
    if (!isIP(originalHost)) {
      const answers = await dnsLookup(originalHost, { all: true, verbatim: true });
      if (!answers.length || answers.some((a) => isForbiddenMcpAddress(a.address))) {
        throw new Error("mcp_upstream_host_became_unsafe");
      }
    }
    // Compose the CALL's deadline into this fetch, whatever fetch it is: the
    // original POST, an SSE reconnect, a session DELETE. The transport supplies
    // its own signal (that is how close() tears everything down), so keep it
    // and add ours rather than replacing it.
    const callDeadline = activeCallDeadline.getStore();
    const signals = [init?.signal, callDeadline].filter(Boolean) as AbortSignal[];
    const signal = signals.length > 1 ? AbortSignal.any(signals) : signals[0];
    return fetch(url, { ...init, redirect: "error", ...(signal ? { signal } : {}) });
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
  // Internal, NOT read from `config`: a tenant must not choose how long we hold
  // a connection for them. Exists so the deadline is testable in milliseconds.
  requestTimeoutMs: number = UPSTREAM_REQUEST_TIMEOUT_MS,
  // Internal, same rule and same reason: testable without sitting through
  // eight one-second reconnect delays.
  streamGetBudget: number = STREAM_GET_BUDGET,
  // Internal test probe, forwarded to guardedFetch: sees every stream GET
  // ATTEMPT, including dials the budget refuses locally — the only way a test
  // can prove the SDK's reconnect loop actually terminated rather than being
  // silently refused forever.
  onStreamGetAttempt?: () => void,
): Promise<ProxiedMcpServer | null> {
  const url = new URL(config.url);
  const transport = new StreamableHTTPClientTransport(url, {
    fetch: guardedFetch(url.hostname, { remaining: streamGetBudget }, onStreamGetAttempt),
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
      // ONE deadline for the whole call, opened here and visible to every fetch
      // the SDK makes while it runs. `AbortSignal.timeout` starts counting now,
      // so reconnects inherit the REMAINING time rather than a fresh budget —
      // that is the entire difference from the per-fetch attempt this replaces.
      const deadline = AbortSignal.any([
        extra.signal,
        AbortSignal.timeout(requestTimeoutMs),
      ]);
      return activeCallDeadline.run(deadline, () => client.request(
        { method: "tools/call", params: request.params },
        CallToolResultSchema,
        {
          // The composed deadline: downstream cancellation OR our wall. This
          // rejects the pending call; guardedFetch closes the socket, because
          // the same signal reaches the fetch itself.
          signal: deadline,
          // SET THIS EXPLICITLY. Omitting it does not mean "no timeout" — it
          // means `options?.timeout ?? DEFAULT_REQUEST_TIMEOUT_MSEC`
          // (shared/protocol.js:712), i.e. a 60s cap firing at half the
          // harness's own 120s budget, so a tool legitimately running 60-120s
          // would die here for no stated reason.
          //
          // `resetTimeoutOnProgress` is deliberately NOT set. That flag is what
          // makes this subsystem subtle: it lets a progress notification reset
          // the timer, and combined with `maxTotalTimeout` the real ceiling
          // becomes ~2x the budget at a moment the tenant chooses. Left off,
          // this is a plain timer nothing upstream can push back.
          timeout: requestTimeoutMs,
        },
      ));
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
