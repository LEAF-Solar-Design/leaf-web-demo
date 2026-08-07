import { AsyncLocalStorage } from "node:async_hooks";
import { createHash } from "node:crypto";

import type {
  AgentRunInput,
  AgentRunResult,
  AgentRunner,
  GrantLease,
  GrantSettlement,
  OAuthGrantProvider,
} from "../index.js";
import type {
  RunnerCapabilityProfileId,
  StandardServiceIdentity,
  StandardServicesResolver,
  StandardServicesSessionAttachment,
  TenantBrokerApprovalStore,
  TenantBrokerAuthorization,
  TenantBrokerClient,
  TrustedStandardServicesContext,
} from "../../vendor/mushy-author/index.js";
import { TenantBrokerStandardServiceProvider } from "../../vendor/mushy-author/index.js";
import type { LeafTenantBrokerApprovalStore } from "./tenantBrokerApprovalStore.js";

const MAX_ATTACHMENT_BYTES = 64 * 1024;
const DEFAULT_TIMEOUT_MS = 5_000;
const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;
const TOKEN = /^[A-Za-z0-9._~-]{16,8192}$/;
const CANONICAL_BROKER_ORIGIN = {
  staging: "https://staging-api.leafdesign.ai",
  production: "https://api.leafdesign.ai",
} as const;
const BROKER_MCP_PATH = "/mcp";

type FetchLike = typeof fetch;

interface AttachmentResponse {
  bearer_token: string;
  channel_secret: string;
  expires_at: string;
  identity: StandardServiceIdentity;
}

export interface AuthorAuthority {
  tenant_id: string;
  session_id: string;
  authority_session_id: string;
  authority_turn_id: string;
  subscription_mount_id?: string;
}

const authorAuthority = new AsyncLocalStorage<AuthorAuthority>();

function requiredId(name: string, value: unknown): string {
  if (typeof value !== "string" || !ID.test(value)) {
    throw new Error(`standard_services_exchange_invalid:${name}`);
  }
  return value;
}

function runnerProfile(value: unknown): RunnerCapabilityProfileId {
  const profile = requiredId("runner_profile_id", value);
  if (profile !== "author" && profile !== "spine") {
    throw new Error("standard_services_exchange_invalid:runner_profile_id");
  }
  return profile;
}

export function parseStandardServicesEnvironment(value: string | undefined): "local" | "staging" | "production" {
  const normalized = (value ?? "").trim().toLowerCase();
  if (normalized === "local" || normalized === "development" || normalized === "test") return "local";
  if (normalized === "staging" || normalized === "production") return normalized;
  throw new Error("LEAF_RUNTIME_ENV must be local, staging, or production for standard services");
}

function parseAppOrigin(value: string, environment: "local" | "staging" | "production"): URL {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error("LEAF_APP_URL must be an absolute origin");
  }
  if (
    !["http:", "https:"].includes(url.protocol)
    || !url.hostname
    || url.username
    || url.password
    || url.search
    || url.hash
    || !["", "/"].includes(url.pathname)
  ) {
    throw new Error("LEAF_APP_URL must be an HTTP(S) origin without credentials or a path");
  }
  const loopback = ["127.0.0.1", "localhost", "::1"].includes(url.hostname.toLowerCase());
  if (url.protocol !== "https:" && !(environment === "local" && url.protocol === "http:" && loopback)) {
    throw new Error("LEAF_APP_URL must use HTTPS outside local loopback");
  }
  return new URL(url.origin);
}

function parseBrokerEndpoint(value: string, environment: "local" | "staging" | "production"): URL {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error("LEAF_TENANT_MCP_BROKER_URL must be an absolute URL");
  }
  if (
    !url.hostname
    || url.username
    || url.password
    || url.search
    || url.hash
    || !["", "/"].includes(url.pathname)
  ) {
    throw new Error("LEAF_TENANT_MCP_BROKER_URL must not contain credentials, query, or fragment");
  }
  const loopback = ["127.0.0.1", "localhost", "::1"].includes(url.hostname.toLowerCase());
  if (environment === "local") {
    if (!loopback || !["http:", "https:"].includes(url.protocol)) {
      throw new Error("LEAF_TENANT_MCP_BROKER_URL must use an explicit local loopback origin in local mode");
    }
    return new URL(BROKER_MCP_PATH, url.origin);
  }
  const canonicalOrigin = CANONICAL_BROKER_ORIGIN[environment];
  if (url.protocol !== "https:" || url.origin !== canonicalOrigin) {
    throw new Error("LEAF_TENANT_MCP_BROKER_URL must match the canonical tenant broker origin");
  }
  return new URL(BROKER_MCP_PATH, canonicalOrigin);
}

