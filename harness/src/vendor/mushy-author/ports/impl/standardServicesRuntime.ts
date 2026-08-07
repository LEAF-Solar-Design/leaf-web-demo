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

function requireId(name: string, value: string): void {
  if (!REQUIRED_ID.test(value)) throw new Error(`standard_services_context_invalid:${name}`);
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
  requireId("tenant_id", context.tenant_id);
  requireId("session_id", context.session_id);
  requireId("subscription_mount_id", context.subscription_mount_id);
  requireId("authority_session_id", context.authority_session_id);
  if (context.authority_turn_id !== undefined) requireId("authority_turn_id", context.authority_turn_id);

  const attachment = await resolver.resolve(structuredClone(context), runnerProfileId);
  const identity = attachment?.identity;
  if (!identity || !attachment.provider || !["local", "staging", "production"].includes(attachment.environment)) {
    throw new Error("standard_services_attachment_incomplete");
  }
  requireId("subject_id", identity.subject_id);
  if (
    identity.tenant_id !== context.tenant_id
    || identity.session_id !== context.session_id
    || identity.authority_turn_id !== context.authority_turn_id
    || identity.subscription_mount_id !== context.subscription_mount_id
    || identity.runner_profile_id !== runnerProfileId
  ) {
    throw new Error("standard_services_attachment_identity_mismatch");
  }
  if (attachment.credential_expires_at !== undefined) {
    const expiresAt = Date.parse(attachment.credential_expires_at);
    if (!Number.isFinite(expiresAt) || expiresAt <= now()) {
      throw new Error("standard_services_attachment_expired");
    }
  }
  return attachment;
}
