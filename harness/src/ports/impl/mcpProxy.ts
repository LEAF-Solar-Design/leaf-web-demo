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
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

import { isForbiddenMcpAddress, type McpServerConfig } from "./mcpBridge.js";

/** How long to wait for the upstream handshake before giving up on a server. */
const CONNECT_TIMEOUT_MS = 10_000;

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
export function guardedFetch(originalHost: string): typeof fetch {
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
    return fetch(url, { ...init, redirect: "error" });
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
): Promise<ProxiedMcpServer | null> {
  const url = new URL(config.url);
  const transport = new StreamableHTTPClientTransport(url, {
    fetch: guardedFetch(url.hostname),
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
    // FORWARD THE PARAMS, including the pagination cursor. Calling listTools()
    // bare made every page request return page one, so a downstream client
    // paging through a large tenant catalogue would loop on the first page
    // forever. Round 3 caught this; it is a product bug, not a test gap.
    instance.server.setRequestHandler(ListToolsRequestSchema, async (request) =>
      client.listTools(request.params));
    instance.server.setRequestHandler(CallToolRequestSchema, async (request) =>
      client.callTool(request.params));
  } catch (error) {
    // The connection already succeeded, so anything failing here would leave a
    // live upstream client with no owner. Close it before giving up.
    await client.close().catch(() => {});
    report(`[leaf-mcp] tenant MCP server could not be proxied: ${JSON.stringify(config.name)}`);
    return null;
  }

  return { name: config.name, instance, close: () => client.close() };
}
