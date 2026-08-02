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

import { buildScrubbedEnv, resolveAuthorTimeoutMs } from "../src/ports/impl/agentSdkRunner.js";
import type { AgentGrant } from "../src/ports/index.js";

const FAKE_API_KEY = "sk-ant-api03-FAKE-not-a-real-key-abc123";
const FAKE_OAUTH = "sk-ant-oat01-FAKE-not-a-real-token-def456";

describe("resolveAuthorTimeoutMs", () => {
  it("uses a five-minute default shared with the app wait budget", () => {
    expect(resolveAuthorTimeoutMs(undefined, {})).toBe(300_000);
  });

  it("accepts a bounded operator value and rejects unsafe values", () => {
    expect(resolveAuthorTimeoutMs(undefined, { LEAF_AUTHOR_TIMEOUT_S: "240" })).toBe(240_000);
    expect(resolveAuthorTimeoutMs(undefined, { LEAF_AUTHOR_TIMEOUT_S: "invalid" })).toBe(300_000);
    expect(resolveAuthorTimeoutMs(30_000, { LEAF_AUTHOR_TIMEOUT_S: "240" })).toBe(30_000);
    expect(resolveAuthorTimeoutMs(1_000, { LEAF_AUTHOR_TIMEOUT_S: "240" })).toBe(240_000);
  });
});

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
    // Round-3: AUTH/JWT-named credential carriers dodge a SECRET/TOKEN-only pattern.
    DOCKER_AUTH_CONFIG: "CRED-docker-auth-should-be-removed",
    NPM_CONFIG__AUTH: "CRED-npm-auth-should-be-removed",
    LEAF_ADMIN_JWT: "CRED-admin-jwt-should-be-removed",
    // Round-4: PAT (personal access token) as an underscore-delimited segment.
    GITHUB_PAT: "CRED-github-pat-should-be-removed",
    AZURE_DEVOPS_EXT_PAT: "CRED-azdo-pat-should-be-removed",
    // Round-5: password-provider hooks — a child honoring one can be steered
    // to a credential-emitting program.
    GIT_ASKPASS: "/usr/bin/evil-cred-provider",
    SSH_ASKPASS: "/usr/bin/evil-cred-provider",
    // PAT must NOT over-match PATH-like names (PATH itself is pinned below).
    LEAF_DATA_PATH: "/var/leaf/data",
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
    // Round-3: AUTH/JWT-named carriers are swept too
    expect(env.DOCKER_AUTH_CONFIG).toBeUndefined();
    expect(env.NPM_CONFIG__AUTH).toBeUndefined();
    expect(env.LEAF_ADMIN_JWT).toBeUndefined();
    // Round-4: PAT-named carriers are swept as whole segments
    expect(env.GITHUB_PAT).toBeUndefined();
    expect(env.AZURE_DEVOPS_EXT_PAT).toBeUndefined();
    // Round-5: password-provider hooks are swept
    expect(env.GIT_ASKPASS).toBeUndefined();
    expect(env.SSH_ASKPASS).toBeUndefined();
    // untouched, non-credential env survives — and PAT does not eat PATH-like keys
    expect(env.PATH).toBe("/usr/bin");
    expect(env.LEAF_DATA_PATH).toBe("/var/leaf/data");
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
