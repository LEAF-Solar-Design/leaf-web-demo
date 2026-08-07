import { randomUUID } from "node:crypto";

import type {
  RunnerCapabilityProfileId,
  StandardServiceEnvironment,
  StandardServiceIdentity,
  StandardServiceProvider,
} from "./standardServices.js";

/**
 * Product-owned authority presented to a standard-services resolver.
 * The model never supplies this object. The subject is deliberately absent:
 * the product must re-resolve it from its authenticated session or turn.
 */
export interface TrustedStandardServicesContext {
  tenant_id: string;
  session_id: string;
  subscription_mount_id: string;
  authority_session_id: string;
  authority_turn_id?: string;
}

export interface StandardServicesSessionAttachment {
  identity: StandardServiceIdentity;
  provider: StandardServiceProvider;
  environment: StandardServiceEnvironment;
  credential_expires_at?: string;
}

/** Configured once by the product runtime, then resolved once per model run. */
export interface StandardServicesResolver {
  resolve(
    context: TrustedStandardServicesContext,
    runnerProfileId: RunnerCapabilityProfileId,
  ): Promise<StandardServicesSessionAttachment>;
}

export interface TurnBoundInspectionTargetProvider {
  issueInspectionTarget(
    identity: StandardServiceIdentity,
    url: string,
    ttlMs?: number,
  ): Promise<{ inspection_target_id: string; expires_at: string }>;
}

const REQUIRED_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;
const OPAQUE_ID = /^[A-Za-z0-9_-]{8,256}$/;

function requireId(name: string, value: unknown): asserts value is string {
  if (typeof value !== "string" || !REQUIRED_ID.test(value)) {
    throw new Error(`standard_services_context_invalid:${name}`);
  }
}

function deepFreeze<T>(value: T, seen: Set<unknown> = new Set()): T {
  if (!value || typeof value !== "object" || seen.has(value)) return value;
  seen.add(value);
  for (const nested of Object.values(value as Record<string, unknown>)) deepFreeze(nested, seen);
  return Object.freeze(value);
}

/** Validate and pin product authority before any asynchronous dependency runs. */
export function snapshotTrustedStandardServicesContext(
  context: TrustedStandardServicesContext,
): TrustedStandardServicesContext {
  let snapshot: unknown;
  try {
    snapshot = structuredClone(context);
  } catch {
    throw new Error("standard_services_context_invalid");
  }
  if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot)) {
    throw new Error("standard_services_context_invalid");
  }
  const record = snapshot as Record<string, unknown>;
  const allowed = new Set([
    "tenant_id",
    "session_id",
    "subscription_mount_id",
    "authority_session_id",
    "authority_turn_id",
  ]);
  if (Object.keys(record).some((key) => !allowed.has(key))) {
    throw new Error("standard_services_context_invalid");
  }
  requireId("tenant_id", record.tenant_id);
  requireId("session_id", record.session_id);
  requireId("subscription_mount_id", record.subscription_mount_id);
  requireId("authority_session_id", record.authority_session_id);
  if (record.authority_turn_id !== undefined) requireId("authority_turn_id", record.authority_turn_id);
  return deepFreeze({
    tenant_id: record.tenant_id,
    session_id: record.session_id,
    subscription_mount_id: record.subscription_mount_id,
    authority_session_id: record.authority_session_id,
    ...(record.authority_turn_id === undefined
      ? {}
      : { authority_turn_id: record.authority_turn_id }),
  });
}

