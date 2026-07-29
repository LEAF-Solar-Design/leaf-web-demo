/**
 * Per-tenant MCP attachment storage. Tokens remain in the stored configuration
 * and the SDK attachment only. Diagnostics use describeConfig(), which never
 * renders a token or a URL path.
 */

import { createHash, randomUUID } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
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

function validateConfigs(configs: McpServerConfig[]): void {
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
      validateConfigs(configs);
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
    writeFileSync(temp, content, "utf8");
    renameSync(temp, path);
  }
}

/** Build the remote HTTP configuration accepted directly by sdk.query options. */
export async function resolveMcpAttachment(
  store: McpBridgeStore,
  tenantId: string,
): Promise<McpAttachment | null> {
  const configs = await store.get(tenantId);
  if (!configs?.length) return null;

  const attachment: McpAttachment = {};
  for (const config of configs) {
    attachment[config.name] = {
      type: "http",
      url: config.url,
      ...(config.authToken ? { headers: { Authorization: `Bearer ${config.authToken}` } } : {}),
    };
  }
  return attachment;
}
