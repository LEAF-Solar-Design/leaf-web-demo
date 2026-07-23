/**
 * §17 — AgentSdkRunner env injection by grant kind (the ONLY Anthropic-egress seam).
 *
 * `buildScrubbedEnv` is the exact function the real runner calls to construct the
 * SCRUBBED child env it hands to the SDK's `env` option (agentSdkRunner.ts step 1).
 * Testing it directly exercises the api_key-vs-oauth injection + ambient-strip without
 * constructing the SDK or making a single real turn (HARD RULE: no real SDK).
 *
 * Contract B: an `api_key` grant injects ANTHROPIC_API_KEY (and strips
 * CLAUDE_CODE_OAUTH_TOKEN); an `oauth` grant injects CLAUDE_CODE_OAUTH_TOKEN (and strips
 * ANTHROPIC_API_KEY). All ambient Anthropic identities are scrubbed either way.
 */

import { describe, expect, it } from "vitest";

import { buildScrubbedEnv } from "../src/ports/impl/agentSdkRunner.js";
import type { AgentGrant } from "../src/ports/index.js";

const FAKE_API_KEY = "sk-ant-api03-FAKE-not-a-real-key-abc123";
const FAKE_OAUTH = "sk-ant-oat01-FAKE-not-a-real-token-def456";

/** A base env pre-polluted with EVERY ambient Anthropic credential, to prove they scrub. */
function pollutedBase(): NodeJS.ProcessEnv {
  return {
    PATH: "/usr/bin",
    ANTHROPIC_API_KEY: "AMBIENT-should-be-removed-or-overwritten",
    ANTHROPIC_AUTH_TOKEN: "AMBIENT-auth-should-be-removed",
    CLAUDE_CODE_OAUTH_TOKEN: "AMBIENT-oauth-should-be-removed-or-overwritten",
    CLAUDE_CODE_USE_BEDROCK: "1",
    CLAUDE_CODE_USE_VERTEX: "1",
    CLAUDE_CODE_USE_FOUNDRY: "1",
    // Platform-internal secrets (sol-critic F3): must never reach the SDK child.
    LEAF_HARNESS_SECRET: "INTERNAL-hop-secret-should-be-removed",
    LEAF_HARNESS_AUTH: "1",
    LEAF_APP_DISPATCH_SECRET: "INTERNAL-dispatch-secret-should-be-removed",
    LEAF_BROKER_SECRET: "INTERNAL-broker-secret-should-be-removed",
    E2B_API_KEY: "INTERNAL-e2b-key-should-be-removed",
    // R3: the sweep must be by key-name PATTERN, not a fixed list — these four
    // were reachable under the first fix's denylist.
    LEAF_OPS_SECRET: "INTERNAL-ops-secret-should-be-removed",
    LEAF_GUEST_SECRET: "INTERNAL-guest-secret-should-be-removed",
    LEAF_CALLBACK_SECRET: "INTERNAL-callback-secret-should-be-removed",
    PLATFORM_ADMIN_TOKEN: "INTERNAL-admin-token-should-be-removed",
  };
}

describe("buildScrubbedEnv — grant kind drives the injected credential var", () => {
  it("api_key grant -> ANTHROPIC_API_KEY set; CLAUDE_CODE_OAUTH_TOKEN absent", () => {
    const grant: AgentGrant = { kind: "api_key", apiKey: FAKE_API_KEY };
    const env = buildScrubbedEnv(grant, pollutedBase());
    expect(env.ANTHROPIC_API_KEY).toBe(FAKE_API_KEY);
    expect(env.CLAUDE_CODE_OAUTH_TOKEN).toBeUndefined();
    // ambient non-injected creds are stripped regardless
    expect(env.ANTHROPIC_AUTH_TOKEN).toBeUndefined();
    expect(env.CLAUDE_CODE_USE_BEDROCK).toBeUndefined();
    expect(env.CLAUDE_CODE_USE_VERTEX).toBeUndefined();
    expect(env.CLAUDE_CODE_USE_FOUNDRY).toBeUndefined();
    // platform-internal secrets are stripped too (F3)
    expect(env.LEAF_HARNESS_SECRET).toBeUndefined();
    expect(env.LEAF_HARNESS_AUTH).toBeUndefined();
    expect(env.LEAF_APP_DISPATCH_SECRET).toBeUndefined();
    expect(env.LEAF_BROKER_SECRET).toBeUndefined();
    expect(env.E2B_API_KEY).toBeUndefined();
    // R3: pattern sweep catches secret-like keys NOT on any fixed list
    expect(env.LEAF_OPS_SECRET).toBeUndefined();
    expect(env.LEAF_GUEST_SECRET).toBeUndefined();
    expect(env.LEAF_CALLBACK_SECRET).toBeUndefined();
    expect(env.PLATFORM_ADMIN_TOKEN).toBeUndefined();
    // untouched, non-credential env survives
    expect(env.PATH).toBe("/usr/bin");
  });

  it("oauth grant -> CLAUDE_CODE_OAUTH_TOKEN set; ANTHROPIC_API_KEY absent", () => {
    const grant: AgentGrant = { kind: "oauth", oauthToken: FAKE_OAUTH };
    const env = buildScrubbedEnv(grant, pollutedBase());
    expect(env.CLAUDE_CODE_OAUTH_TOKEN).toBe(FAKE_OAUTH);
    expect(env.ANTHROPIC_API_KEY).toBeUndefined();
    expect(env.ANTHROPIC_AUTH_TOKEN).toBeUndefined();
    expect(env.PATH).toBe("/usr/bin");
  });

  it("does not mutate the caller's base env (returns a fresh object)", () => {
    const base = pollutedBase();
    const before = { ...base };
    buildScrubbedEnv({ kind: "api_key", apiKey: FAKE_API_KEY }, base);
    expect(base).toEqual(before); // base untouched
  });
});
