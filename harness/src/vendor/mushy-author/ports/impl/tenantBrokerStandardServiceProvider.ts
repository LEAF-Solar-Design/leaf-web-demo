import { createHash } from "node:crypto";

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

import type {
  StandardServiceCall,
  StandardServiceArtifactReference,
  StandardServiceCatalog,
  StandardServiceEffect,
  StandardServiceIdentity,
  StandardServiceProvider,
  StandardServiceRequestResult,
  StandardServiceResult,
  StandardServiceStatus,
  StandardServiceTool,
  StandardServiceVisualResult,
} from "./standardServices.js";
import {
  sha256Schema,
  tenantBrokerApprovalDigest,
  validateStandardServiceCatalog,
} from "./standardServices.js";

type GatewayToolResult = {
  content?: Array<{ type?: string; text?: string }>;
  structuredContent?: Record<string, unknown>;
  isError?: boolean;
};

export interface TenantBrokerClient {
  connect(): Promise<void>;
  callTool(name: string, args: Record<string, unknown>): Promise<GatewayToolResult>;
  close(): Promise<void>;
}

export interface TenantBrokerAuthorization {
  bearer_token: string;
  channel_secret: string;
  expires_at: string;
}

export interface TenantBrokerStandardServiceProviderOptions {
  endpoint: string;
  authorization: (
    identity: StandardServiceIdentity,
  ) => Promise<TenantBrokerAuthorization>;
  clientFactory?: (context: {
    identity: StandardServiceIdentity;
    bearerToken: string;
    channelSecret: string;
  }) => Promise<TenantBrokerClient>;
  now?: () => number;
}

const GENERIC_INPUT = { type: "object", additionalProperties: true };
const GENERIC_OUTPUT = { type: "object", additionalProperties: true };
const REQUIRED_IDENTITY = [
  "tenant_id",
  "subject_id",
  "session_id",
  "authority_turn_id",
  "subscription_mount_id",
  "runner_profile_id",
];
const APPROVAL_ID = /^[A-Za-z0-9_-]{8,256}$/;
const ARGUMENT_DIGEST = /^[a-f0-9]{64}$/;
const ARTIFACT_ID = /^[A-Za-z0-9_-]{16,256}$/;

function sameIdentity(a: StandardServiceIdentity, b: StandardServiceIdentity): boolean {
  return a.tenant_id === b.tenant_id
    && a.subject_id === b.subject_id
    && a.session_id === b.session_id
    && a.authority_turn_id === b.authority_turn_id
    && a.subscription_mount_id === b.subscription_mount_id
    && a.runner_profile_id === b.runner_profile_id;
}

function requireTurnIdentity(identity: StandardServiceIdentity): void {
  if (
    !identity.tenant_id
    || !identity.subject_id
    || !identity.session_id
    || !identity.authority_turn_id
    || !identity.subscription_mount_id
    || !identity.runner_profile_id
  ) {
    throw new Error("standard_service_broker_identity_incomplete");
  }
}

function parseToolResult(value: GatewayToolResult): Record<string, unknown> {
  if (value.isError) throw new Error("standard_service_broker_tool_failed");
  if (value.structuredContent && typeof value.structuredContent === "object") {
    return value.structuredContent;
  }
  const text = value.content?.find((item) =>
    item.type === "text" && typeof item.text === "string")?.text;
  if (!text) throw new Error("standard_service_broker_result_missing");
  try {
    const parsed = JSON.parse(text);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("not_object");
    }
    return parsed as Record<string, unknown>;
  } catch {
    throw new Error("standard_service_broker_result_invalid");
  }
}

function requireCompleted(value: Record<string, unknown>): Record<string, unknown> {
  if (value.status === "error") throw new Error("standard_service_broker_rejected");
  if (value.status !== "completed" && value.status !== "ready") {
    throw new Error("standard_service_broker_result_unexpected");
  }
  return value;
}

function mapEffect(value: unknown): StandardServiceEffect {
  switch (value) {
    case "read": return "observe-contained";
    case "external_read": return "observe-external";
    case "write":
    case "external_write": return "mutate-tenant";
    default: throw new Error("standard_service_broker_effect_invalid");
  }
}

