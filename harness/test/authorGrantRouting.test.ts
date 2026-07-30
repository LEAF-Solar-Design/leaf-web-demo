import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";

import { AuthorLoop, authorGrantSettlement } from "../src/agent/authorLoop.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { FakeTenantRepoProvider } from "../src/ports/fakes/fakeTenantRepo.js";
import type { AgentGrant, HarnessPorts } from "../src/ports/index.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE = join(HERE, "fixtures", "tenant-repo");
const TENANT = "author-routing-tenant";

describe("author grant routing", () => {
  it("settles successful author telemetry as cost-relevant usage", () => {
    expect(authorGrantSettlement({ telemetry: { input_tokens: 120, output_tokens: 30 } } as never, undefined)).toEqual({
      usage: { cost_tokens: 150 },
      stop_reason: "end_turn",
    });
  });

  it("classifies rate limits with their retry horizon", () => {
    expect(authorGrantSettlement(undefined, new Error("Agent SDK rate limited (retry after ~42s)"))).toEqual({
      usage: { cost_tokens: 0 },
      stop_reason: "llm_rate_limited",
      retry_after_s: 42,
    });
  });

  it("classifies quota and other failures without inventing usage", () => {
    expect(authorGrantSettlement(undefined, new Error("Agent SDK auth failure: billing_error"))).toEqual({
      usage: { cost_tokens: 0 },
      stop_reason: "llm_quota_exhausted",
    });
    expect(authorGrantSettlement(undefined, new Error("author runner failed"))).toEqual({
      usage: { cost_tokens: 0 },
      stop_reason: "error",
    });
    expect(authorGrantSettlement(undefined, new Error("schema omits required field quota"))).toEqual({
      usage: { cost_tokens: 0 },
      stop_reason: "error",
    });
  });

  it("prefers a routed lease and settles its exact account on success", async () => {
    const leasedGrant: AgentGrant = { kind: "api_key", apiKey: "FAKE-ROUTED-KEY" };
    const getGrant = vi.fn(async (): Promise<AgentGrant> => {
      throw new Error("legacy grant lookup must not run");
    });
    const acquireGrant = vi.fn(async () => ({
      grant: leasedGrant,
      account_id: "account-routed",
      lease_id: "lease-routed",
    }));
    const settleGrant = vi.fn(async () => undefined);
    const fakeRunner = new FakeAgentRunner();
    const run = vi.fn(async (input: Parameters<typeof fakeRunner.run>[0]) => {
      expect(input.grant).toBe(leasedGrant);
      return fakeRunner.run(input);
    });
    const ports: HarnessPorts = {
      oauth: { getGrant, acquireGrant, settleGrant },
      tenantRepo: new FakeTenantRepoProvider(FIXTURE),
      broker: new FakeBrokerApsClient(),
      agentRunner: { run },
    };

    await expect(new AuthorLoop(ports).buildLegacyAuthOff(TENANT, "count entities per layer")).resolves.toBeDefined();
    expect(getGrant).not.toHaveBeenCalled();
    expect(acquireGrant).toHaveBeenCalledWith(TENANT);
    expect(settleGrant).toHaveBeenCalledOnce();
    expect(settleGrant).toHaveBeenCalledWith(TENANT, "lease-routed", {
      usage: { cost_tokens: 0 },
      stop_reason: "end_turn",
    });
  });

  it("settles failures without allowing settlement errors to mask the root cause", async () => {
    const rootFailure = new Error("Agent SDK rate limited (retry after ~19s)");
    const settleGrant = vi.fn(async () => {
      throw new Error("settlement unavailable");
    });
    const ports: HarnessPorts = {
      oauth: {
        getGrant: vi.fn(async (): Promise<AgentGrant> => ({ kind: "api_key", apiKey: "UNUSED" })),
        acquireGrant: vi.fn(async () => ({
          grant: { kind: "api_key", apiKey: "FAKE-ROUTED-KEY" } as AgentGrant,
          account_id: "account-routed",
          lease_id: "lease-routed",
        })),
        settleGrant,
      },
      tenantRepo: new FakeTenantRepoProvider(FIXTURE),
      broker: new FakeBrokerApsClient(),
      agentRunner: { run: vi.fn(async () => { throw rootFailure; }) },
    };

    await expect(new AuthorLoop(ports).buildLegacyAuthOff(TENANT, "count entities per layer")).rejects.toBe(rootFailure);
    expect(settleGrant).toHaveBeenCalledWith(TENANT, "lease-routed", {
      usage: { cost_tokens: 0 },
      stop_reason: "llm_rate_limited",
      retry_after_s: 19,
    });
  });
});
