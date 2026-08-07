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
import { guardedFetch } from "./mcpProxy.js";
import {
  isAllowedMcpHost,
  resolveAllowedMcpHost,
  type McpHostResolver,
} from "./mcpNetworkPolicy.js";

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

export interface TenantBrokerPendingApprovalBinding {
  approval_id: string;
  identity: StandardServiceIdentity;
  call: StandardServiceCall;
  argument_digest: string;
  expires_at: string;
}

export interface TenantBrokerApprovalStore {
  /** Atomically insert one binding. Return false when the id already exists. */
  create(binding: TenantBrokerPendingApprovalBinding): Promise<boolean>;
  /**
   * Atomically remove and return the exact unexpired binding only when every
   * supplied identity field matches. A mismatch, expiry, replay, or missing id
   * returns null and must not change another principal's record.
   */
  consume(input: {
    approval_id: string;
    identity: StandardServiceIdentity;
    now_ms: number;
  }): Promise<TenantBrokerPendingApprovalBinding | null>;
}

export interface TenantBrokerStandardServiceProviderOptions {
  endpoint: string;
  authorization: (
    identity: StandardServiceIdentity,
  ) => Promise<TenantBrokerAuthorization>;
  approvalStore: TenantBrokerApprovalStore;
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
export const TENANT_BROKER_MAX_RESPONSE_BYTES = 1_048_576;
export const TENANT_BROKER_MAX_REQUEST_BYTES = 1_048_576;
type FetchBody = Exclude<RequestInit["body"], null | undefined>;

function requestUrl(input: string | URL | Request): URL {
  return new URL(typeof input === "string" || input instanceof URL ? input : input.url);
}

function requireRequestSize(bytes: number): void {
  if (!Number.isSafeInteger(bytes) || bytes < 0 || bytes > TENANT_BROKER_MAX_REQUEST_BYTES) {
    throw new Error("standard_service_broker_request_too_large");
  }
}

async function measureKnownBody(body: FetchBody): Promise<void> {
  if (typeof body === "string") {
    requireRequestSize(new TextEncoder().encode(body).byteLength);
    return;
  }
  if (body instanceof URLSearchParams) {
    requireRequestSize(new TextEncoder().encode(body.toString()).byteLength);
    return;
  }
  if (body instanceof Blob) {
    requireRequestSize(body.size);
    return;
  }
  if (body instanceof ArrayBuffer) {
    requireRequestSize(body.byteLength);
    return;
  }
  if (ArrayBuffer.isView(body)) {
    requireRequestSize(body.byteLength);
    return;
  }
  if (body instanceof FormData) {
    try {
      requireRequestSize((await new Response(body).arrayBuffer()).byteLength);
    } catch (error) {
      if (error instanceof Error && error.message === "standard_service_broker_request_too_large") throw error;
      throw new Error("standard_service_broker_request_body_invalid");
    }
    return;
  }
  if (body instanceof ReadableStream) {
    throw new Error("standard_service_broker_request_stream_unmeasurable");
  }
  throw new Error("standard_service_broker_request_body_invalid");
}

async function measureRequestInputBody(request: Request): Promise<void> {
  if (!request.body) return;
  let reader: ReadableStreamDefaultReader<Uint8Array>;
  try {
    reader = request.clone().body!.getReader();
  } catch {
    throw new Error("standard_service_broker_request_body_invalid");
  }
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) return;
      if (!(value instanceof Uint8Array)) {
        throw new Error("standard_service_broker_request_body_invalid");
      }
      total += value.byteLength;
      requireRequestSize(total);
    }
  } catch (error) {
    void reader.cancel().catch(() => {});
    if (error instanceof Error && (
      error.message === "standard_service_broker_request_too_large"
      || error.message === "standard_service_broker_request_body_invalid"
    )) throw error;
    throw new Error("standard_service_broker_request_body_invalid");
  } finally {
    reader.releaseLock();
  }
}