async function boundedJson(response: Response): Promise<unknown> {
  const length = response.headers.get("content-length");
  if (length) {
    const declared = Number(length);
    if (!Number.isSafeInteger(declared) || declared < 0) {
      throw new Error("standard_services_exchange_response_invalid");
    }
    if (declared > MAX_ATTACHMENT_BYTES) {
      throw new Error("standard_services_exchange_response_too_large");
    }
  }
  if (!response.body) throw new Error("standard_services_exchange_response_missing");
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_ATTACHMENT_BYTES) {
      await reader.cancel();
      throw new Error("standard_services_exchange_response_too_large");
    }
    chunks.push(value);
  }
  const body = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return JSON.parse(new TextDecoder().decode(body));
  } catch {
    throw new Error("standard_services_exchange_response_invalid");
  }
}

function parseAttachment(value: unknown): AttachmentResponse {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("standard_services_exchange_response_invalid");
  }
  const body = value as Record<string, unknown>;
  const identityValue = body.identity;
  if (!identityValue || typeof identityValue !== "object" || Array.isArray(identityValue)) {
    throw new Error("standard_services_exchange_identity_invalid");
  }
  const identityRaw = identityValue as Record<string, unknown>;
  const identity: StandardServiceIdentity = {
    tenant_id: requiredId("tenant_id", identityRaw.tenant_id),
    subject_id: requiredId("subject_id", identityRaw.subject_id),
    session_id: requiredId("session_id", identityRaw.session_id),
    authority_turn_id: requiredId("authority_turn_id", identityRaw.authority_turn_id),
    subscription_mount_id: requiredId("subscription_mount_id", identityRaw.subscription_mount_id),
    runner_profile_id: runnerProfile(identityRaw.runner_profile_id),
  };
  if (typeof body.bearer_token !== "string" || !TOKEN.test(body.bearer_token)) {
    throw new Error("standard_services_exchange_bearer_invalid");
  }
  if (typeof body.channel_secret !== "string" || !TOKEN.test(body.channel_secret)) {
    throw new Error("standard_services_exchange_channel_invalid");
  }
  if (typeof body.expires_at !== "string" || !Number.isFinite(Date.parse(body.expires_at))) {
    throw new Error("standard_services_exchange_expiry_invalid");
  }
  return {
    bearer_token: body.bearer_token,
    channel_secret: body.channel_secret,
    expires_at: body.expires_at,
    identity,
  };
}

export interface LeafStandardServicesResolverOptions {
  appOrigin: string;
  brokerEndpoint: string;
  dispatchSecret: string;
  environment: "local" | "staging" | "production";
  approvalStore: TenantBrokerApprovalStore;
  fetchImpl?: FetchLike;
  timeoutMs?: number;
  now?: () => number;
}

/** Leaf consumer adapter. It exchanges only app-owned turn authority for a broker facade. */
export class LeafStandardServicesResolver implements StandardServicesResolver {
  private readonly appOrigin: URL;
  private readonly brokerEndpoint: URL;
  private readonly dispatchSecret: string;
  private readonly fetchImpl: FetchLike;
  private readonly timeoutMs: number;
  private readonly now: () => number;

  constructor(private readonly options: LeafStandardServicesResolverOptions) {
    this.appOrigin = parseAppOrigin(options.appOrigin, options.environment);
    this.brokerEndpoint = parseBrokerEndpoint(options.brokerEndpoint, options.environment);
    this.dispatchSecret = options.dispatchSecret.trim();
    if (this.dispatchSecret.length < 16) throw new Error("LEAF_APP_DISPATCH_SECRET is invalid");
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.now = options.now ?? Date.now;
  }