function catalogTool(value: unknown): StandardServiceTool {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("standard_service_broker_catalog_invalid");
  }
  const item = value as Record<string, unknown>;
  if (
    typeof item.service_id !== "string"
    || typeof item.tool_id !== "string"
    || typeof item.description !== "string"
    || typeof item.requires_approval !== "boolean"
  ) {
    throw new Error("standard_service_broker_catalog_invalid");
  }
  const effect = mapEffect(item.effect);
  return {
    service_id: item.service_id,
    tool_id: item.tool_id,
    description: item.description,
    input_schema_digest: sha256Schema(GENERIC_INPUT),
    output_schema_digest: sha256Schema(GENERIC_OUTPUT),
    effect,
    egress: effect === "observe-contained" ? "none" : "trusted-gateway",
    principal: "tenant-human",
    required_identity_claims: [...REQUIRED_IDENTITY],
    approval: item.requires_approval ? "explicit-once" : "none",
    audit: { retention: "security-default", immutable_receipt: true },
    environments: ["staging", "production"],
  };
}

function artifactIds(result: unknown): string[] | undefined {
  if (!result || typeof result !== "object" || Array.isArray(result)) return undefined;
  const artifact = (result as Record<string, unknown>).artifact;
  if (!artifact || typeof artifact !== "object" || Array.isArray(artifact)) return undefined;
  const artifactId = (artifact as Record<string, unknown>).artifact_id;
  return typeof artifactId === "string" && artifactId ? [artifactId] : undefined;
}

/** Production adapter for the public tenant broker's six facade tools. */
export class TenantBrokerStandardServiceProvider implements StandardServiceProvider {
  private readonly endpoint: string;
  private readonly authorization: TenantBrokerStandardServiceProviderOptions["authorization"];
  private readonly clientFactory?: TenantBrokerStandardServiceProviderOptions["clientFactory"];
  private readonly now: () => number;
  private lastCatalog?: StandardServiceCatalog;
  private readonly approvals = new Map<string, {
    identity: StandardServiceIdentity;
    call: StandardServiceCall;
    digest: string;
    expiresAtMs: number;
    consumed: boolean;
  }>();
  private readonly visualArtifacts = new Map<string, {
    identity: StandardServiceIdentity;
    reference: StandardServiceArtifactReference;
  }>();

  constructor(options: TenantBrokerStandardServiceProviderOptions) {
    const endpoint = new URL(options.endpoint);
    if (
      endpoint.protocol !== "https:"
      && endpoint.hostname !== "127.0.0.1"
      && endpoint.hostname !== "localhost"
    ) {
      throw new Error("standard_service_broker_endpoint_insecure");
    }
    this.endpoint = endpoint.toString();
    this.authorization = options.authorization;
    this.clientFactory = options.clientFactory;
    this.now = options.now ?? Date.now;
  }

  async catalog(identity: StandardServiceIdentity): Promise<StandardServiceCatalog> {
    requireTurnIdentity(identity);
    const response = await this.withClient(identity, (client) =>
      client.callTool("services_catalog", {}));
    const value = requireCompleted(parseToolResult(response));
    if (!Array.isArray(value.tools)) throw new Error("standard_service_broker_catalog_invalid");
    const tools = value.tools.map(catalogTool);
    const digest = createHash("sha256").update(JSON.stringify(tools)).digest("hex").slice(0, 16);
    const catalog = validateStandardServiceCatalog({
      catalog_version: `broker-${digest}`,
      policy_version: "broker-1",
      tools,
    });
    this.lastCatalog = catalog;
    return structuredClone(catalog);
  }

  async read(identity: StandardServiceIdentity, call: StandardServiceCall): Promise<StandardServiceResult> {
    requireTurnIdentity(identity);
    const value = requireCompleted(parseToolResult(await this.withClient(identity, (client) =>
      client.callTool("services_read", {
        service_id: call.service_id,
        tool_id: call.tool_id,
        arguments: structuredClone(call.arguments),
      }))));
    const ids = artifactIds(value.result);
    return {
      content: JSON.stringify(value.result ?? null),
      ...(ids ? { artifact_ids: ids } : {}),
    };
  }

