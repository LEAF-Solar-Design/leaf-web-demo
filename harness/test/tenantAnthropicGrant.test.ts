import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentGrant, GrantLease, GrantSettlement, OAuthGrantProvider } from "../src/ports/index.js";
import { TenantAnthropicGrantProvider } from "../src/ports/impl/tenantAnthropicGrant.js";
import { FileTenantProviderStore, TenantProviderConfigError } from "../src/ports/impl/tenantProviderStore.js";
import { createTenantRunnerFor } from "../src/ports/impl/tenantRunnerFor.js";
import { buildScrubbedEnv } from "../src/vendor/mushy-author/ports/impl/agentSdkRunner.js";

const record = {
  provider: "anthropic_compatible", base_url: "https://llm.example.test/v1", model: "qwen3.8-27b",
  api_key_env: "LEAF_PROVIDER_KEY_MINI", label: "local on Mac mini",
};
const env = { LEAF_TENANT_PROVIDER_HOSTS: "llm.example.test", LEAF_PROVIDER_KEY_MINI: "test-mini-key" };
const original: AgentGrant = { kind: "oauth", oauthToken: "test-linked-token" };
let dir: string;
beforeEach(async () => { dir = await mkdtemp(join(tmpdir(), "tenant-anthropic-")); });
afterEach(async () => { await rm(dir, { recursive: true, force: true }); });

async function writeRecord(raw: unknown = record) {
  await writeFile(join(dir, "film-tenant.provider.json"), JSON.stringify(raw));
}

describe("tenant Anthropic grant routing", () => {
  it("returns the selected tenant's key, base URL and model without delegating", async () => {
    await writeRecord();
    const getGrant = vi.fn(async () => original);
    const provider = new TenantAnthropicGrantProvider({ getGrant }, dir, env);
    expect(await provider.getGrant("film-tenant")).toEqual({
      kind: "api_key", apiKey: env.LEAF_PROVIDER_KEY_MINI, baseUrl: record.base_url, model: record.model,
    });
    expect(getGrant).not.toHaveBeenCalled();
    expect(await provider.getGrant("other-tenant")).toBe(original);
  });

  it("delegates unchanged with no record or an OpenAI record", async () => {
    const getGrant = vi.fn(async () => original);
    const provider = new TenantAnthropicGrantProvider({ getGrant }, dir, env);
    expect(await provider.getGrant("film-tenant")).toBe(original);
    await writeRecord({ ...record, provider: "openai_compatible" });
    expect(await provider.getGrant("film-tenant")).toBe(original);
    expect(getGrant.mock.calls).toHaveLength(2);
  });

  it("validates writes for both providers and accepts an omitted label", async () => {
    const store = new FileTenantProviderStore(dir);
    const { label: _label, ...withoutLabel } = record;
    expect(await store.put("film-tenant", withoutLabel, new Set(["llm.example.test"])))
      .toMatchObject({ provider: "anthropic_compatible", label: "tenant model" });
    await expect(store.put("film-tenant", { ...record, base_url: "http://llm.example.test" },
      new Set(["llm.example.test"]))).rejects.toBeInstanceOf(TenantProviderConfigError);
  });

  it.each([
    { ...env, LEAF_TENANT_PROVIDER_HOSTS: "" },
    { ...env, LEAF_PROVIDER_KEY_MINI: "" },
  ])("fails closed with a value-free error when the route cannot be honoured", async badEnv => {
    await writeRecord();
    const getGrant = vi.fn(async () => original);
    const provider = new TenantAnthropicGrantProvider({ getGrant }, dir, badEnv);
    await expect(provider.getGrant("film-tenant")).rejects.toBeInstanceOf(TenantProviderConfigError);
    await expect(provider.getGrant("film-tenant")).rejects.not.toThrow(env.LEAF_PROVIDER_KEY_MINI);
    expect(getGrant).not.toHaveBeenCalled();
  });

  it("delegates optional lease methods with their receiver and arguments intact", async () => {
    const lease: GrantLease = { grant: original, account_id: "account", lease_id: "lease" };
    const outcome: GrantSettlement = { usage: { cost_tokens: 0 }, stop_reason: "end_turn" };
    const inner: OAuthGrantProvider = {
      getGrant: async () => original,
      async acquireGrant(tenantId) {
        expect(this).toBe(inner);
        expect(tenantId).toBe("film-tenant");
        return lease;
      },
      async settleGrant(tenantId, leaseId, settlement) {
        expect(this).toBe(inner);
        expect([tenantId, leaseId, settlement]).toEqual(["film-tenant", "lease", outcome]);
      },
    };
    const provider = new TenantAnthropicGrantProvider(inner, dir, env);
    expect(await provider.acquireGrant!("film-tenant")).toBe(lease);
    await provider.settleGrant!("film-tenant", "lease", outcome);
    const minimal = new TenantAnthropicGrantProvider({ getGrant: inner.getGrant }, dir, env);
    expect(minimal.acquireGrant).toBeUndefined();
    expect(minimal.settleGrant).toBeUndefined();
  });

  it("routes an Anthropic tenant's chat lease to its own grant and never settles it upstream", async () => {
    await writeRecord();
    const pooled: GrantLease = { grant: original, account_id: "pool", lease_id: "pool-1" };
    const inner = {
      getGrant: vi.fn(async () => original),
      acquireGrant: vi.fn(async () => pooled),
      settleGrant: vi.fn(async () => undefined),
    };
    const provider = new TenantAnthropicGrantProvider(inner, dir, env);
    const lease = await provider.acquireGrant!("film-tenant");
    expect(lease.grant).toEqual({
      kind: "api_key", apiKey: env.LEAF_PROVIDER_KEY_MINI, baseUrl: record.base_url, model: record.model,
    });
    expect(lease.lease_id.startsWith("tenant-provider:")).toBe(true);
    expect(inner.acquireGrant).not.toHaveBeenCalled();
    const outcome: GrantSettlement = { usage: { cost_tokens: 1 }, stop_reason: "end_turn" };
    await provider.settleGrant!("film-tenant", lease.lease_id, outcome);
    expect(inner.settleGrant).not.toHaveBeenCalled();
    expect(await provider.acquireGrant!("other-tenant")).toBe(pooled);
    await provider.settleGrant!("other-tenant", "pool-1", outcome);
    expect(inner.settleGrant).toHaveBeenCalledWith("other-tenant", "pool-1", outcome);
  });

  it("does not select an OpenAI runner for an Anthropic record", async () => {
    await writeRecord();
    expect(await createTenantRunnerFor({ grantsDir: dir, env })("film-tenant", original)).toBeNull();
  });
});