async function enforceBrokerRequestBodyLimit(
  input: string | URL | Request,
  init?: RequestInit,
): Promise<void> {
  if (init?.body !== undefined && init.body !== null) {
    await measureKnownBody(init.body);
    return;
  }
  if (input instanceof Request) await measureRequestInputBody(input);
}

async function boundedBrokerResponse(response: Response): Promise<Response> {
  const declared = response.headers.get("content-length");
  if (declared !== null) {
    const bytes = Number(declared);
    if (!Number.isSafeInteger(bytes) || bytes < 0 || bytes > TENANT_BROKER_MAX_RESPONSE_BYTES) {
      await response.body?.cancel().catch(() => {});
      throw new Error("standard_service_broker_response_too_large");
    }
  }
  if (!response.body) return response;
  let received = 0;
  const bounded = response.body.pipeThrough(new TransformStream<Uint8Array, Uint8Array>({
    transform(chunk, controller) {
      if (!(chunk instanceof Uint8Array)) {
        controller.error(new Error("standard_service_broker_response_invalid"));
        return;
      }
      received += chunk.byteLength;
      if (received > TENANT_BROKER_MAX_RESPONSE_BYTES) {
        controller.error(new Error("standard_service_broker_response_too_large"));
        return;
      }
      controller.enqueue(chunk);
    },
  }));
  return new Response(bounded, {
    status: response.status,
    statusText: response.statusText,
    headers: response.headers,
  });
}

/**
 * Keep the broker attachment credential on the one configured HTTPS endpoint.
 * Every request rechecks DNS before dispatch and refuses redirects. The MCP
 * transport cannot widen the endpoint through a response or session value.
 */
export function guardedTenantBrokerFetch(
  endpointInput: string | URL,
  resolver?: McpHostResolver,
): typeof fetch {
  let endpoint: URL;
  try {
    endpoint = new URL(endpointInput);
  } catch {
    throw new Error("standard_service_broker_endpoint_insecure");
  }
  const guarded = guardedFetch(endpoint.hostname, undefined, undefined, resolver);
  return (async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
    const destination = requestUrl(input);
    if (
      destination.protocol !== "https:"
      || destination.username
      || destination.password
      || destination.origin !== endpoint.origin
      || destination.pathname !== endpoint.pathname
      || destination.search !== endpoint.search
    ) {
      throw new Error("standard_service_broker_endpoint_changed");
    }
    // Body size is settled before the DNS gate. No oversized or streaming
    // request can trigger a lookup or carry credentials onto the network.
    await enforceBrokerRequestBodyLimit(input, init);
    return boundedBrokerResponse(await guarded(input, init));
  }) as typeof fetch;
}

function sameIdentity(a: StandardServiceIdentity, b: StandardServiceIdentity): boolean {
  return a.tenant_id === b.tenant_id
    && a.subject_id === b.subject_id
    && a.session_id === b.session_id
    && a.authority_turn_id === b.authority_turn_id
    && a.subscription_mount_id === b.subscription_mount_id
    && a.runner_profile_id === b.runner_profile_id;
}