  async request(
    identity: StandardServiceIdentity,
    call: StandardServiceCall,
  ): Promise<StandardServiceRequestResult> {
    requireTurnIdentity(identity);
    const frozenCall = structuredClone(call);
    const expectedDigest = tenantBrokerApprovalDigest(identity, frozenCall);
    const value = parseToolResult(await this.withClient(identity, (client) =>
      client.callTool("services_request", {
        service_id: frozenCall.service_id,
        tool_id: frozenCall.tool_id,
        arguments: structuredClone(frozenCall.arguments),
      })));
    if (
      value.status !== "approval_required"
      || typeof value.approval_id !== "string"
      || !APPROVAL_ID.test(value.approval_id)
      || typeof value.argument_digest !== "string"
      || !ARGUMENT_DIGEST.test(value.argument_digest)
      || typeof value.expires_at !== "number"
      || !Number.isInteger(value.expires_at)
      || value.expires_at * 1_000 <= this.now()
      || value.argument_digest !== expectedDigest
      || this.approvals.has(value.approval_id)
    ) {
      throw new Error("standard_service_broker_approval_invalid");
    }
    this.approvals.set(value.approval_id, {
      identity: structuredClone(identity),
      call: frozenCall,
      digest: expectedDigest,
      expiresAtMs: value.expires_at * 1_000,
      consumed: false,
    });
    return {
      approval_id: value.approval_id,
      argument_digest: expectedDigest,
      expires_at: new Date(value.expires_at * 1_000).toISOString(),
      summary: `${call.service_id}/${call.tool_id} requires human approval`,
    };
  }

  async confirm(identity: StandardServiceIdentity, approvalId: string): Promise<StandardServiceResult> {
    requireTurnIdentity(identity);
    const pending = this.approvals.get(approvalId);
    if (
      !APPROVAL_ID.test(approvalId)
      || !pending
      || pending.consumed
      || pending.expiresAtMs <= this.now()
      || !sameIdentity(identity, pending.identity)
      || tenantBrokerApprovalDigest(pending.identity, pending.call) !== pending.digest
    ) {
      throw new Error("standard_service_broker_approval_binding_invalid");
    }
    // Consume before the remote call. A lost response must never make an exact
    // one-use effect look safe to retry.
    pending.consumed = true;
    const value = requireCompleted(parseToolResult(await this.withClient(identity, (client) =>
      client.callTool("services_confirm", { approval_id: approvalId }))));
    const ids = pending.call.service_id === "visual"
      && pending.call.tool_id === "inspect-issued-target"
      ? [this.bindVisualArtifact(identity, value.result).artifact_id]
      : artifactIds(value.result);
    return {
      content: JSON.stringify(value.result ?? null),
      ...(ids ? { artifact_ids: ids } : {}),
    };
  }

  async visualInspect(
    identity: StandardServiceIdentity,
    inspectionTargetId: string,
    viewport: "desktop" | "mobile",
  ): Promise<StandardServiceVisualResult> {
    requireTurnIdentity(identity);
    if (!ARTIFACT_ID.test(inspectionTargetId)) {
      throw new Error("standard_service_broker_visual_target_invalid");
    }
    const value = parseToolResult(await this.withClient(identity, (client) =>
      client.callTool("visual_inspect", {
        artifact_id: inspectionTargetId,
        viewport,
      })));
    if (value.status === "approval_required") {
      throw new Error("standard_service_visual_approval_required");
    }
    const completed = requireCompleted(value);
    const artifact = this.bindVisualArtifact(identity, completed.result);
    return {
      inspection_target_id: inspectionTargetId,
      content: JSON.stringify(completed.result ?? null),
      artifact_ids: [artifact.artifact_id],
      image_artifact_id: artifact.artifact_id,
      image_artifact: artifact,
      media_type: artifact.media_type,
    };
  }

  async status(identity: StandardServiceIdentity): Promise<StandardServiceStatus> {
    try {
      requireTurnIdentity(identity);
      const value = requireCompleted(parseToolResult(await this.withClient(identity, (client) =>
        client.callTool("services_status", {}))));
      return {
        state: "ready",
        catalog_digest: this.lastCatalog
          ? createHash("sha256").update(JSON.stringify(this.lastCatalog)).digest("hex")
          : "unknown",
        services: { broker: "ready" },
        ...(typeof value.message === "string" ? { message: value.message } : {}),
      };
    } catch {
      return {
        state: "unavailable",
        catalog_digest: "unknown",
        services: { broker: "unavailable" },
      };
    }
  }

