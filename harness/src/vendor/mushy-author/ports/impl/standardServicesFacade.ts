import type {
  RunnerCapabilityProfile,
  RunnerCapabilityProfileId,
  StandardServiceCall,
  StandardServiceCatalog,
  StandardServiceIdentity,
  StandardServiceProvider,
  StandardServiceTool,
  StandardServiceEnvironment,
} from "./standardServices.js";
import { RUNNER_CAPABILITY_PROFILES, validateStandardServiceCatalog } from "./standardServices.js";

type CallToolResult = { content: Array<{ type: "text"; text: string }>; isError?: boolean };

export interface StandardServicesFacadeSdk {
  tool(
    name: string,
    description: string,
    inputSchema: Record<string, unknown>,
    handler: (args: Record<string, unknown>) => Promise<CallToolResult>,
  ): unknown;
  createSdkMcpServer(options: { name: string; version: string; tools: unknown[] }): unknown;
}

export interface StandardServicesFacadeZod {
  string(): unknown;
  enum(values: readonly string[]): unknown;
  record(inner: unknown): unknown;
  unknown(): unknown;
}

export interface StandardServicesFacadeOptions {
  sdk: StandardServicesFacadeSdk;
  z: StandardServicesFacadeZod;
  provider: StandardServiceProvider;
  identity: StandardServiceIdentity;
  profile?: RunnerCapabilityProfileId | RunnerCapabilityProfile;
  environment: StandardServiceEnvironment;
}

export interface StandardServicesFacadeAttachment {
  server: unknown;
  allowed_tools: string[];
}

export const STANDARD_SERVICES_FACADE_TOOLS = [
  "services_catalog",
  "services_read",
  "services_request",
  "services_confirm",
  "visual_inspect",
  "services_status",
] as const;

const ISSUED_ID = /^[A-Za-z0-9_-]{8,256}$/;
const PUBLIC_STANDARD_SERVICE_ERRORS = new Set([
  "standard_service_approval_identity_mismatch",
  "standard_service_approval_invalid_or_used",
  "standard_service_approval_pending_human",
  "standard_service_denied",
  "standard_service_identity_incomplete",
  "standard_service_read_requires_approval_request",
  "standard_service_read_requires_observe_effect",
  "standard_service_request_effect_not_supported",
  "standard_service_request_requires_explicit_approval",
  "standard_service_tool_not_permitted",
  "standard_service_tool_unknown",
  "standard_service_visual_requires_approval_request",
  "standard_service_visual_target_identity_mismatch",
  "standard_service_visual_target_missing_or_expired",
  "standard_service_visual_target_must_be_issued_id",
]);

function result(value: unknown): CallToolResult {
  return { content: [{ type: "text", text: JSON.stringify(value) }] };
}

function failure(error: unknown): CallToolResult {
  let raw = "";
  try {
    raw = error instanceof Error && typeof error.message === "string" ? error.message : "";
  } catch {
    // A hostile Error subclass may expose message through a throwing getter.
  }
  const safe = PUBLIC_STANDARD_SERVICE_ERRORS.has(raw) ? raw : "standard_service_provider_failure";
  return { content: [{ type: "text", text: safe }], isError: true };
}

function toolKey(tool: Pick<StandardServiceTool, "service_id" | "tool_id">): string {
  return `${tool.service_id}/${tool.tool_id}`;
}

function identityHasClaim(identity: StandardServiceIdentity, claim: string): boolean {
  const value = identity[claim as keyof StandardServiceIdentity];
  return typeof value === "string" && value.trim().length > 0;
}

function profileAllows(
  profile: RunnerCapabilityProfile,
  identity: StandardServiceIdentity,
  environment: StandardServiceEnvironment,
  tool: StandardServiceTool,
): boolean {
  if (!profile.allowed_standard_tools.includes(toolKey(tool))) return false;
  if (!profile.allowed_effects.includes(tool.effect)) return false;
  if (tool.effect === "operator-privileged") return false;
  if (tool.principal !== "tenant-human") return false;
  if (!tool.environments.includes(environment)) return false;
  if (!tool.required_identity_claims.every((claim) => identityHasClaim(identity, claim))) return false;
  if (tool.effect === "observe-contained" && !["none", "issued-target"].includes(tool.egress)) return false;
  if (tool.effect !== "observe-contained" && !["issued-target", "trusted-gateway"].includes(tool.egress)) {
    return false;
  }
  const serviceAllowed = profile.required_standard_services.includes(tool.service_id)
    || profile.optional_standard_services.includes(tool.service_id);
  if (!serviceAllowed) return false;
  const requiredApproval = profile.approval_rules[tool.effect];
  return !requiredApproval || requiredApproval === tool.approval;
}

function findTool(catalog: StandardServiceCatalog, call: StandardServiceCall): StandardServiceTool | null {
  return catalog.tools.find((tool) => tool.service_id === call.service_id && tool.tool_id === call.tool_id) ?? null;
}

