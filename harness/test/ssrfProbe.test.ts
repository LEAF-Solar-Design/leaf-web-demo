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
  ])("refuses %s (%s)", (address) => {
    expect(isForbiddenMcpAddress(address)).toBe(true);
    expect(isAllowedMcpHost(address)).toBe(false);
  });

  // The other half of the guarantee: decoding must not over-refuse. A public
  // address wearing a v4-in-v6 spelling is still public.
  it.each([
    ["::ffff:cb00:7110", "hex v4-mapped 203.0.113.16"],
    ["::ffff:203.0.113.16", "dotted v4-mapped public"],
    ["64:ff9b::cb00:7110", "NAT64 public"],
    ["2606:4700::1111", "ordinary public v6"],
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
 * wrote that the live path "needs this regardless", when in fact the LIVE
 * converse runner mounts no tenant MCP servers at all. The bridge is reached
 * only by AgentSdkTurnRunner, which serve.ts does not construct. So this PR
 * hardens a surface that is currently unreachable in the converse lane, and
 * that is exactly right: admitting tenant MCP servers to the live lane is the
 * HELD design decision (#322), not something to smuggle in behind a security
 * fix.
 */
describe("scope: the live converse runner still mounts no tenant MCP", () => {
  it("ConverseSdkRunner passes only the spine server, and never touches the bridge", async () => {
    const { readFileSync } = await import("node:fs");
    const { fileURLToPath } = await import("node:url");
    const source = readFileSync(
      fileURLToPath(new URL("../src/ports/impl/converseSdkRunner.ts", import.meta.url)), "utf8");

    expect(source).toMatch(/mcpServers:\s*\{\s*spine:\s*server\s*\}/);
    // If this ever fails, tenant MCP reached the live lane and the held #322
    // ruling was bypassed — the mount gate above is then load-bearing, not
    // defence in depth, and the DNS-rebinding residual becomes a blocker.
    expect(source).not.toMatch(/resolveEnvMcpAttachment|resolveMcpAttachment|mcpBridge/);
  });
});
