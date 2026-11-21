import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { SpineTurnAdapter } from "../src/agent/spineTurnAdapter.js";
import { FakeAppRunClient } from "../src/ports/fakes/fakeAppRunClient.js";
import { FakeConverseRunner } from "../src/ports/fakes/fakeConverseRunner.js";
import { FakeGateClient } from "../src/ports/fakes/fakeGateClient.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { FakeSessionStore } from "../src/ports/fakes/fakeSessionStore.js";
import type { AgentGrant, IntentSynthesizer } from "../src/ports/index.js";
import {
  FileTenantProviderStore, allowedProviderHosts, parseTenantProviderOverride, resolveTenantProvider,
} from "../src/ports/impl/tenantProviderStore.js";
import { OpenAiConverseRunner } from "../src/ports/impl/openaiConverseRunner.js";
import type { ConverseTurnInput, HarnessTurnEvent } from "../src/ports/converse.js";

const hosts = new Set(["llm.example.test"]);
const record = {
  provider: "openai_compatible", base_url: "https://llm.example.test/v1/", model: "qwen3.8-27b",
  api_key_env: "LEAF_PROVIDER_KEY_MINI", label: "local on Mac mini",
};
const env = { LEAF_TENANT_PROVIDER_HOSTS: "llm.example.test", LEAF_PROVIDER_KEY_MINI: "mini-key-not-real-000000000000" };

let dir: string;
beforeEach(async () => { dir = await mkdtemp(join(tmpdir(), "tenant-provider-")); });
afterEach(async () => { await rm(dir, { recursive: true, force: true }); });

describe("tenant provider records", () => {
  it("validates and normalizes a record, refusing every unsafe field", () => {
    expect(parseTenantProviderOverride(record, hosts)).toEqual({ ...record, base_url: "https://llm.example.test/v1" });
    expect(parseTenantProviderOverride({ ...record, turn_timeout_s: 300 }, hosts)).toMatchObject({ turn_timeout_s: 300 });
    const bad: Record<string, unknown>[] = [
      { ...record, provider: "anthropic" },
      { ...record, base_url: "http://llm.example.test/v1" },
      { ...record, base_url: "https://evil.example.test/v1" },
      { ...record, base_url: "https://user:pw@llm.example.test/v1" },
      { ...record, base_url: "https://llm.example.test:8443/v1" },
      { ...record, base_url: "https://llm.example.test/v1?x=1" },
      { ...record, api_key_env: "ANTHROPIC_API_KEY" },
      { ...record, api_key_env: "LEAF_HARNESS_SECRET" },
      { ...record, model: "" },
      { ...record, label: "<script>" },
      { ...record, turn_timeout_s: 5 },
      { ...record, api_key: "inline-secret" },
    ];
    for (const raw of bad) expect(() => parseTenantProviderOverride(raw, hosts)).toThrow();
    expect(() => parseTenantProviderOverride(record, new Set())).toThrow("base_url host not allowed");
  });

  it("reads the host allowlist strictly", () => {
    expect([...allowedProviderHosts({ LEAF_TENANT_PROVIDER_HOSTS: " LLM.example.test , a.b.test " })])
      .toEqual(["llm.example.test", "a.b.test"]);
    expect(allowedProviderHosts({}).size).toBe(0);
    expect(() => allowedProviderHosts({ LEAF_TENANT_PROVIDER_HOSTS: "https://x.test" })).toThrow();
  });

  it("stores per tenant beside the grants, never a key value, and removes cleanly", async () => {
    const store = new FileTenantProviderStore(dir);
    expect(await store.get("film-tenant")).toBeNull();
    await store.put("film-tenant", record, hosts);
    const text = await readFile(join(dir, "film-tenant.provider.json"), "utf8");
    expect(text).not.toContain(env.LEAF_PROVIDER_KEY_MINI);
    expect(await store.get("other-tenant")).toBeNull();
    await expect(store.put("../escape", record, hosts)).rejects.toThrow("invalid tenant id");
    await expect(store.put("film-tenant", { ...record, base_url: "https://evil.example.test" }, hosts)).rejects.toThrow();
    await store.remove("film-tenant");
    expect(await store.get("film-tenant")).toBeNull();
  });

  it("resolves only a configured tenant, and fails closed on a missing key or a revoked host", async () => {
    const store = new FileTenantProviderStore(dir);
    await store.put("film-tenant", record, hosts);
    expect(await resolveTenantProvider(store, "other-tenant", env)).toBeNull();
    expect(await resolveTenantProvider(store, "film-tenant", env)).toMatchObject({
      apiKey: env.LEAF_PROVIDER_KEY_MINI, override: { model: "qwen3.8-27b", label: "local on Mac mini" },
    });
    await expect(resolveTenantProvider(store, "film-tenant", { ...env, LEAF_PROVIDER_KEY_MINI: "" }))
      .rejects.toThrow("provider key unavailable");
    await expect(resolveTenantProvider(store, "film-tenant", { LEAF_PROVIDER_KEY_MINI: "k" }))
      .rejects.toThrow("base_url host not allowed");
    await writeFile(join(dir, "broken.provider.json"), "{", "utf8");
    await expect(resolveTenantProvider(store, "broken", env)).rejects.toThrow("provider record unreadable");
  });
});

