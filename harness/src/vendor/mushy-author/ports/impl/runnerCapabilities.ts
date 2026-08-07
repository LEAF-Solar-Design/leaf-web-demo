import type {
  RunnerCapabilityProfile,
  RunnerCapabilityProfileId,
} from "./standardServices.js";
import { RUNNER_CAPABILITY_PROFILES } from "./standardServices.js";

export const RESERVED_RUNNER_MCP_NAMES = new Set([
  "repo",
  "spine",
  "author",
  "services",
  "gateway",
  "converse",
]);

export interface RunnerServicesAttachment {
  server: unknown;
  allowed_tools: string[];
}

export interface RunnerCompositionInput {
  profile: RunnerCapabilityProfileId | RunnerCapabilityProfile;
  private_mcp_servers: Record<string, unknown>;
  private_allowed_tools?: string[];
  private_disallowed_tools?: string[];
  services?: RunnerServicesAttachment;
}

export interface RunnerComposition {
  profile: RunnerCapabilityProfile;
  mcpServers: Record<string, unknown>;
  allowedTools?: string[];
  disallowedTools?: string[];
}

function unique(values: string[]): string[] {
  return [...new Set(values)];
}

/**
 * One deterministic MCP composition for every Agent SDK runner role.
 *
 * During Phase 1, callers omit `services` and receive their old private
 * surface byte for byte. Later phases add only the local `services` server.
 */
export function composeRunnerCapabilities(input: RunnerCompositionInput): RunnerComposition {
  const profile = typeof input.profile === "string"
    ? RUNNER_CAPABILITY_PROFILES[input.profile]
    : input.profile;
  const mounted = Object.keys(input.private_mcp_servers);
  const allowedPrivate = new Set([...profile.private_mcp_servers, ...profile.optional_private_mcp_servers]);
  const missing = profile.private_mcp_servers.filter((name) => !mounted.includes(name));
  const unexpected = mounted.filter((name) => !allowedPrivate.has(name));
  if (missing.length) throw new Error(`runner_capability_missing_private_server:${missing.join(",")}`);
  if (unexpected.length) throw new Error(`runner_capability_unexpected_private_server:${unexpected.join(",")}`);
  if (mounted.includes("services")) throw new Error("runner_capability_services_name_is_reserved");

  const mcpServers = {
    ...input.private_mcp_servers,
    ...(input.services ? { services: input.services.server } : {}),
  };
  const allowedTools = input.private_allowed_tools || input.services
    ? unique([
        ...(input.private_allowed_tools ?? []),
        ...(input.services ? input.services.allowed_tools : []),
      ])
    : undefined;
  return {
    profile,
    mcpServers,
    ...(allowedTools ? { allowedTools } : {}),
    ...(input.private_disallowed_tools
      ? { disallowedTools: unique(input.private_disallowed_tools) }
      : {}),
  };
}