  async resolve(
    context: TrustedStandardServicesContext,
    runnerProfileId: RunnerCapabilityProfileId,
  ): Promise<StandardServicesSessionAttachment> {
    if (runnerProfileId !== "author" && runnerProfileId !== "spine") {
      throw new Error("standard_services_runner_profile_denied");
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    let response: Response;
    try {
      response = await this.fetchImpl(new URL("/internal/mcp/gateway/attachment", this.appOrigin), {
        method: "POST",
        redirect: "error",
        signal: controller.signal,
        headers: {
          "content-type": "application/json",
          "x-dispatch-secret": this.dispatchSecret,
          "x-tenant-id": context.tenant_id,
        },
        body: JSON.stringify({
          session_id: context.session_id,
          authority_session_id: context.authority_session_id,
          authority_turn_id: context.authority_turn_id,
          subscription_mount_id: context.subscription_mount_id,
          runner_profile_id: runnerProfileId,
        }),
      });
    } catch (error) {
      throw new Error(`standard_services_exchange_unavailable:${error instanceof Error ? error.name : "error"}`);
    } finally {
      clearTimeout(timer);
    }
    if (response.status !== 200) throw new Error(`standard_services_exchange_rejected:${response.status}`);
    const attachment = parseAttachment(await boundedJson(response));
    const expiresAt = Date.parse(attachment.expires_at);
    if (expiresAt <= this.now()) throw new Error("standard_services_attachment_expired");
    const expected: Omit<StandardServiceIdentity, "subject_id"> = {
      tenant_id: context.tenant_id,
      session_id: context.session_id,
      authority_turn_id: context.authority_turn_id ?? "",
      subscription_mount_id: context.subscription_mount_id,
      runner_profile_id: runnerProfileId,
    };
    for (const [key, expectedValue] of Object.entries(expected)) {
      if (attachment.identity[key as keyof StandardServiceIdentity] !== expectedValue) {
        throw new Error(`standard_services_attachment_identity_mismatch:${key}`);
      }
    }
    const provider = new TenantBrokerStandardServiceProvider({
      endpoint: this.brokerEndpoint.toString(),
      approvalStore: this.options.approvalStore,
      authorization: async (identity) => {
        for (const key of [
          "tenant_id",
          "subject_id",
          "session_id",
          "authority_turn_id",
          "subscription_mount_id",
          "runner_profile_id",
        ] as const) {
          if (identity[key] !== attachment.identity[key]) {
            throw new Error("standard_services_authorization_identity_mismatch");
          }
        }
        if (Date.parse(attachment.expires_at) <= this.now()) {
          throw new Error("standard_services_attachment_expired");
        }
        return {
          bearer_token: attachment.bearer_token,
          channel_secret: attachment.channel_secret,
          expires_at: attachment.expires_at,
        };
      },
      now: this.now,
    });
    return {
      identity: attachment.identity,
      provider,
      environment: this.options.environment,
      credential_expires_at: attachment.expires_at,
    };
  }
}

export function standardServicesResolverFromEnv(
  env: NodeJS.ProcessEnv = process.env,
  fetchImpl?: FetchLike,
  approvalStore?: TenantBrokerApprovalStore,
): LeafStandardServicesResolver | undefined {
  const appOrigin = (env.LEAF_APP_URL ?? "").trim();
  const brokerEndpoint = (env.LEAF_TENANT_MCP_BROKER_URL ?? "").trim();
  const dispatchSecret = (env.LEAF_APP_DISPATCH_SECRET ?? "").trim();
  const configured = [appOrigin, brokerEndpoint, dispatchSecret].filter(Boolean).length;
  const runtime = (env.LEAF_RUNTIME_ENV ?? "").trim().toLowerCase();
  if (configured === 0) {
    if (runtime === "staging" || runtime === "production") {
      throw new Error("standard services are required in staging and production");
    }
    return undefined;
  }
  if (configured !== 3) throw new Error("standard services require LEAF_APP_URL, LEAF_TENANT_MCP_BROKER_URL, and LEAF_APP_DISPATCH_SECRET");
  if (!approvalStore) throw new Error("standard services require a durable tenant broker approval store");
  return new LeafStandardServicesResolver({
    appOrigin,
    brokerEndpoint,
    dispatchSecret,
    environment: parseStandardServicesEnvironment(env.LEAF_RUNTIME_ENV),
    approvalStore,
    ...(fetchImpl ? { fetchImpl } : {}),
  });
}

export interface HumanApprovalHostInput {
  approval_id: string;
  argument_digest: string;
  identity: StandardServiceIdentity;
  human_bearer: string;
  attachment: TenantBrokerAuthorization;
}

export interface HumanApprovalReceipt {
  status: "completed";
  receipt_id: string;
  artifact_ids?: string[];
}

/** Human-only host adapter. It is never attached to an SDK MCP server. */
export class LeafStandardServicesHumanApprovalHost {
  private readonly endpoint: URL;
  private readonly fetchImpl: FetchLike;
  private readonly now: () => number;

