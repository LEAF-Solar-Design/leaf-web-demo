/**
 * Child-process env hygiene (sol-critic F3 + re-review R3, 2026-07-22).
 *
 * Every subprocess this harness spawns — the LLM-facing Agent SDK child, the
 * boot-time git worker — gets a SCRUBBED copy of the environment, never the raw
 * one. Two layers:
 *
 *   1. Known keys: ambient Anthropic identities (would bridge someone else's
 *      credit into a tenant session) and internal toggles that do not match the
 *      pattern below.
 *   2. Pattern sweep: every key whose NAME looks credential-bearing is removed
 *      wholesale. A fixed denylist loses to the next secret someone adds
 *      (LEAF_OPS_SECRET, LEAF_GUEST_SECRET, PLATFORM_ADMIN_TOKEN, ... were all
 *      reachable under the old list); the sweep fails safe instead. Callers that
 *      need a credential in the child inject exactly ONE variable AFTER
 *      scrubbing (buildScrubbedEnv).
 *
 * Key NAMES are matched; values are never inspected or logged here.
 */

/** Ambient Anthropic identity/config keys (not all match the pattern sweep). */
export const AMBIENT_CRED_KEYS = [
  "ANTHROPIC_API_KEY",
  "ANTHROPIC_AUTH_TOKEN",
  "CLAUDE_CODE_OAUTH_TOKEN",
  "CLAUDE_CODE_USE_BEDROCK",
  "CLAUDE_CODE_USE_VERTEX",
  "CLAUDE_CODE_USE_FOUNDRY",
];

/** Internal non-secret toggles that still have no business in a child. */
const INTERNAL_FLAG_KEYS = ["LEAF_HARNESS_AUTH"];

/** Key-NAME pattern for the wholesale sweep. Deliberately broad: over-stripping
 *  env from a child is safe by default; under-stripping is the bug. AUTH also
 *  matches e.g. LEAF_AUTHOR_HARNESS_URL — intentionally accepted: no child this
 *  harness spawns has any use for those either (round-3 additions: AUTH, JWT,
 *  PASSWD — DOCKER_AUTH_CONFIG / NPM_CONFIG__AUTH / *_JWT survived the first
 *  pattern). */
const SECRETLIKE_KEY_RE = /(SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|APIKEY|API_KEY|_KEY$|AUTH|JWT)/i;

/** Scrubbed copy of `base`: known keys + every secret-like key removed. */
export function scrubSecrets(base: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ...base };
  for (const k of AMBIENT_CRED_KEYS) delete env[k];
  for (const k of INTERNAL_FLAG_KEYS) delete env[k];
  for (const k of Object.keys(env)) {
    if (SECRETLIKE_KEY_RE.test(k)) delete env[k];
  }
  return env;
}
