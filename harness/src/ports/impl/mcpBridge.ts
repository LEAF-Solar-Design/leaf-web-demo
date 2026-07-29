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
import type { McpServerConfig as AgentSdkMcpServerConfig } from "@anthropic-ai/claude-agent-sdk";

export type McpServerConfig = {
  name: string;
  url: string;
  authToken?: string;
};

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

// A tenant named "converse" could replace the local money-gated MCP server.
export const RESERVED_MCP_SERVER_NAMES = new Set(["converse"]);

function isReservedServerName(name: string): boolean {
  return RESERVED_MCP_SERVER_NAMES.has(name.toLowerCase());
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
      if (parsed.protocol !== "http:" && parsed.protocol !== "https:") throw new Error("unsupported protocol");
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
): Promise<McpAttachment | null> {
  const configs = await store.get(tenantId);
  if (!configs?.length) return null;

  const attachment: McpAttachment = {};
  for (const config of configs) {
    if (isReservedServerName(config.name)) {
      report(`[leaf-mcp] skipping reserved tenant MCP server: ${describeConfig(config)}`);
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