  constructor(private readonly options: {
    brokerEndpoint: string;
    environment: "local" | "staging" | "production";
    approvalStore: LeafTenantBrokerApprovalStore;
    fetchImpl?: FetchLike;
    now?: () => number;
    providerFactory?: typeof TenantBrokerStandardServiceProvider;
    clientFactory?: (context: {
      identity: StandardServiceIdentity;
      bearerToken: string;
      channelSecret: string;
    }) => Promise<TenantBrokerClient>;
  }) {
    this.endpoint = parseBrokerEndpoint(options.brokerEndpoint, options.environment);
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.now = options.now ?? Date.now;
  }

  async review(input: {
    approval_id: string;
    argument_digest: string;
    tenant_id: string;
    subject_id: string;
  }): Promise<{ status: "pending"; identity: StandardServiceIdentity } | null> {
    const binding = await this.options.approvalStore.review({
      approval_id: input.approval_id,
      argument_digest: input.argument_digest,
      tenant_id: input.tenant_id,
      subject_id: input.subject_id,
      now_ms: this.now(),
    });
    return binding ? { status: "pending", identity: structuredClone(binding.identity) } : null;
  }

  async execute(input: HumanApprovalHostInput): Promise<HumanApprovalReceipt> {
    const approvalId = requiredId("approval_id", input.approval_id);
    if (!/^[A-Za-z0-9_-]{8,256}$/.test(approvalId) || !/^[a-f0-9]{64}$/.test(input.argument_digest)) {
      throw new Error("standard_services_human_approval_invalid");
    }
    const identity: StandardServiceIdentity = {
      tenant_id: requiredId("tenant_id", input.identity.tenant_id),
      subject_id: requiredId("subject_id", input.identity.subject_id),
      session_id: requiredId("session_id", input.identity.session_id),
      authority_turn_id: requiredId("authority_turn_id", input.identity.authority_turn_id),
      subscription_mount_id: requiredId("subscription_mount_id", input.identity.subscription_mount_id),
      runner_profile_id: runnerProfile(input.identity.runner_profile_id),
    };
    if (!TOKEN.test(input.human_bearer) || !TOKEN.test(input.attachment.bearer_token)
      || !TOKEN.test(input.attachment.channel_secret)
      || Date.parse(input.attachment.expires_at) <= this.now()) {
      throw new Error("standard_services_human_approval_credential_invalid");
    }
    const reviewed = await this.options.approvalStore.review({
      approval_id: approvalId,
      argument_digest: input.argument_digest,
      tenant_id: identity.tenant_id,
      subject_id: identity.subject_id,
      now_ms: this.now(),
    });
    if (!reviewed) throw new Error("standard_services_human_approval_binding_invalid");
    for (const key of [
      "tenant_id", "subject_id", "session_id", "authority_turn_id",
      "subscription_mount_id", "runner_profile_id",
    ] as const) {
      if (reviewed.identity[key] !== identity[key]) {
        throw new Error("standard_services_human_approval_binding_invalid");
      }
    }
    const approvalUrl = new URL(
      `/mcp/approvals/${encodeURIComponent(approvalId)}`,
      this.endpoint.origin,
    );
    const response = await this.fetchImpl(approvalUrl, {
      method: "POST",
      redirect: "error",
      headers: {
        authorization: `Bearer ${input.human_bearer}`,
        "content-type": "application/json",
      },
      body: JSON.stringify({ argument_digest: input.argument_digest }),
    });
    if (response.status !== 200) throw new Error("standard_services_human_approval_rejected");
    const approved = await boundedJson(response);
    if (
      !approved
      || typeof approved !== "object"
      || Array.isArray(approved)
      || Object.keys(approved).length !== 1
      || (approved as Record<string, unknown>).status !== "approved"
    ) {
      throw new Error("standard_services_human_approval_response_invalid");
    }
    const Provider = this.options.providerFactory ?? TenantBrokerStandardServiceProvider;
    const provider = new Provider({
      endpoint: this.endpoint.toString(),
      approvalStore: this.options.approvalStore,
      authorization: async (requested) => {
        for (const key of [
          "tenant_id", "subject_id", "session_id", "authority_turn_id",
          "subscription_mount_id", "runner_profile_id",
        ] as const) {
          if (requested[key] !== identity[key]) {
            throw new Error("standard_services_human_approval_identity_mismatch");
          }
        }
        return structuredClone(input.attachment);
      },
      ...(this.options.clientFactory ? { clientFactory: this.options.clientFactory } : {}),
      now: this.now,
    });
    const result = await provider.confirm(identity, approvalId);
    const artifactIds = result.artifact_ids?.map((artifactId) => requiredId("artifact_id", artifactId));
    if (artifactIds && artifactIds.length > 32) {
      throw new Error("standard_services_human_approval_artifacts_invalid");
    }
    const receiptId = createHash("sha256")
      .update(`${approvalId}\0${input.argument_digest}\0${result.content}`)
      .digest("hex");
    return {
      status: "completed",
      receipt_id: receiptId,
      ...(artifactIds?.length ? { artifact_ids: artifactIds } : {}),
    };
  }
}

export function withAuthorStandardServicesAuthority<T>(
  authority: Omit<AuthorAuthority, "subscription_mount_id">,
  action: () => Promise<T>,
): Promise<T> {
  for (const [name, value] of Object.entries(authority)) requiredId(name, value);
  return authorAuthority.run({ ...authority }, action);
}

export class StandardServicesOAuthGrantProvider implements OAuthGrantProvider {
  constructor(private readonly inner: OAuthGrantProvider) {}

