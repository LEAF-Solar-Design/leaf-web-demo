import { describe, expect, it } from "vitest";

import { isAllowedMcpHost, isForbiddenMcpAddress } from "../src/ports/impl/mcpBridge.js";

/**
 * Round-1 review found a REAL bypass, reproduced here in the exact spelling the
 * URL parser produces: `new URL("http://[::ffff:127.0.0.1]/")` yields hostname
 * `[::ffff:7f00:1]`, and the old regex only recognised the dotted form, so a
 * connection to it reached a listener bound to 127.0.0.1.
 */
describe("IPv4 embedded in IPv6 is decoded, not pattern-matched", () => {
  it.each([
    ["::ffff:7f00:1", "hex v4-mapped loopback — the reported bypass"],
    ["::ffff:127.0.0.1", "dotted v4-mapped loopback"],
    ["::ffff:a00:1", "hex v4-mapped 10.0.0.1"],
    ["::ffff:a9fe:a9fe", "hex v4-mapped 169.254.169.254 (cloud metadata)"],
    ["::ffff:c0a8:1", "hex v4-mapped 192.168.0.1"],
    ["::a00:1", "v4-compatible 10.0.0.1"],
    ["64:ff9b::a00:1", "NAT64 10.0.0.1"],
    ["64:ff9b::7f00:1", "NAT64 loopback"],
    ["::1", "loopback"],
    ["::", "unspecified"],
    ["fe80::1", "link-local"],
    ["fc00::1", "unique local"],
    // Round 2: the denylist missed all of these, which is why the policy is
    // now an allowlist. Each one is a real internal-routing surface.
    ["100.64.0.1", "carrier-grade NAT, routed inside many deployments"],
    ["198.18.0.1", "benchmark/infrastructure space"],
    ["224.0.0.1", "IPv4 multicast all-hosts"],
    ["240.0.0.1", "reserved"],
    ["255.255.255.255", "broadcast"],
    ["192.0.0.1", "IETF protocol assignments"],
    ["192.88.99.1", "deprecated 6to4 relay anycast"],
    ["ff02::1", "IPv6 multicast all-nodes"],
    ["fec0::1", "deprecated site-local"],
    // Documentation prefixes are not routable; naming one is confusion or probing.
    ["192.0.2.1", "TEST-NET-1"],
    ["198.51.100.1", "TEST-NET-2"],
    ["203.0.113.1", "TEST-NET-3"],
    ["2001:db8::1", "IPv6 documentation"],
    // Round 3: all of these sit INSIDE 2000::/3, so envelope membership
    // is necessary but never sufficient.
    ["2001:2::1", "IPv6 benchmarking (counterpart of 198.18/15)"],
    ["2001:10::1", "ORCHID"],
    ["2001:20::1", "ORCHIDv2"],
    ["2002:7f00:1::", "6to4 transition space wrapping 127.0.0.1"],
    ["3fff::1", "newer documentation prefix"],
    // Round 4: enumerating the children of 2001::/23 left these allowed.
    ["2001:40::1", "unallocated remainder of 2001::/23"],
    ["2001:1ff:ffff:ffff:ffff:ffff:ffff:ffff", "top of 2001::/23"],
    ["3ffe::1", "former 6bone, IANA-reserved"],
    ["3ffe:831f::1", "6bone/Teredo historical"],
    // ...but Teredo must not smuggle a private address through the v6
    // spelling: server 10.0.0.1, and client 127.0.0.1 obfuscated.
    ["2001:0:a00:1:0:0:ffff:fefe", "Teredo with a PRIVATE embedded server"],
    ["2001:0:808:808:0:0:80ff:fffe", "Teredo whose client decodes to 127.0.0.1"],
    ["2002::1", "6to4 transition space"],
    ["3ffe:ffff:ffff:ffff:ffff:ffff:ffff:ffff", "top of 3ffe::/16"],
    ["2001:1::1", "Port Control Protocol anycast, inside 2001::/23"],
    // The envelope's own edges: 2000::/3 is 2000:: through 3fff:ffff…, so
    // anything at or past 4000:: is not global unicast at all.
    ["4000::1", "above the 2000::/3 envelope"],
    ["1fff::1", "below the 2000::/3 envelope"],
  ])("refuses %s (%s)", (address) => {
    expect(isForbiddenMcpAddress(address)).toBe(true);
    expect(isAllowedMcpHost(address)).toBe(false);
  });

  // The other half of the guarantee: decoding must not over-refuse. A public
  // address wearing a v4-in-v6 spelling is still public.
  it.each([
    ["::ffff:5db8:d822", "hex v4-mapped 93.184.216.34"],
    ["::ffff:93.184.216.34", "dotted v4-mapped public"],
    ["64:ff9b::5db8:d822", "NAT64 public"],
    ["2606:4700::1111", "ordinary public v6"],
    // Just OUTSIDE the refused blocks — the over-refusal guard. Two entries I
    // first wrote here were actually INSIDE 2002::/16 and 3ffe::/16, so the
    // code was right and the fixture was wrong; they moved to the refused list.
    ["2000::1", "bottom of global unicast, below 2001::/23"],
    ["2003::1", "just above 6to4"],
    ["2400::1", "APNIC space"],
    ["3ffd::1", "just below 3ffe::/16"],
    // Teredo with PUBLIC embedded addresses: server 8.8.8.8, client
    // 93.184.216.34 obfuscated (XOR ffff per hextet). Round 5 was right
    // that blanket-refusing 2001::/32 over-refuses.
    ["2001:0:808:808:0:0:a247:27dd", "Teredo, both embedded IPv4s public"],
  ])("allows %s (%s)", (address) => {
    expect(isForbiddenMcpAddress(address)).toBe(false);
    expect(isAllowedMcpHost(address)).toBe(true);
  });

  it("decodes the address the URL parser actually hands us", () => {
    // Not a hand-written literal: this is what the parser produces, which is
    // precisely where the original regex lost.
    const hostname = new URL("http://[::ffff:127.0.0.1]/mcp").hostname;
    expect(hostname).toBe("[::ffff:7f00:1]");
    expect(isAllowedMcpHost(hostname)).toBe(false);
  });
});

