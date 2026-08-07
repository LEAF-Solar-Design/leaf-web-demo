import { createHash } from "node:crypto";

export type StandardServiceEffect =
  | "observe-contained"
  | "observe-external"
  | "mutate-tenant"
  | "operator-privileged";

export type StandardServiceEnvironment = "local" | "staging" | "production";
export type StandardServiceEgress = "none" | "issued-target" | "public-internet" | "trusted-gateway";
export type StandardServicePrincipal = "tenant-human" | "operator" | "service";
export type StandardServiceApproval = "none" | "explicit-once" | "operator-only";

export interface StandardServiceTool {
  service_id: string;
  tool_id: string;
  description: string;
  input_schema_digest: string;
  output_schema_digest: string;
  effect: StandardServiceEffect;
  egress: StandardServiceEgress;
  principal: StandardServicePrincipal;
  required_identity_claims: string[];
  approval: StandardServiceApproval;
  audit: { retention: string; immutable_receipt: boolean };
  environments: StandardServiceEnvironment[];
}

export interface StandardServiceCatalog {
  catalog_version: string;
  policy_version: string;
  tools: StandardServiceTool[];
}

export interface StandardServiceIdentity {
  tenant_id: string;
  subject_id: string;
  session_id: string;
  /** Opaque account id selected by the grant router. Never a model credential. */
  subscription_mount_id: string;
  runner_profile_id: RunnerCapabilityProfileId;
}

export interface StandardServiceCall {
  service_id: string;
  tool_id: string;
  arguments: Record<string, unknown>;
}

export interface StandardServiceResult {
  content: string;
  artifact_ids?: string[];
  receipt_id?: string;
}

export interface StandardServiceRequestResult {
  approval_id: string;
  argument_digest: string;
  expires_at: string;
  summary: string;
}

export interface StandardServiceVisualResult extends StandardServiceResult {
  inspection_target_id: string;
  media_type?: string;
  image_artifact_id?: string;
}

export interface StandardServiceStatus {
  state: "ready" | "degraded" | "unavailable";
  catalog_digest: string;
  services: Record<string, "ready" | "degraded" | "unavailable">;
  message?: string;
}

/**
 * Gateway-facing port. Identity is supplied by the runner and is never read
 * from model arguments. Implementations must enforce policy again at the
 * gateway boundary. The facade is a narrow transport, not the final guard.
 */
export interface StandardServiceProvider {
  catalog(identity: StandardServiceIdentity): Promise<StandardServiceCatalog>;
  read(identity: StandardServiceIdentity, call: StandardServiceCall): Promise<StandardServiceResult>;
  request(identity: StandardServiceIdentity, call: StandardServiceCall): Promise<StandardServiceRequestResult>;
  confirm(identity: StandardServiceIdentity, approvalId: string): Promise<StandardServiceResult>;
  visualInspect(
    identity: StandardServiceIdentity,
    inspectionTargetId: string,
    viewport: "desktop" | "mobile",
  ): Promise<StandardServiceVisualResult>;
  status(identity: StandardServiceIdentity): Promise<StandardServiceStatus>;
}

export type RunnerCapabilityProfileId = "shell-editor" | "author" | "spine";

export interface RunnerCapabilityProfile {
  profile_version: "1";
  id: RunnerCapabilityProfileId;
  private_mcp_servers: string[];
  optional_private_mcp_servers: string[];
  required_standard_services: string[];
  optional_standard_services: string[];
  allowed_effects: StandardServiceEffect[];
  approval_rules: Partial<Record<StandardServiceEffect, StandardServiceApproval>>;
  missing_service_behavior: "fail" | "degrade";
}

export const RUNNER_CAPABILITY_PROFILES: Readonly<Record<RunnerCapabilityProfileId, RunnerCapabilityProfile>> = {
  "shell-editor": {
    profile_version: "1",
    id: "shell-editor",
    private_mcp_servers: ["repo"],
    optional_private_mcp_servers: [],
    required_standard_services: [],
    optional_standard_services: ["visual"],
    allowed_effects: ["observe-contained"],
    approval_rules: { "observe-contained": "none" },
    missing_service_behavior: "degrade",
  },
  author: {
    profile_version: "1",
    id: "author",
    private_mcp_servers: ["author"],
    optional_private_mcp_servers: ["registry"],
    required_standard_services: [],
    optional_standard_services: [],
    allowed_effects: ["observe-contained", "mutate-tenant"],
    approval_rules: { "mutate-tenant": "explicit-once" },
    missing_service_behavior: "degrade",
  },
  spine: {
    profile_version: "1",
    id: "spine",
    private_mcp_servers: ["spine"],
    optional_private_mcp_servers: [],
    required_standard_services: [],
    optional_standard_services: ["visual"],
    allowed_effects: ["observe-contained", "observe-external", "mutate-tenant"],
    approval_rules: {
      "observe-external": "explicit-once",
      "mutate-tenant": "explicit-once",
    },
    missing_service_behavior: "degrade",
  },
};