function turnInput(tenant: string, text: string): ConverseTurnInput {
  return { tenant_id: tenant, session_id: "s1", turn_id: "t1", drawing_id: "rooftop_demo", messages: [], text };
}
async function drain(gen: AsyncIterable<HarnessTurnEvent>) {
  const events: HarnessTurnEvent[] = [];
  for await (const ev of gen) events.push(ev);
  return events;
}

describe("SpineTurnAdapter tenant provider override", () => {
  function adapter(tenantRunnerFor: ConstructorParameters<typeof SpineTurnAdapter>[0]["tenantRunnerFor"]) {
    const defaultRunner = new FakeConverseRunner();
    const intents: string[] = [];
    const intent: IntentSynthesizer = { async synthesize(text) { intents.push(text); return null; } };
    const grants: AgentGrant[] = [];
    const subject = new SpineTurnAdapter({
      oauth: new FakeOAuthGrantProvider(), appRun: new FakeAppRunClient(), gate: new FakeGateClient(),
      store: new FakeSessionStore(),
      runnerFor: (grant) => { grants.push(grant); return defaultRunner; },
      intentFor: () => intent,
      tenantRunnerFor,
    });
    return { subject, defaultRunner, intents, grants };
  }

  it("leaves every tenant without a record on the default runner and classifier", async () => {
    const seen: string[] = [];
    const { subject, defaultRunner, intents } = adapter(async (tenant) => { seen.push(tenant); return null; });
    await drain(subject.runTurn(turnInput("investor-tenant", "STATE:summary")));
    expect(seen).toEqual(["investor-tenant"]);
    expect(defaultRunner.runs).toHaveLength(1);
    expect(intents).toHaveLength(1);
  });

  it("routes only the configured tenant to its runner, skips the Claude classifier and scrubs its key", async () => {
    const tenantRunner = new FakeConverseRunner();
    const key = "mini-key-not-real-0123456789abcdef";
    const { subject, defaultRunner, intents } = adapter(async (tenant) =>
      tenant === "film-tenant" ? { runner: tenantRunner, secrets: [key] } : null);
    await drain(subject.runTurn(turnInput("film-tenant", `STATE:summary ${key}`)));
    expect(tenantRunner.runs).toHaveLength(1);
    expect(tenantRunner.runs[0]?.userMessage).not.toContain(key);
    expect(defaultRunner.runs).toHaveLength(0);
    expect(intents).toHaveLength(0);
    await drain(subject.runTurn({ ...turnInput("other-tenant", "STATE:summary"), session_id: "s2", turn_id: "t2" }));
    expect(defaultRunner.runs).toHaveLength(1);
    expect(tenantRunner.runs).toHaveLength(1);
  });

  it("fails the turn before any stream when the override cannot be honoured", async () => {
    const { subject, defaultRunner } = adapter(async () => { throw new Error("provider key unavailable"); });
    await expect(drain(subject.runTurn(turnInput("film-tenant", "STATE:summary"))))
      .rejects.toThrow("provider key unavailable");
    expect(defaultRunner.runs).toHaveLength(0);
  });
});

describe("OpenAiConverseRunner identity", () => {
  it("labels every usage event with the provider that actually answered", async () => {
    const fetchImpl = async () => Response.json({
      model: "qwen3.8-27b", output: [{ type: "message", content: [{ type: "output_text", text: "hi" }] }],
      usage: { input_tokens: 2, output_tokens: 1 },
    });
    const run = (identity?: ConstructorParameters<typeof OpenAiConverseRunner>[0]["identity"]) =>
      new OpenAiConverseRunner({
        apiKey: "k", model: "qwen3.8-27b", fetchImpl, grant: { kind: "oauth", oauthToken: "g" },
        ...(identity ? { identity } : {}),
      }).run({
        systemPrompt: "S", userMessage: "hi", tools: { list: () => [], execute: async () => ({ content: "" }) },
        canUseTool: async (_n, args) => ({ behavior: "allow", updatedInput: args }),
      });
    const usage = async (identity?: Parameters<typeof run>[0]) => {
      for await (const event of run(identity)) if (event.type === "usage") return event.usage;
      return undefined;
    };
    expect(await usage({ provider: "openai_compatible", label: "local on Mac mini" })).toMatchObject({
      models: ["qwen3.8-27b"], provider: "openai_compatible", provider_label: "local on Mac mini",
    });
    const plain = await usage();
    expect(plain).toMatchObject({ provider: "openai" });
    expect(plain).not.toHaveProperty("provider_label");
  });
});