/**
 * The scope boundary this PR must not cross, pinned so the claim in its
 * description stays honest.
 *
 * Round 1 caught me repeating the trap this whole program keeps hitting: I
 * Standard services enter through one product-owned resolver and a local facade.
 * No runner may reopen the raw tenant bridge.
 */
describe("scope: live runners mount only the tenant broker facade", () => {
  const read = async (relative: string): Promise<string> => {
    const { readFileSync } = await import("node:fs");
    const { fileURLToPath } = await import("node:url");
    return readFileSync(fileURLToPath(new URL(relative, import.meta.url)), "utf8");
  };

  it("ConverseSdkRunner composes the spine plus resolver facade and never touches the bridge", async () => {
    const source = await read("../src/vendor/mushy-author/ports/impl/converseSdkRunner.ts");
    expect(source).toMatch(/composeRunnerCapabilities\(\{[\s\S]*profile:\s*"spine"[\s\S]*private_mcp_servers:\s*\{\s*spine:\s*server\s*\}/);
    expect(source).toMatch(/standardServicesResolver/);
    expect(source).not.toMatch(/resolveEnvMcpAttachment|resolveMcpAttachment|mcpBridge/);
  });

  it("the production composition injects the fixed standard-services resolver", async () => {
    // Round 2 was right that reading the runner alone is not enough: the live
    // selection happens in server.ts's startReal, and a runner swap or wrapper
    // THERE could mount tenant MCP while a runner-only test stayed green.
    const source = await read("../src/server.ts");
    // The one place a converse runner is chosen for production.
    expect(source).toMatch(/new ConverseSdkRunner\(\{[\s\S]*grant,[\s\S]*standardServicesResolver/);
    expect(source).not.toMatch(/resolveEnvMcpAttachment|resolveMcpAttachment|mcpBridge/);
  });

  it("the serve entrypoint composes the same way", async () => {
    // dist/scripts/serve.js is the container's CMD, so this file decides what
    // actually runs in production — see the deployed-entrypoint lesson.
    const source = await read("../scripts/serve.ts");
    expect(source).not.toMatch(/resolveEnvMcpAttachment|resolveMcpAttachment|mcpBridge/);
  });

  it("the legacy runner cannot reopen the old tenant bridge", async () => {
    const dead = await read("../src/vendor/mushy-author/ports/impl/agentSdkTurnRunner.ts");
    expect(dead).not.toMatch(/LEAF_MCP_BRIDGE_DIR|resolveEnvMcpAttachment|resolveMcpAttachment|mcpBridge/);
    expect(dead).toMatch(/mcpServers:\s*\{\s*\[MCP_SERVER_NAME\]:\s*input\.server\s*\}/);
  });
});

/**
 * Round 6 refuted my "unfixable at this layer" claim AND proved the wake-up
 * test I wrote to guard it was vacuous: its regex captured only the type/url/
 * headers lines, so a mutation inserting `fetch?: FetchLike` right after
 * `headers` still passed.
 *
 * The replacement asserts the OPPOSITE thing, because the opposite thing is
 * what is true: the SDK does expose a route that closes redirect SSRF and DNS
 * rebinding — the `{ type: "sdk", instance }` config variant plus a transport
 * that accepts requestInit and a custom fetch. This test exists so nobody
 * (including me, a second time) re-derives "it cannot be done here" from the
 * http variant alone.
 */
describe("the fix route for redirects and rebinding exists in the SDK surface", () => {
  const read = async (relative: string): Promise<string> => {
    const { readFileSync } = await import("node:fs");
    const { fileURLToPath } = await import("node:url");
    return readFileSync(fileURLToPath(new URL(relative, import.meta.url)), "utf8");
  };

  it("McpServerConfig accepts an in-process sdk instance, not only a url", async () => {
    const dts = await read("../node_modules/@anthropic-ai/claude-agent-sdk/sdk.d.ts");
    // The escape hatch: hand the SDK a server INSTANCE we control the transport of.
    expect(dts).toMatch(/type:\s*'sdk';/);
    expect(dts).toMatch(/instance:\s*McpServer/);
    // ...and, the part round 7 caught missing: that the arm is actually IN the
    // union `mcpServers` accepts. Asserting only that the declarations exist
    // left the guard green when the arm was removed from McpServerConfig —
    // the same vacuity as the test this one replaced.
    const union = /export declare type McpServerConfig =([^;]+);/.exec(dts);
    expect(union).not.toBeNull();
    expect(union![1]).toContain("McpSdkServerConfigWithInstance");
  });

  it("the MCP client transport accepts requestInit and a custom fetch", async () => {
    const dts = await read("../node_modules/@modelcontextprotocol/sdk/dist/esm/client/streamableHttp.d.ts");
    // Whole-file assertions, NOT a captured substring — that is exactly how the
    // previous version of this test managed to miss an inserted property.
    expect(dts).toMatch(/requestInit\?:/);
    expect(dts).toMatch(/fetch\?:\s*FetchLike/);
  });
});
