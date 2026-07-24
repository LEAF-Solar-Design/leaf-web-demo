/**
 * Wire <-> internal mapping for a per-session bring-your-own Agent SDK credential
 * ("mount your LLM", Concern 2). The FROZEN turn wire (converse.ts) carries a
 * snake_case `credential_grant`; the internal runner boundary speaks the
 * camelCase `AgentGrant` (ports/index.ts). This module is the ONE place the two
 * shapes meet.
 *
 * Secret discipline: these helpers only VALIDATE the shape and rename keys. The
 * token value is never logged here (nothing in this module prints). Downstream,
 * the mapped AgentGrant is injected into a scrubbed runner child env
 * (buildScrubbedEnv) and never persisted — see spineTurnAdapter.ts.
 */

import type { AgentGrant } from "./index.js";
import type { WireAgentGrant } from "./converse.js";

/**
 * Validate an untrusted `credential_grant` wire value. Returns the typed wire
 * grant when it is EXACTLY one of the two accepted shapes with a non-empty token
 * string, else null (the caller renders a 400). Extra keys are ignored; a missing
 * or empty token, or an unknown kind, fails.
 */
export function parseWireGrant(raw: unknown): WireAgentGrant | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const g = raw as Record<string, unknown>;
  if (g.kind === "api_key" && typeof g.api_key === "string" && g.api_key.trim()) {
    return { kind: "api_key", api_key: g.api_key };
  }
  if (g.kind === "oauth" && typeof g.oauth_token === "string" && g.oauth_token.trim()) {
    return { kind: "oauth", oauth_token: g.oauth_token };
  }
  return null;
}

/** Map a validated wire grant to the internal AgentGrant the runner env consumes. */
export function wireGrantToAgentGrant(g: WireAgentGrant): AgentGrant {
  return g.kind === "api_key"
    ? { kind: "api_key", apiKey: g.api_key }
    : { kind: "oauth", oauthToken: g.oauth_token };
}