describe("tenant SDK environment", () => {
  const ambient = {
    ANTHROPIC_BASE_URL: "https://ambient.example.test", Anthropic_Base_Url: "https://mixed.example.test",
    ANTHROPIC_MODEL: "ambient-model", ANTHROPIC_SMALL_FAST_MODEL: "ambient-fast",
    ANTHROPIC_API_KEY: "ambient-key", CLAUDE_CODE_OAUTH_TOKEN: "ambient-token",
    LEAF_PROVIDER_KEY_MINI: env.LEAF_PROVIDER_KEY_MINI, PATH: "/bin",
  };

  it("injects only the selected route after scrubbing ambient identities and routing", () => {
    const child = buildScrubbedEnv({ kind: "api_key", apiKey: env.LEAF_PROVIDER_KEY_MINI,
      baseUrl: record.base_url, model: record.model }, ambient);
    expect(child).toEqual({ PATH: "/bin", ANTHROPIC_API_KEY: env.LEAF_PROVIDER_KEY_MINI,
      ANTHROPIC_BASE_URL: record.base_url, ANTHROPIC_MODEL: record.model, ANTHROPIC_SMALL_FAST_MODEL: record.model });
    expect(ambient.ANTHROPIC_API_KEY).toBe("ambient-key");
  });

  it("leaves ordinary API key and OAuth grants without ambient routing", () => {
    expect(buildScrubbedEnv({ kind: "api_key", apiKey: "ordinary-key" }, ambient))
      .toEqual({ PATH: "/bin", ANTHROPIC_API_KEY: "ordinary-key" });
    expect(buildScrubbedEnv(original, ambient))
      .toEqual({ PATH: "/bin", CLAUDE_CODE_OAUTH_TOKEN: "test-linked-token" });
  });
});
