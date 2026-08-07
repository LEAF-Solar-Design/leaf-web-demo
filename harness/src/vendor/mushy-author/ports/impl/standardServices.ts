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
  authority_turn_id?: string;
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

export interface StandardServiceArtifactReference {
  artifact_id: string;
  digest: string;
  media_type: "image/png";
  size: number;
  expires_at: string;
}

export interface StandardServiceVisualResult extends StandardServiceResult {
  inspection_target_id: string;
  media_type?: string;
  image_artifact_id?: string;
  image_artifact?: StandardServiceArtifactReference;
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
  /** Exact service/tool pairs trusted for this model role. */
  allowed_standard_tools: string[];
  allowed_effects: StandardServiceEffect[];
  approval_rules: Partial<Record<StandardServiceEffect, StandardServiceApproval>>;
  missing_service_behavior: "fail" | "degrade";
}

const CONTAINED_READ_SERVICES = [
  "time",
  "solar-reference",
  "diagram",
  "chart",
  "workspace",
  "code",
  "artifact",
];

const EXTERNAL_READ_SERVICES = [
  "research",
  "visual",
  "deployment",
  "runtime",
];

const TENANT_CHANGE_SERVICES = [
  "build",
  "preview",
  "source",
];

const READ_TOOL_POLICY = [
  "time/convert",
  "solar-reference/get-module",
  "solar-reference/get-inverter",
  "solar-reference/check-compatibility",
  "diagram/render",
  "chart/render",
  "research/search-arxiv",
  "visual/inspect-issued-target",
  "workspace/inspect",
  "code/search",
  "deployment/status",
  "runtime/observe",
  "artifact/read",
];

const CHANGE_TOOL_POLICY = [
  "workspace/change",
  "build/verify",
  "preview/create",
  "source/stage",
];

export const RUNNER_CAPABILITY_PROFILES: Readonly<Record<RunnerCapabilityProfileId, RunnerCapabilityProfile>> = {
  "shell-editor": {
    profile_version: "1",
    id: "shell-editor",
    private_mcp_servers: ["repo"],
    optional_private_mcp_servers: [],
    required_standard_services: [],
    optional_standard_services: [...CONTAINED_READ_SERVICES, ...EXTERNAL_READ_SERVICES],
    allowed_standard_tools: [...READ_TOOL_POLICY],
    allowed_effects: ["observe-contained", "observe-external"],
    approval_rules: {
      "observe-contained": "none",
      "observe-external": "explicit-once",
    },
    missing_service_behavior: "degrade",
  },
  author: {
    profile_version: "1",
    id: "author",
    private_mcp_servers: ["author"],
    optional_private_mcp_servers: ["registry"],
    required_standard_services: [],
    optional_standard_services: [
      ...CONTAINED_READ_SERVICES,
      ...EXTERNAL_READ_SERVICES,
      ...TENANT_CHANGE_SERVICES,
    ],
    allowed_standard_tools: [...READ_TOOL_POLICY, ...CHANGE_TOOL_POLICY],
    allowed_effects: ["observe-contained", "observe-external", "mutate-tenant"],
    approval_rules: {
      "observe-contained": "none",
      "observe-external": "explicit-once",
      "mutate-tenant": "explicit-once",
    },
    missing_service_behavior: "degrade",
  },
  spine: {
    profile_version: "1",
    id: "spine",
    private_mcp_servers: ["spine"],
    optional_private_mcp_servers: [],
    required_standard_services: [],
    optional_standard_services: [
      ...CONTAINED_READ_SERVICES,
      ...EXTERNAL_READ_SERVICES,
      ...TENANT_CHANGE_SERVICES,
    ],
    allowed_standard_tools: [...READ_TOOL_POLICY, ...CHANGE_TOOL_POLICY],
    allowed_effects: ["observe-contained", "observe-external", "mutate-tenant"],
    approval_rules: {
      "observe-contained": "none",
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
  "authority_turn_id",
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

function pythonJsonString(value: string): string {
  return JSON.stringify(value).replace(/[\u007f-\uffff]/g, (character) =>
    `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`);
}

function pythonJsonNumber(value: number): string {
  if (!Number.isFinite(value)) throw new Error("standard_service_arguments_not_canonical_json");
  if (Object.is(value, -0)) return "0";
  if (Number.isInteger(value)) return String(value);
  const rendered = Math.abs(value) < 0.0001 ? value.toExponential() : String(value);
  const exponent = rendered.match(/^(.+)e([+-]?)(\d+)$/i);
  if (!exponent) return rendered;
  const sign = exponent[2] === "-" ? "-" : "+";
  return `${exponent[1]}e${sign}${exponent[3]!.padStart(2, "0")}`;
}

function comparePythonKeys(a: string, b: string): number {
  const left = [...a].map((character) => character.codePointAt(0)!);
  const right = [...b].map((character) => character.codePointAt(0)!);
  const length = Math.min(left.length, right.length);
  for (let index = 0; index < length; index += 1) {
    if (left[index] !== right[index]) return left[index]! - right[index]!;
  }
  return left.length - right.length;
}

/**
 * Match the tenant broker's Python json.dumps(sort_keys=True, separators=(",", ":"))
 * representation for values that crossed the MCP JSON boundary.
 */
function brokerCanonicalJson(value: unknown): string {
  if (value === null) return "null";
  if (typeof value === "string") return pythonJsonString(value);
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return pythonJsonNumber(value);
  if (Array.isArray(value)) return `[${value.map(brokerCanonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) {
      throw new Error("standard_service_arguments_not_canonical_json");
    }
    const entries = Object.entries(value as Record<string, unknown>)
      .sort(([a], [b]) => comparePythonKeys(a, b));
    if (entries.some(([, item]) => item === undefined
      || typeof item === "function"
      || typeof item === "symbol"
      || typeof item === "bigint")) {
      throw new Error("standard_service_arguments_not_canonical_json");
    }
    return `{${entries.map(([key, item]) =>
      `${pythonJsonString(key)}:${brokerCanonicalJson(item)}`).join(",")}}`;
  }
  throw new Error("standard_service_arguments_not_canonical_json");
}

/** Exact digest contract used by tenant_mcp_broker.ApprovalService. */
export function tenantBrokerApprovalDigest(
  identity: StandardServiceIdentity,
  call: StandardServiceCall,
): string {
  const body = {
    identity: [
      identity.tenant_id,
      identity.subject_id,
      identity.session_id,
      identity.authority_turn_id ?? "",
      identity.subscription_mount_id,
      identity.runner_profile_id,
    ],
    service_id: call.service_id,
    tool_id: call.tool_id,
    arguments: call.arguments,
  };
  return createHash("sha256").update(brokerCanonicalJson(body)).digest("hex");
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
        "authority_turn_id",
        "subscription_mount_id",
        "runner_profile_id",
      ],
      approval: "none",
      audit: { retention: "security-default", immutable_receipt: true },
      environments: ["local", "staging", "production"],
    },
  ],
});
