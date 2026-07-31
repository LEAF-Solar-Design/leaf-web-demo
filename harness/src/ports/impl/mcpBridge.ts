/**
 * Per-tenant MCP attachment storage. Tokens remain in the stored configuration
 * and the SDK attachment only. Diagnostics use describeConfig(), which never
 * renders a token or a URL path.
 *
 * File-backed records hold bearer tokens in plaintext at rest. The deployment
 * must protect the store directory because it is the security boundary for
 * those files.
 */

import { createHash, randomUUID } from "node:crypto";
import { lookup as dnsLookup } from "node:dns/promises";
import {
  closeSync,
  existsSync,
  fsyncSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import { isIP } from "node:net";
import type { McpServerConfig as AgentSdkMcpServerConfig } from "@anthropic-ai/claude-agent-sdk";

export type McpServerConfig = {
  name: string;
  url: string;
  authToken?: string;
};

export type McpHostResolver = (host: string) => Promise<string | { address: string } | Array<string | { address: string }>>;

export interface McpBridgeStore {
  set(tenantId: string, configs: McpServerConfig[]): Promise<void>;
  get(tenantId: string): Promise<McpServerConfig[] | null>;
  delete(tenantId: string): Promise<void>;
}

/** The exact shape accepted by sdk.query({ options: { mcpServers } }). */
export type McpAttachment = Record<string, AgentSdkMcpServerConfig>;

const MAX_CONFIGS_PER_TENANT = 16;
const MAX_SERIALIZED_CONFIG_BYTES = 64 * 1024;
const SERVER_NAME = /^[a-z0-9][a-z0-9._-]{0,63}$/i;

// "converse" remains the exact local server name used in existing diagnostics.
export const RESERVED_MCP_SERVER_NAMES = new Set(["converse"]);
const RESERVED_LOCAL_MCP_NAMESPACE = /^converse(__.*)?$/i;

function isReservedServerName(name: string): boolean {
  return RESERVED_MCP_SERVER_NAMES.has(name.toLowerCase()) || RESERVED_LOCAL_MCP_NAMESPACE.test(name);
}

function tenantHash(tenantId: string): string {
  return createHash("sha256").update(tenantId).digest("hex");
}

function cloneConfigs(configs: McpServerConfig[]): McpServerConfig[] {
  return configs.map((config) => ({ ...config }));
}

function hostForDescription(url: unknown): string {
  if (typeof url !== "string") return "<invalid>";
  try {
    return new URL(url).host || "<invalid>";
  } catch {
    return "<invalid>";
  }
}

function unbracketHost(host: string): string {
  return host.startsWith("[") && host.endsWith("]") ? host.slice(1, -1) : host;
}

/**
 * True when an IP address is not safe for a tenant-controlled MCP endpoint.
 *
 * ALLOWLIST, not a denylist. Round 2 refuted the denylist by naming ranges it
 * missed (100.64/10 CGNAT, 198.18/15 benchmark, 224/4 multicast, 240/4
 * reserved, ff02::1, fec0::1) and the answer to "you missed a range" is not
 * one more range, it is to invert the question: an address is allowed only if
 * it is GLOBAL UNICAST and outside every IANA special-purpose block. Anything
 * unrecognised is refused, so the next special range IANA assigns is refused
 * without a code change.
 *
 * Documentation prefixes (192.0.2/24, 198.51.100/24, 203.0.113/24,
 * 2001:db8::/32) are refused too: they are not routable, so a tenant naming
 * one is either confused or probing.
 */
export function isForbiddenMcpAddress(address: string): boolean {
  const normalized = unbracketHost(address).toLowerCase();
  if (isIP(normalized) === 4) return !isGlobalUnicastIpv4(normalized);
  if (isIP(normalized) === 6) {
    // Embedded IPv4 decides first: ::ffff:7f00:1 IS 127.0.0.1 on the wire, and
    // `new URL("http://[::ffff:127.0.0.1]/")` hands back exactly that hex form,
    // which is where a text-matching version of this check lost (round 1).
    const embedded = embeddedIpv4(normalized);
    if (embedded) return !isGlobalUnicastIpv4(embedded);
    const parts = hextets(normalized);
    if (!parts || parts.some((h) => !Number.isInteger(h))) return true;
    // Global unicast is 2000::/3 and nothing else. That single test refuses
    // ::, ::1, fc00::/7, fe80::/10, fec0::/10 and ff00::/8 without naming them.
    const global = parts[0] >= 0x2000 && parts[0] <= 0x3fff;
    const documentation = parts[0] === 0x2001 && parts[1] === 0x0db8;
    return !global || documentation;
  }
  return true;
}

/** IANA special-purpose IPv4 blocks, as [first octet-matched predicate]. */
function isGlobalUnicastIpv4(address: string): boolean {
  const octets = address.split(".").map(Number);
  if (octets.length !== 4 || octets.some((o) => !Number.isInteger(o) || o < 0 || o > 255)) return false;
  const [a, b, c] = octets;
  const value = ((a << 24) >>> 0) + (b << 16) + (c << 8) + octets[3];
  const inBlock = (net: string, bits: number): boolean => {
    const [na, nb, nc, nd] = net.split(".").map(Number);
    const base = ((na << 24) >>> 0) + (nb << 16) + (nc << 8) + nd;
    const mask = bits === 0 ? 0 : (0xffffffff << (32 - bits)) >>> 0;
    return ((value & mask) >>> 0) === ((base & mask) >>> 0);
  };
  const special = [
    ["0.0.0.0", 8],          // this network
    ["10.0.0.0", 8],         // private
    ["100.64.0.0", 10],      // carrier-grade NAT (round 2)
    ["127.0.0.0", 8],        // loopback
    ["169.254.0.0", 16],     // link-local, incl. cloud metadata
    ["172.16.0.0", 12],      // private
    ["192.0.0.0", 24],       // IETF protocol assignments
    ["192.0.2.0", 24],       // TEST-NET-1
    ["192.88.99.0", 24],     // 6to4 relay anycast (deprecated)
    ["192.168.0.0", 16],     // private
    ["198.18.0.0", 15],      // benchmarking (round 2)
    ["198.51.100.0", 24],    // TEST-NET-2
    ["203.0.113.0", 24],     // TEST-NET-3
    ["224.0.0.0", 4],        // multicast (round 2)
    ["240.0.0.0", 4],        // reserved, incl. 255.255.255.255 broadcast
  ] as const;
  return !special.some(([net, bits]) => inBlock(net, bits));
}

/** The eight hextets of an IPv6 address, or null when it cannot be expanded. */
function hextets(address: string): number[] | null {
  const [head, tail] = address.split("::", 2);
  const parse = (part: string): number[] =>
    part ? part.split(":").filter(Boolean).map((h) => Number.parseInt(h, 16)) : [];
  // A dotted tail (::ffff:1.2.3.4) contributes two hextets, not one.
  const expand = (part: string): number[] => {
    const dotted = part.match(/(\d+\.\d+\.\d+\.\d+)$/);
    if (!dotted) return parse(part);
    const octets = dotted[1].split(".").map(Number);
    if (octets.some((o) => !Number.isInteger(o) || o < 0 || o > 255)) return [];
    return [...parse(part.slice(0, dotted.index)), (octets[0] << 8) | octets[1], (octets[2] << 8) | octets[3]];
  };
  const left = expand(head);
  if (tail === undefined) return left.length === 8 ? left : null;
  const right = expand(tail);
  const gap = 8 - left.length - right.length;
  if (gap < 0) return null;
  return [...left, ...Array(gap).fill(0), ...right];
}

/** The dotted IPv4 embedded in a v6 address (mapped, compatible, or NAT64), else null. */
function embeddedIpv4(address: string): string | null {
  const parts = hextets(address);
  if (!parts || parts.some((h) => !Number.isInteger(h))) return null;
  const [a, b, c, d, e, f, g, h] = parts;
  const mapped = a === 0 && b === 0 && c === 0 && d === 0 && e === 0 && f === 0xffff;
  const compatible = a === 0 && b === 0 && c === 0 && d === 0 && e === 0 && f === 0;
  const nat64 = a === 0x64 && b === 0xff9b && c === 0 && d === 0 && e === 0 && f === 0;
  if (!mapped && !compatible && !nat64) return null;
  // `::` and `::1` are addresses in their own right, not embeddings of
  // 0.0.0.0 / 0.0.0.1 — and both are refused by the 2000::/3 test anyway.
  if (compatible && g === 0 && h <= 1) return null;
  return `${(g >> 8) & 0xff}.${g & 0xff}.${(h >> 8) & 0xff}.${h & 0xff}`;
}

/** Set-time policy. A DNS name must be dotted, and literals must be public. */
export function isAllowedMcpHost(host: string): boolean {
  const normalized = unbracketHost(host).trim();
  if (!normalized) return false;
  return isIP(normalized) ? !isForbiddenMcpAddress(normalized) : normalized.includes(".");
}

/** Execute-time DNS policy. Every resolved address must remain public. */
export async function resolveAllowedMcpHost(
  host: string,
  resolver: McpHostResolver = async (name) => dnsLookup(name, { all: true, verbatim: true }),
): Promise<boolean> {
  if (!isAllowedMcpHost(host)) return false;
  try {
    const resolved = await resolver(unbracketHost(host));
    const answers = Array.isArray(resolved) ? resolved : [resolved];
    return answers.length > 0 && answers.every((answer) => {
      const address = typeof answer === "string" ? answer : answer.address;
      return typeof address === "string" && !isForbiddenMcpAddress(address);
    });
  } catch {
    return false;
  }
}

/** Safe to use in every diagnostic. It deliberately omits URL path/query and token. */
export function describeConfig(config: Pick<McpServerConfig, "name" | "url" | "authToken">): string {
  return `MCP server name=${JSON.stringify(config.name)} host=${JSON.stringify(hostForDescription(config.url))} authToken="<redacted>"`;
}

function validationError(config: McpServerConfig): Error {
  return new Error(`mcp_bridge_invalid_config: ${describeConfig(config)}`);
}

function validateConfigs(configs: McpServerConfig[], allowReservedServerNames = false): void {
  if (!Array.isArray(configs)) {
    throw new Error("mcp_bridge_invalid_configs: expected an array");
  }
  if (configs.length > MAX_CONFIGS_PER_TENANT) {
    throw new Error(`mcp_bridge_too_many_configs: maximum is ${MAX_CONFIGS_PER_TENANT}`);
  }

  for (const config of configs) {
    if (
      !config ||
      typeof config.name !== "string" ||
      !SERVER_NAME.test(config.name) ||
      config.name.endsWith(".") ||
      (!allowReservedServerNames && isReservedServerName(config.name)) ||
      typeof config.url !== "string" ||
      (config.authToken !== undefined && typeof config.authToken !== "string")
    ) {
      throw validationError(config ?? { name: "<invalid>", url: "" });
    }
    try {
      const parsed = new URL(config.url);
      if ((parsed.protocol !== "http:" && parsed.protocol !== "https:") || !isAllowedMcpHost(parsed.hostname)) throw new Error("unsafe host or protocol");
    } catch {
      throw validationError(config);
    }
  }

  let serialized: string;
  try {
    serialized = JSON.stringify(configs);
  } catch {
    throw new Error("mcp_bridge_invalid_configs: configurations must be serializable");
  }
  if (Buffer.byteLength(serialized, "utf8") > MAX_SERIALIZED_CONFIG_BYTES) {
    throw new Error(`mcp_bridge_config_too_large: maximum is ${MAX_SERIALIZED_CONFIG_BYTES} bytes`);
  }
}

export class InMemoryMcpBridgeStore implements McpBridgeStore {
  private readonly byTenant = new Map<string, McpServerConfig[]>();

  async set(tenantId: string, configs: McpServerConfig[]): Promise<void> {
    validateConfigs(configs);
    this.byTenant.set(tenantId, cloneConfigs(configs));
  }

  async get(tenantId: string): Promise<McpServerConfig[] | null> {
    const configs = this.byTenant.get(tenantId);
    return configs ? cloneConfigs(configs) : null;
  }

  async delete(tenantId: string): Promise<void> {
    this.byTenant.delete(tenantId);
  }
}

export interface FileMcpBridgeStoreOptions {
  dir: string;
}

/** JSON files are named by a SHA-256 tenant hash, never the tenant identifier. */
export class FileMcpBridgeStore implements McpBridgeStore {
  private readonly dir: string;

  constructor(options: FileMcpBridgeStoreOptions) {
    this.dir = resolve(options.dir);
    mkdirSync(this.dir, { recursive: true });
  }

  async set(tenantId: string, configs: McpServerConfig[]): Promise<void> {
    validateConfigs(configs);
    const path = this.pathFor(tenantId);
    try {
      this.writeAtomic(path, JSON.stringify(cloneConfigs(configs)) + "\n");
    } catch {
      throw new Error(`mcp_bridge_write_failed: tenant=${tenantHash(tenantId)}`);
    }
  }

  async get(tenantId: string): Promise<McpServerConfig[] | null> {
    const path = this.pathFor(tenantId);
    if (!existsSync(path)) return null;
    try {
      const configs = JSON.parse(readFileSync(path, "utf8")) as McpServerConfig[];
      // Old or tampered files can contain a now-reserved name. Keep the record
      // readable so resolveMcpAttachment can skip it without mounting it.
      validateConfigs(configs, true);
      return cloneConfigs(configs);
    } catch {
      throw new Error(`mcp_bridge_read_failed: tenant=${tenantHash(tenantId)}`);
    }
  }

  async delete(tenantId: string): Promise<void> {
    try {
      rmSync(this.pathFor(tenantId), { force: true });
    } catch {
      throw new Error(`mcp_bridge_delete_failed: tenant=${tenantHash(tenantId)}`);
    }
  }

  private pathFor(tenantId: string): string {
    return join(this.dir, `${tenantHash(tenantId)}.json`);
  }

  private writeAtomic(path: string, content: string): void {
    const temp = join(dirname(path), `.${randomUUID()}.tmp`);
    let fd: number | null = null;
    try {
      fd = openSync(temp, "wx", 0o600);
      writeFileSync(fd, content, "utf8");
      fsyncSync(fd);
      closeSync(fd);
      fd = null;
      renameSync(temp, path);
    } finally {
      // Each cleanup step swallows its OWN failure: a throw here would REPLACE
      // the original write/fsync/rename error (masking the actual fault), and
      // a failing closeSync would skip the rmSync below it — leaving the
      // plaintext-token temp file on disk (review round 2, finding 1). Both
      // are best-effort; the try body's error is the one that must surface.
      if (fd !== null) {
        try { closeSync(fd); } catch { /* already closed / EIO — best effort */ }
      }
      try { rmSync(temp, { force: true }); } catch { /* EBUSY/EPERM — best effort */ }
    }
  }
}

/** Build the remote HTTP configuration accepted directly by sdk.query options. */
export async function resolveMcpAttachment(
  store: McpBridgeStore,
  tenantId: string,
  report: (message: string) => void = console.error,
  resolver?: McpHostResolver,
): Promise<McpAttachment | null> {
  const configs = await store.get(tenantId);
  if (!configs?.length) return null;

  const attachment: McpAttachment = {};
  for (const config of configs) {
    if (isReservedServerName(config.name)) {
      report(`[leaf-mcp] skipping reserved tenant MCP server: ${describeConfig(config)}`);
      continue;
    }
    // Execute-time SSRF gate, not just set-time. The SDK CONNECTS to every
    // mounted URL at session start to list tools, so a hostname that passed
    // set-time validation but now resolves to a private or link-local address
    // (DNS changed, or was always attacker-controlled) must not be handed to
    // it. Fail CLOSED per server: skip and say so, exactly like the reserved-
    // name skip above. Known residual: resolve-then-connect is still two
    // lookups (a rebinding window); closing it fully needs a pinned-IP dialer,
    // which belongs to the held execution follow-up (#322).
    let hostAllowed = false;
    try {
      hostAllowed = resolver
        ? await resolveAllowedMcpHost(new URL(config.url).hostname, resolver)
        : await resolveAllowedMcpHost(new URL(config.url).hostname);
    } catch {
      hostAllowed = false;
    }
    if (!hostAllowed) {
      report(`[leaf-mcp] skipping tenant MCP server with unsafe or unresolvable host: ${describeConfig(config)}`);
      continue;
    }
    attachment[config.name] = {
      type: "http",
      url: config.url,
      ...(config.authToken ? { headers: { Authorization: `Bearer ${config.authToken}` } } : {}),
    };
  }
  return Object.keys(attachment).length ? attachment : null;
}