const ID = /^[a-z][a-z0-9._-]{0,63}$/;
const VERSION = /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$/;
const DIGEST = /^sha256:[a-f0-9]{64}$/;
const CLAIMS = new Set([
  "tenant_id",
  "subject_id",
  "session_id",
  "subscription_mount_id",
  "runner_profile_id",
]);

function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b));
    return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${stable(item)}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

export function sha256Schema(schema: unknown): string {
  return `sha256:${createHash("sha256").update(stable(schema)).digest("hex")}`;
}

export function standardServiceCatalogDigest(catalog: StandardServiceCatalog): string {
  return `sha256:${createHash("sha256").update(stable(catalog)).digest("hex")}`;
}

export function standardServiceArgumentDigest(call: StandardServiceCall): string {
  return `sha256:${createHash("sha256").update(stable(call)).digest("hex")}`;
}

export function validateStandardServiceCatalog(catalog: StandardServiceCatalog): StandardServiceCatalog {
  if (!catalog || !VERSION.test(catalog.catalog_version) || !VERSION.test(catalog.policy_version)) {
    throw new Error("standard_service_catalog_invalid_version");
  }
  if (!Array.isArray(catalog.tools)) throw new Error("standard_service_catalog_invalid_tools");
  const seen = new Set<string>();
  for (const tool of catalog.tools) {
    const key = `${tool?.service_id ?? ""}/${tool?.tool_id ?? ""}`;
    if (!tool || !ID.test(tool.service_id) || !ID.test(tool.tool_id) || seen.has(key)) {
      throw new Error(`standard_service_catalog_invalid_tool:${key}`);
    }
    seen.add(key);
    if (!tool.description || !DIGEST.test(tool.input_schema_digest) || !DIGEST.test(tool.output_schema_digest)) {
      throw new Error(`standard_service_catalog_invalid_schema:${key}`);
    }
    if (!tool.required_identity_claims.length || tool.required_identity_claims.some((claim) => !CLAIMS.has(claim))) {
      throw new Error(`standard_service_catalog_invalid_identity:${key}`);
    }
    if (!tool.audit?.retention || tool.audit.immutable_receipt !== true || !tool.environments.length) {
      throw new Error(`standard_service_catalog_invalid_governance:${key}`);
    }
    if (tool.effect === "operator-privileged" && tool.approval !== "operator-only") {
      throw new Error(`standard_service_catalog_privileged_without_operator_approval:${key}`);
    }
  }
  return catalog;
}

const visualInput = {
  type: "object",
  required: ["inspection_target_id", "viewport"],
  properties: {
    inspection_target_id: { type: "string" },
    viewport: { enum: ["desktop", "mobile"] },
  },
  additionalProperties: false,
};

export const STANDARD_SERVICE_CATALOG_V1: StandardServiceCatalog = validateStandardServiceCatalog({
  catalog_version: "1",
  policy_version: "1",
  tools: [
    {
      service_id: "visual",
      tool_id: "inspect-issued-target",
      description: "Inspect one server-issued visual target and return DOM, console, layout, and screenshot artifacts.",
      input_schema_digest: sha256Schema(visualInput),
      output_schema_digest: sha256Schema({
        type: "object",
        required: ["inspection_target_id", "content"],
        properties: {
          inspection_target_id: { type: "string" },
          content: { type: "string" },
          image_artifact_id: { type: "string" },
          media_type: { type: "string" },
        },
      }),
      effect: "observe-contained",
      egress: "issued-target",
      principal: "tenant-human",
      required_identity_claims: [
        "tenant_id",
        "subject_id",
        "session_id",
        "subscription_mount_id",
        "runner_profile_id",
      ],
      approval: "none",
      audit: { retention: "security-default", immutable_receipt: true },
      environments: ["local", "staging", "production"],
    },
  ],
});