function snapshotStandardServiceIdentity(value: unknown): StandardServiceIdentity {
  let snapshot: unknown;
  try {
    snapshot = structuredClone(value);
  } catch {
    throw new Error("standard_services_attachment_incomplete");
  }
  if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot)) {
    throw new Error("standard_services_attachment_incomplete");
  }
  const record = snapshot as Record<string, unknown>;
  const fields = [
    "tenant_id",
    "subject_id",
    "session_id",
    "authority_turn_id",
    "subscription_mount_id",
    "runner_profile_id",
  ] as const;
  if (Object.keys(record).some((key) => !fields.includes(key as typeof fields[number]))) {
    throw new Error("standard_services_attachment_incomplete");
  }
  try {
    for (const field of fields) requireId(field, record[field]);
  } catch {
    throw new Error("standard_services_attachment_incomplete");
  }
  if (!(["shell-editor", "author", "spine"] as const).includes(
    record.runner_profile_id as RunnerCapabilityProfileId,
  )) {
    throw new Error("standard_services_attachment_incomplete");
  }
  return deepFreeze({
    tenant_id: record.tenant_id as string,
    subject_id: record.subject_id as string,
    session_id: record.session_id as string,
    authority_turn_id: record.authority_turn_id as string,
    subscription_mount_id: record.subscription_mount_id as string,
    runner_profile_id: record.runner_profile_id as RunnerCapabilityProfileId,
  });
}

/** Create a fresh identity and opaque visual target for one model turn. */
export async function issueTurnBoundInspectionTarget(
  provider: TurnBoundInspectionTargetProvider,
  identityBase: Omit<StandardServiceIdentity, "authority_turn_id">,
  url: string,
  createTurnId: () => string = randomUUID,
): Promise<{
  identity: StandardServiceIdentity;
  inspection_target_id: string;
  expires_at: string;
}> {
  const identity = { ...structuredClone(identityBase), authority_turn_id: createTurnId() };
  requireId("authority_turn_id", identity.authority_turn_id);
  const target = await provider.issueInspectionTarget(identity, url);
  const expiresAt = Date.parse(target?.expires_at ?? "");
  if (!OPAQUE_ID.test(target?.inspection_target_id ?? "") || !Number.isFinite(expiresAt) || expiresAt <= Date.now()) {
    throw new Error("standard_services_visual_target_incomplete");
  }
  return { identity, ...target };
}

/**
 * Resolve an attachment and pin it to the trusted caller context. This blocks
 * a buggy or compromised credential exchange from swapping tenant, session,
 * mount, or runner profile after the product has authenticated the request.
 */
export async function resolveStandardServicesSession(
  resolver: StandardServicesResolver,
  context: TrustedStandardServicesContext,
  runnerProfileId: RunnerCapabilityProfileId,
  now: () => number = Date.now,
): Promise<StandardServicesSessionAttachment> {
  const trusted = snapshotTrustedStandardServicesContext(context);

  const attachment = await resolver.resolve(trusted, runnerProfileId);
  const provider = attachment?.provider;
  const environment = attachment?.environment;
  const credentialExpiresAt = attachment?.credential_expires_at;
  if (!provider || !["local", "staging", "production"].includes(environment)) {
    throw new Error("standard_services_attachment_incomplete");
  }
  const identity = snapshotStandardServiceIdentity(attachment.identity);
  if (
    identity.tenant_id !== trusted.tenant_id
    || identity.session_id !== trusted.session_id
    || identity.authority_turn_id !== trusted.authority_turn_id
    || identity.subscription_mount_id !== trusted.subscription_mount_id
    || identity.runner_profile_id !== runnerProfileId
  ) {
    throw new Error("standard_services_attachment_identity_mismatch");
  }
  if (credentialExpiresAt !== undefined) {
    if (typeof credentialExpiresAt !== "string") {
      throw new Error("standard_services_attachment_expired");
    }
    const expiresAt = Date.parse(credentialExpiresAt);
    if (!Number.isFinite(expiresAt) || expiresAt <= now()) {
      throw new Error("standard_services_attachment_expired");
    }
  }
  return Object.freeze({
    identity,
    provider,
    environment: environment as StandardServiceEnvironment,
    ...(credentialExpiresAt === undefined
      ? {}
      : { credential_expires_at: credentialExpiresAt }),
  });
}