  getGrant(tenantId: string) {
    return this.inner.getGrant(tenantId);
  }

  async acquireGrant(tenantId: string): Promise<GrantLease> {
    if (!this.inner.acquireGrant) throw new Error("grant routing is not configured");
    const lease = await this.inner.acquireGrant(tenantId);
    const current = authorAuthority.getStore();
    if (current) {
      if (current.tenant_id !== tenantId) throw new Error("standard_services_authority_tenant_mismatch");
      current.subscription_mount_id = requiredId("subscription_mount_id", lease.account_id);
    }
    return lease;
  }

  settleGrant(tenantId: string, leaseId: string, outcome: GrantSettlement): Promise<void> {
    return this.inner.settleGrant?.(tenantId, leaseId, outcome) ?? Promise.resolve();
  }
}

export class AuthorStandardServicesRunner implements AgentRunner {
  constructor(
    private readonly inner: AgentRunner,
    private readonly enabled: boolean,
  ) {}

  run(input: AgentRunInput): Promise<AgentRunResult> {
    if (!this.enabled) return this.inner.run(input);
    const current = authorAuthority.getStore();
    if (!current?.subscription_mount_id) {
      throw new Error("standard_services_author_authority_missing");
    }
    return this.inner.run({
      ...input,
      standardServicesContext: {
        tenant_id: current.tenant_id,
        session_id: current.session_id,
        subscription_mount_id: current.subscription_mount_id,
        authority_session_id: current.authority_session_id,
        authority_turn_id: current.authority_turn_id,
      },
    });
  }
}
