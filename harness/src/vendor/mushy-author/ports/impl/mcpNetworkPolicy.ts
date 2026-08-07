/**
 * Host and address policy for guarded tenant MCP transport.
 * This module contains no credential store and creates no raw SDK attachment.
 */

import { lookup as dnsLookup } from "node:dns/promises";
import { isIP } from "node:net";

export type McpServerConfig = {
  name: string;
  url: string;
  authToken?: string;
};

export type McpHostResolver = (host: string) => Promise<string | { address: string } | Array<string | { address: string }>>;

function unbracketHost(host: string): string {
  return host.startsWith("[") && host.endsWith("]") ? host.slice(1, -1) : host;
}

export function isForbiddenMcpAddress(address: string): boolean {
  const normalized = unbracketHost(address).toLowerCase();
  if (isIP(normalized) === 4) return !isGlobalUnicastIpv4(normalized);
  if (isIP(normalized) === 6) {
    const embedded = embeddedIpv4(normalized);
    if (embedded) return !isGlobalUnicastIpv4(embedded);
    const parts = hextets(normalized);
    if (!parts || parts.some((h) => !Number.isInteger(h))) return true;

    if (parts[0] < 0x2000 || parts[0] > 0x3fff) return true;
    if (inV6Block(parts, [0x2001, 0x0000], 32)) {
      const server = `${(parts[2] >> 8) & 0xff}.${parts[2] & 0xff}.${(parts[3] >> 8) & 0xff}.${parts[3] & 0xff}`;
      const client4 = (parts[6] ^ 0xffff) & 0xffff;
      const client5 = (parts[7] ^ 0xffff) & 0xffff;
      const client = `${(client4 >> 8) & 0xff}.${client4 & 0xff}.${(client5 >> 8) & 0xff}.${client5 & 0xff}`;
      return !isGlobalUnicastIpv4(server) || !isGlobalUnicastIpv4(client);
    }
    return IPV6_SPECIAL_INSIDE_GLOBAL.some(([prefix, bits]) => inV6Block(parts, prefix, bits));
  }
  return true;
}

const IPV6_SPECIAL_INSIDE_GLOBAL: ReadonlyArray<readonly [readonly number[], number]> = [
  [[0x2001], 23],
  [[0x2001, 0x0db8], 32],
  [[0x2002], 16],
  [[0x3ffe], 16],
  [[0x3fff], 20],
] as const;

function inV6Block(parts: number[], prefix: readonly number[], bits: number): boolean {
  let remaining = bits;
  for (let index = 0; index < 8 && remaining > 0; index += 1) {
    const width = Math.min(16, remaining);
    const mask = width === 16 ? 0xffff : (0xffff << (16 - width)) & 0xffff;
    if (((parts[index] ?? 0) & mask) !== ((prefix[index] ?? 0) & mask)) return false;
    remaining -= width;
  }
  return true;
}

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
    ["0.0.0.0", 8],
    ["10.0.0.0", 8],
    ["100.64.0.0", 10],
    ["127.0.0.0", 8],
    ["169.254.0.0", 16],
    ["172.16.0.0", 12],
    ["192.0.0.0", 24],
    ["192.0.2.0", 24],
    ["192.88.99.0", 24],
    ["192.168.0.0", 16],
    ["198.18.0.0", 15],
    ["198.51.100.0", 24],
    ["203.0.113.0", 24],
    ["224.0.0.0", 4],
    ["240.0.0.0", 4],
  ] as const;
  return !special.some(([net, bits]) => inBlock(net, bits));
}

function hextets(address: string): number[] | null {
  const [head, tail] = address.split("::", 2);
  const parse = (part: string): number[] =>
    part ? part.split(":").filter(Boolean).map((h) => Number.parseInt(h, 16)) : [];
  const expand = (part: string): number[] => {
    const dotted = part.match(/(\d+\.\d+\.\d+\.\d+)$/);
    if (!dotted) return parse(part);
    const octets = dotted[1].split(".").map(Number);
    if (octets.some((o) => !Number.isInteger(o) || o < 0 || o > 255)) return [];
    return [
      ...parse(part.slice(0, dotted.index)),
      (octets[0] << 8) | octets[1],
      (octets[2] << 8) | octets[3],
    ];
  };
  const left = expand(head);
  if (tail === undefined) return left.length === 8 ? left : null;
  const right = expand(tail);
  const gap = 8 - left.length - right.length;
  if (gap < 0) return null;
  return [...left, ...Array(gap).fill(0), ...right];
}

function embeddedIpv4(address: string): string | null {
  const parts = hextets(address);
  if (!parts || parts.some((h) => !Number.isInteger(h))) return null;
  const [a, b, c, d, e, f, g, h] = parts;
  const mapped = a === 0 && b === 0 && c === 0 && d === 0 && e === 0 && f === 0xffff;
  const compatible = a === 0 && b === 0 && c === 0 && d === 0 && e === 0 && f === 0;
  const nat64 = a === 0x64 && b === 0xff9b && c === 0 && d === 0 && e === 0 && f === 0;
  if (!mapped && !compatible && !nat64) return null;
  if (compatible && g === 0 && h <= 1) return null;
  return [
    (g >> 8) & 0xff,
    g & 0xff,
    (h >> 8) & 0xff,
    h & 0xff,
  ].join(".");
}

export function isAllowedMcpHost(host: string): boolean {
  const normalized = unbracketHost(host).trim();
  if (!normalized) return false;
  return isIP(normalized) ? !isForbiddenMcpAddress(normalized) : normalized.includes(".");
}

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