  private async withClient<T>(
    identity: StandardServiceIdentity,
    action: (client: TenantBrokerClient) => Promise<T>,
  ): Promise<T> {
    const client = await this.openClient(identity);
    try {
      return await action(client);
    } finally {
      await client.close().catch(() => {});
    }
  }

  private async openClient(identity: StandardServiceIdentity): Promise<TenantBrokerClient> {
    requireTurnIdentity(identity);
    const authorization = await this.authorization(structuredClone(identity));
    const expiresAt = Date.parse(authorization?.expires_at ?? "");
    if (
      !authorization?.bearer_token
      || typeof authorization.channel_secret !== "string"
      || authorization.channel_secret.length < 32
      || !Number.isFinite(expiresAt)
      || expiresAt <= this.now()
    ) {
      throw new Error("standard_service_broker_authorization_invalid_or_expired");
    }
    if (this.clientFactory) {
      const client = await this.clientFactory({
        identity: structuredClone(identity),
        bearerToken: authorization.bearer_token,
        channelSecret: authorization.channel_secret,
      });
      try {
        await client.connect();
        return client;
      } catch (error) {
        await client.close().catch(() => {});
        throw error;
      }
    }
    const transport = new StreamableHTTPClientTransport(new URL(this.endpoint), {
      requestInit: {
        headers: {
          Authorization: `Bearer ${authorization.bearer_token}`,
          "X-Leaf-Gateway-Channel": authorization.channel_secret,
        },
      },
    });
    const client = new Client({ name: "mushy-tenant-standard-services", version: "1.0.0" });
    const adapter: TenantBrokerClient = {
      connect: () => client.connect(transport),
      callTool: (name, args) => client.callTool({ name, arguments: args }) as Promise<GatewayToolResult>,
      close: () => client.close(),
    };
    try {
      await adapter.connect();
      return adapter;
    } catch (error) {
      await adapter.close().catch(() => {});
      throw error;
    }
  }

  /** Return visual retrieval metadata only to the identity that received it. */
  artifactReference(
    identity: StandardServiceIdentity,
    artifactId: string,
  ): StandardServiceArtifactReference {
    const bound = this.visualArtifacts.get(artifactId);
    if (
      !bound
      || !sameIdentity(identity, bound.identity)
      || Date.parse(bound.reference.expires_at) <= this.now()
    ) {
      throw new Error("standard_service_broker_artifact_binding_invalid");
    }
    return structuredClone(bound.reference);
  }

  private bindVisualArtifact(
    identity: StandardServiceIdentity,
    result: unknown,
  ): StandardServiceArtifactReference {
    if (!result || typeof result !== "object" || Array.isArray(result)) {
      throw new Error("standard_service_broker_visual_artifact_invalid");
    }
    const artifact = (result as Record<string, unknown>).artifact;
    if (!artifact || typeof artifact !== "object" || Array.isArray(artifact)) {
      throw new Error("standard_service_broker_visual_artifact_invalid");
    }
    const item = artifact as Record<string, unknown>;
    if (
      typeof item.artifact_id !== "string"
      || !ARTIFACT_ID.test(item.artifact_id)
      || typeof item.digest !== "string"
      || !ARGUMENT_DIGEST.test(item.digest)
      || item.media_type !== "image/png"
      || typeof item.size !== "number"
      || !Number.isSafeInteger(item.size)
      || item.size <= 0
      || typeof item.expires_at !== "number"
      || !Number.isInteger(item.expires_at)
      || item.expires_at * 1_000 <= this.now()
      || this.visualArtifacts.has(item.artifact_id)
    ) {
      throw new Error("standard_service_broker_visual_artifact_invalid");
    }
    const reference: StandardServiceArtifactReference = {
      artifact_id: item.artifact_id,
      digest: item.digest,
      media_type: "image/png",
      size: item.size,
      expires_at: new Date(item.expires_at * 1_000).toISOString(),
    };
    this.visualArtifacts.set(reference.artifact_id, {
      identity: structuredClone(identity),
      reference,
    });
    return structuredClone(reference);
  }
}
