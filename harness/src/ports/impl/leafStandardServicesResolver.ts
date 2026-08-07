import { AsyncLocalStorage } from "node:async_hooks";

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
} from "../../vendor/mushy-author/ports/impl/standardServices.js";
import { TenantBrokerStandardServiceProvider } from "../../vendor/mushy-author/ports/impl/tenantBrokerStandardServiceProvider.js";
import type {
  StandardServicesResolver,
  StandardServicesSessionAttachment,
  TrustedStandardServicesContext,
} from "../../vendor/mushy-author/ports/impl/standardServicesRuntime.js";

const MAX_ATTACHMENT_BYTES = 64 * 1024;
const DEFAULT_TIMEOUT_MS = 5_000;
const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;
const TOKEN = /^[A-Za-z0-9._~-]{16,8192}$/;

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

function parseEnvironment(value: string | undefined): "local" | "staging" | "production" {
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
  if (!url.hostname || url.username || url.password || url.search || url.hash) {
    throw new Error("LEAF_TENANT_MCP_BROKER_URL must not contain credentials, query, or fragment");
  }
  const loopback = ["127.0.0.1", "localhost", "::1"].includes(url.hostname.toLowerCase());
  if (url.protocol !== "https:" && !(environment === "local" && url.protocol === "http:" && loopback)) {
    throw new Error("LEAF_TENANT_MCP_BROKER_URL must use HTTPS outside local loopback");
  }
  return url;
}

async function boundedJson(response: Response): Promise<unknown> {
  const length = response.headers.get("content-length");
  if (length && Number(length) > MAX_ATTACHMENT_BYTES) {
    throw new Error("standard_services_exchange_response_too_large");
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
  return new LeafStandardServicesResolver({
    appOrigin,
    brokerEndpoint,
    dispatchSecret,
    environment: parseEnvironment(env.LEAF_RUNTIME_ENV),
    ...(fetchImpl ? { fetchImpl } : {}),
  });
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