function brokerClientError(code: "connect" | "call"): Error {
  const message = `standard_service_broker_${code}_failed`;
  const error = new Error(message);
  error.stack = `Error: ${message}`;
  return error;
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
  private readonly approvalStore: TenantBrokerApprovalStore;
  private readonly now: () => number;
  private lastCatalog?: StandardServiceCatalog;
  private readonly visualArtifacts = new Map<string, {
    identity: StandardServiceIdentity;
    reference: StandardServiceArtifactReference;
  }>();

  constructor(options: TenantBrokerStandardServiceProviderOptions) {
    let endpoint: URL;
    try {
      endpoint = new URL(options.endpoint);
    } catch {
      throw new Error("standard_service_broker_endpoint_insecure");
    }
    if (
      endpoint.protocol !== "https:"
      || endpoint.username
      || endpoint.password
      || endpoint.hash
      || !isAllowedMcpHost(endpoint.hostname)
    ) {
      throw new Error("standard_service_broker_endpoint_insecure");
    }
    this.endpoint = endpoint.toString();
    this.authorization = options.authorization;
    if (
      !options.approvalStore
      || typeof options.approvalStore.create !== "function"
      || typeof options.approvalStore.consume !== "function"
    ) {
      throw new Error("standard_service_broker_approval_store_required");
    }
    this.approvalStore = options.approvalStore;
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
    const boundIdentity = structuredClone(identity);
    requireTurnIdentity(boundIdentity);
    const frozenCall = structuredClone(call);
    const expectedDigest = tenantBrokerApprovalDigest(boundIdentity, frozenCall);
    const value = parseToolResult(await this.withClient(boundIdentity, (client) =>
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
    ) {
      throw new Error("standard_service_broker_approval_invalid");
    }
    const created = await this.approvalStore.create(structuredClone({
      approval_id: value.approval_id,
      identity: boundIdentity,
      call: frozenCall,
      argument_digest: expectedDigest,
      expires_at: new Date(value.expires_at * 1_000).toISOString(),
    }));
    if (!created) throw new Error("standard_service_broker_approval_invalid");
    return {
      approval_id: value.approval_id,
      argument_digest: expectedDigest,
      expires_at: new Date(value.expires_at * 1_000).toISOString(),
      summary: `${call.service_id}/${call.tool_id} requires human approval`,
    };
  }

  async confirm(identity: StandardServiceIdentity, approvalId: string): Promise<StandardServiceResult> {
    const boundIdentity = structuredClone(identity);
    requireTurnIdentity(boundIdentity);
    if (!APPROVAL_ID.test(approvalId)) {
      throw new Error("standard_service_broker_approval_binding_invalid");
    }
    const consumed = await this.approvalStore.consume({
      approval_id: approvalId,
      identity: structuredClone(boundIdentity),
      now_ms: this.now(),
    });
    const pending = consumed === null ? null : structuredClone(consumed);
    const expiresAtMs = Date.parse(pending?.expires_at ?? "");
    if (
      !pending
      || pending.approval_id !== approvalId
      || !sameIdentity(boundIdentity, pending.identity)
      || !ARGUMENT_DIGEST.test(pending.argument_digest)
      || !Number.isFinite(expiresAtMs)
      || expiresAtMs <= this.now()
      || tenantBrokerApprovalDigest(pending.identity, pending.call) !== pending.argument_digest
    ) {
      throw new Error("standard_service_broker_approval_binding_invalid");
    }
    // The store consumed before the remote call. A lost response must never
    // make an exact one-use effect look safe to retry.
    const value = requireCompleted(parseToolResult(await this.withClient(boundIdentity, (client) =>
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
    } catch {
      throw brokerClientError("call");
    } finally {
      await client.close().catch(() => {});
    }
  }

  private async openClient(identity: StandardServiceIdentity): Promise<TenantBrokerClient> {
    requireTurnIdentity(identity);
    const endpoint = new URL(this.endpoint);
    if (!this.clientFactory && !(await resolveAllowedMcpHost(endpoint.hostname))) {
      throw new Error("standard_service_broker_endpoint_unsafe");
    }
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
      let client: TenantBrokerClient | undefined;
      try {
        client = await this.clientFactory({
          identity: structuredClone(identity),
          bearerToken: authorization.bearer_token,
          channelSecret: authorization.channel_secret,
        });
        await client.connect();
        return client;
      } catch {
        await client?.close().catch(() => {});
        throw brokerClientError("connect");
      }
    }
    const transport = new StreamableHTTPClientTransport(endpoint, {
      fetch: guardedTenantBrokerFetch(endpoint),
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
    } catch {
      await adapter.close().catch(() => {});
      throw brokerClientError("connect");
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