async function permittedTool(
  provider: StandardServiceProvider,
  identity: StandardServiceIdentity,
  profile: RunnerCapabilityProfile,
  environment: StandardServiceEnvironment,
  call: StandardServiceCall,
): Promise<StandardServiceTool> {
  const catalog = validateStandardServiceCatalog(await provider.catalog(identity));
  const tool = findTool(catalog, call);
  if (!tool) throw new Error("standard_service_tool_unknown");
  if (!profileAllows(profile, identity, environment, tool)) {
    throw new Error("standard_service_tool_not_permitted");
  }
  return tool;
}

/**
 * Build the only standard-service surface mounted into a model process.
 *
 * The six handlers close over server-side identity. None accepts tenant,
 * subject, session, role, endpoint, token, or URL from the model.
 */
export function createStandardServicesFacade(options: StandardServicesFacadeOptions): StandardServicesFacadeAttachment {
  const { sdk, z, provider, identity } = options;
  const profile = typeof options.profile === "string"
    ? RUNNER_CAPABILITY_PROFILES[options.profile]
    : options.profile ?? RUNNER_CAPABILITY_PROFILES[identity.runner_profile_id];
  if (profile.id !== identity.runner_profile_id) {
    throw new Error("standard_service_profile_identity_mismatch");
  }
  const environment = options.environment;
  if (!["local", "staging", "production"].includes(environment)) {
    throw new Error("standard_service_environment_invalid");
  }

  const callShape = {
    service_id: z.string(),
    tool_id: z.string(),
    arguments: z.record(z.unknown()),
  };
  const tools = [
    sdk.tool(
      "services_catalog",
      "List only the standard services authorized for this identity and runner profile.",
      { query: z.string() },
      async (args) => {
        try {
          const catalog = validateStandardServiceCatalog(await provider.catalog(identity));
          const query = String(args.query ?? "").toLowerCase();
          return result({
            catalog_version: catalog.catalog_version,
            policy_version: catalog.policy_version,
            tools: catalog.tools
              .filter((tool) => profileAllows(profile, identity, environment, tool))
              .filter((tool) => !query || toolKey(tool).includes(query) || tool.description.toLowerCase().includes(query))
              .map((tool) => ({ ...tool, callable: true })),
          });
        } catch (error) { return failure(error); }
      },
    ),
    sdk.tool(
      "services_read",
      "Run one callable read operation. Identity comes from the server-side session, never these arguments.",
      callShape,
      async (args) => {
        const call = {
          service_id: String(args.service_id ?? ""),
          tool_id: String(args.tool_id ?? ""),
          arguments: (args.arguments ?? {}) as Record<string, unknown>,
        };
        try {
          const tool = await permittedTool(provider, identity, profile, environment, call);
          if (tool.effect !== "observe-contained" && tool.effect !== "observe-external") {
            throw new Error("standard_service_read_requires_observe_effect");
          }
          if (tool.approval !== "none") {
            throw new Error("standard_service_read_requires_approval_request");
          }
          return result(await provider.read(identity, call));
        } catch (error) { return failure(error); }
      },
    ),
    sdk.tool(
      "services_request",
      "Request one exact effectful operation. This returns an approval id and does not execute the operation.",
      callShape,
      async (args) => {
        const call = {
          service_id: String(args.service_id ?? ""),
          tool_id: String(args.tool_id ?? ""),
          arguments: (args.arguments ?? {}) as Record<string, unknown>,
        };
        try {
          const tool = await permittedTool(provider, identity, profile, environment, call);
          if (tool.approval !== "explicit-once") {
            throw new Error("standard_service_request_requires_explicit_approval");
          }
          if (tool.effect !== "mutate-tenant" && tool.effect !== "observe-external") {
            throw new Error("standard_service_request_effect_not_supported");
          }
          return result(await provider.request(identity, call));
        } catch (error) { return failure(error); }
      },
    ),
    sdk.tool(
      "services_confirm",
      "Report that a pending request must be approved and executed by the human-authenticated product path.",
      { approval_id: z.string() },
      async () => failure(new Error("standard_service_approval_pending_human")),
    ),
    sdk.tool(
      "visual_inspect",
      "Inspect one server-issued visual target. Arbitrary URLs are not accepted.",
      { inspection_target_id: z.string(), viewport: z.enum(["desktop", "mobile"]) },
      async (args) => {
        try {
          const target = String(args.inspection_target_id ?? "");
          if (!ISSUED_ID.test(target)) throw new Error("standard_service_visual_target_must_be_issued_id");
          const call = { service_id: "visual", tool_id: "inspect-issued-target", arguments: {} };
          const tool = await permittedTool(provider, identity, profile, environment, call);
          if (tool.approval !== "none") {
            throw new Error("standard_service_visual_requires_approval_request");
          }
          return result(await provider.visualInspect(
            identity,
            target,
            args.viewport === "mobile" ? "mobile" : "desktop",
          ));
        } catch (error) { return failure(error); }
      },
    ),
    sdk.tool(
      "services_status",
      "Report gateway and standard-service health for this server-side identity.",
      {},
      async () => {
        try { return result(await provider.status(identity)); }
        catch (error) { return failure(error); }
      },
    ),
  ];

  return {
    server: sdk.createSdkMcpServer({ name: "services", version: "1.0.0", tools }),
    allowed_tools: STANDARD_SERVICES_FACADE_TOOLS.map((name) => `mcp__services__${name}`),
  };
}
