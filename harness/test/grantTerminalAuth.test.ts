import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SpineTurnAdapter } from "../src/agent/spineTurnAdapter.js";
import type { ConverseTurnInput, HarnessTurnEvent } from "../src/ports/converse.js";
import { FakeAppRunClient } from "../src/ports/fakes/fakeAppRunClient.js";
import { FakeConverseRunner } from "../src/ports/fakes/fakeConverseRunner.js";
import { FakeGateClient } from "../src/ports/fakes/fakeGateClient.js";
import { FakeSessionStore } from "../src/ports/fakes/fakeSessionStore.js";
import { FileTenantGrantStore, GrantPoolUnavailableError, GrantRequiredError, type TenantGrantStore } from "../src/ports/impl/oauthGrantProvider.js";
import type { AgentGrant, ConverseRunnerEvent, GrantSettlement, OAuthGrantProvider, SpineConverseRunner } from "../src/ports/index.js";
import { authorGrantSettlement } from "../src/vendor/mushy-author/agent/authorLoop.js";
import { markUntrustedOrigin, withFailureCategory } from "../src/vendor/mushy-author/agent/captureGuards.js";

class ScriptedEnvFallback implements TenantGrantStore {
  async get(): Promise<AgentGrant | null> {
    return null;
  }
}

const refused: GrantSettlement = {
  usage: { cost_tokens: 0 }, stop_reason: "error", error_code: "oauth_org_not_allowed",
};

describe("terminal grant auth refusals", () => {
  let dir: string;
  let store: FileTenantGrantStore;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "grant-terminal-auth-"));
    store = new FileTenantGrantStore({ dir, envFallback: new ScriptedEnvFallback() });
  });
  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  async function link(name: string, tenant = "acme") {
    const status = await store.put(tenant, `FAKE-acct-${name}-token`, "oauth", `private-label-${name}`, "max");
    return status.active_account_id!;
  }

  it("keeps routing to the healthy account even as its usage grows", async () => {
    await link("a");
    await link("b");
    const healthy = await store.acquire("acme");
    await store.settle("acme", healthy.lease_id, { usage: { cost_tokens: 23020 }, stop_reason: "end_turn" });
    const other = await store.acquire("acme");
    expect(other.account_id).not.toBe(healthy.account_id);
    const before = (await store.status("acme")).accounts!.find(a => a.id === other.account_id)!;
    await store.settle("acme", other.lease_id, refused);
    for (let i = 0; i < 5; i++) {
      const next = await store.acquire("acme");
      expect(next.account_id).toBe(healthy.account_id);
      await store.settle("acme", next.lease_id, { usage: { cost_tokens: 1000 }, stop_reason: "end_turn" });
    }
    const status = await store.status("acme");
    expect(status.accounts!.find(a => a.id === other.account_id)).toMatchObject({
      eligible: false, auth_refused_at: expect.any(String), auth_refused_code: "oauth_org_not_allowed",
      label: before.label, usage_tokens: 0, plan: "max",
    });
    expect(await store.get("acme")).toEqual(healthy.grant);
  });

  it("settles outstanding leases on both accounts after a refusal", async () => {
    await link("a");
    await link("b");
    const [first, second] = await Promise.all([store.acquire("acme"), store.acquire("acme")]);
    expect(first.account_id).not.toBe(second.account_id);
    const third = await store.acquire("acme");
    const [same, other] = third.account_id === first.account_id ? [first, second] : [second, first];
    await store.settle("acme", same.lease_id, refused);
    for (const lease of [third, other]) {
      await store.settle("acme", lease.lease_id, { usage: { cost_tokens: 37 }, stop_reason: "end_turn" });
    }
    const accounts = (await store.status("acme")).accounts!;
    expect(accounts.find(a => a.id === same.account_id)).toMatchObject({ eligible: false, usage_tokens: 37 });
    expect(accounts.find(a => a.id === other.account_id)).toMatchObject({ eligible: true, usage_tokens: 37 });
  });

  it("reports sorted, distinct provider codes without exposing account material", async () => {
    for (const name of ["a", "b", "c"]) await link(name);
    for (const code of ["oauth_org_not_allowed", "authentication_failed", "oauth_org_not_allowed"]) {
      const lease = await store.acquire("acme");
      await store.settle("acme", lease.lease_id, { ...refused, error_code: code });
    }
    const attempt = store.acquire("acme");
    await expect(attempt).rejects.toBeInstanceOf(GrantRequiredError);
    await expect(attempt).rejects.toThrow(/refused.*authentication_failed, oauth_org_not_allowed\)/);
    await expect(attempt).rejects.not.toThrow(/FAKE-acct|private-label/);
  });

  it("recovers a specific account by activation or adds a fresh account by relinking", async () => {
    await link("a");
    await link("b");
    const first = await store.acquire("acme");
    await store.settle("acme", first.lease_id, refused);
    const second = await store.acquire("acme");
    await store.settle("acme", second.lease_id, refused);
    const recovered = await store.activate("acme", first.account_id);
    expect(recovered.accounts!.find(a => a.id === first.account_id)).toMatchObject({
      eligible: true, auth_refused_at: null, auth_refused_code: null,
    });
    expect(recovered.accounts!.find(a => a.id === second.account_id)?.eligible).toBe(false);
    const again = await store.acquire("acme");
    expect(again.account_id).toBe(first.account_id);
    expect(again.grant).toEqual(first.grant);
    await store.settle("acme", again.lease_id, refused);
    const freshId = await link("c");
    expect((await store.acquire("acme")).account_id).toBe(freshId);
    expect((await store.status("acme")).accounts!.filter(a => a.eligible).map(a => a.id)).toEqual([freshId]);
  });

  it("ignores replay of a consumed refusal lease after activation", async () => {
    const id = await link("a");
    const lease = await store.acquire("acme");
    const outcome = { ...refused, usage: { cost_tokens: 17 } };
    await store.settle("acme", lease.lease_id, outcome);
    await store.activate("acme", id);
    await store.settle("acme", lease.lease_id, outcome);
    expect((await store.status("acme")).accounts![0]).toMatchObject({
      id, eligible: true, usage_tokens: 17, auth_refused_at: null, auth_refused_code: null,
    });
  });

  it("uses the healthy account's remaining cooldown when the other account is refused", async () => {
    await link("a");
    await link("b");
    const first = await store.acquire("acme");
    await store.settle("acme", first.lease_id, refused);
    const second = await store.acquire("acme");
    const now = Date.now();
    const clock = vi.spyOn(Date, "now").mockReturnValue(now);
    try {
      await store.settle("acme", second.lease_id, {
        usage: { cost_tokens: 0 }, stop_reason: "llm_quota_exhausted", retry_after_s: 120,
      });
      clock.mockReturnValue(now + 30_000);
      const attempt = store.acquire("acme");
      await expect(attempt).rejects.toBeInstanceOf(GrantPoolUnavailableError);
      await expect(attempt).rejects.toMatchObject({ retryAfterS: 90 });
    } finally {
      clock.mockRestore();
    }
  });

  it("isolates tenants that link identical tokens", async () => {
    await link("a");
    await link("b");
    const ids = [await link("a", "other"), await link("b", "other")];
    const lease = await store.acquire("acme");
    const path = join(dir, "other.grant.json");
    const before = readFileSync(path);
    await store.settle("acme", lease.lease_id, refused);
    expect(readFileSync(path)).toEqual(before);
    expect((await store.status("other")).accounts!.every(a => a.eligible)).toBe(true);
    const selected = [await store.acquire("other"), await store.acquire("other")];
    expect(selected.map(item => item.account_id).sort()).toEqual(ids.sort());
  });

  function writeV3(fields: Record<string, unknown> = {}) {
    const path = join(dir, "acme.grant.json");
    writeFileSync(path, JSON.stringify({
      version: 3, active_account_id: "old", accounts: [{
        id: "old", label: "old mount", token: "FAKE-old-token", kind: "oauth", plan: "max",
        linked_at: "2026-01-01T00:00:00.000Z", last_used_at: null, cooldown_until: null,
        usage_tokens: 12, selection_count: 1, leases: [], ...fields,
      }],
    }));
    return path;
  }

  it.each([
    { auth_refused_at: "not-a-date", auth_refused_code: "authentication_failed" },
    { auth_refused_at: "2026-01-01T00:00:00.000Z", auth_refused_code: "BAD-CODE" },
    { auth_refused_at: "2026-01-01T00:00:00.000Z" },
  ])("rejects invalid v3 refusal fields %j", async fields => {
    writeV3(fields);
    await expect(store.status("acme")).rejects.toThrow(/invalid persisted grant record/);
  });

  it("accepts explicit nulls and preserves a valid v3 refusal across a write", async () => {
    writeV3({ auth_refused_at: null, auth_refused_code: null });
    expect((await store.status("acme")).accounts![0]).toMatchObject({
      eligible: true, auth_refused_at: null, auth_refused_code: null,
    });
    const fields = { auth_refused_at: "2026-01-01T00:00:00.000Z", auth_refused_code: "authentication_failed" };
    writeV3(fields);
    await link("new");
    expect((await store.status("acme")).accounts!.find(account => account.id === "old")).toMatchObject({
      ...fields, eligible: false,
    });
  });

  it("reads v3 records written before the refusal fields existed", async () => {
    const path = writeV3();
    expect((await store.status("acme")).accounts![0]).toMatchObject({
      auth_refused_at: null, auth_refused_code: null, eligible: true,
    });
    const lease = await store.acquire("acme");
    expect(lease.account_id).toBe("old");
    await store.settle("acme", lease.lease_id, { usage: { cost_tokens: 1 }, stop_reason: "end_turn" });
    expect(JSON.parse(readFileSync(path, "utf8")).accounts[0]).not.toHaveProperty("auth_refused_at");
  });

  it("leaves accounts eligible after unclassified and internal errors", async () => {
    const id = await link("a");
    for (const detail of [{}, { error_code: "internal" }]) {
      const lease = await store.acquire("acme");
      await store.settle("acme", lease.lease_id, { usage: { cost_tokens: 0 }, stop_reason: "error", ...detail });
      expect((await store.status("acme")).accounts![0]).toMatchObject({
        id, eligible: true, auth_refused_at: null, auth_refused_code: null,
      });
    }
  });
});

it("classifies author auth refusals and keeps billing errors on the quota path", () => {
  for (const code of ["oauth_org_not_allowed", "authentication_failed"]) {
    const error = new Error(`Agent SDK auth failure: ${code}`);
    const plainError = { usage: { cost_tokens: 0 }, stop_reason: "error" };
    expect(authorGrantSettlement(undefined, error)).toEqual(plainError);
    withFailureCategory(error, "auth_failure");
    expect(authorGrantSettlement(undefined, error)).toEqual({
      usage: { cost_tokens: 0 }, stop_reason: "error", error_code: code,
    });
    expect(authorGrantSettlement(undefined, markUntrustedOrigin(error))).toEqual(plainError);
  }
  expect(authorGrantSettlement(undefined, new Error("Agent SDK auth failure: billing_error"))).toEqual({
    usage: { cost_tokens: 0 }, stop_reason: "llm_quota_exhausted",
  });
});

function turnInput(partial: Partial<ConverseTurnInput>): ConverseTurnInput {
  return {
    tenant_id: "demo-tenant",
    session_id: "app-session-1",
    turn_id: "app-turn-1",
    drawing_id: "rooftop_demo",
    messages: [],
    ...partial,
  };
}

async function drain(gen: AsyncIterable<HarnessTurnEvent>): Promise<HarnessTurnEvent[]> {
  const events: HarnessTurnEvent[] = [];
  for await (const ev of gen) events.push(ev);
  return events;
}

function routedAdapter(runner: SpineConverseRunner = new FakeConverseRunner()) {
  const settlements: GrantSettlement[] = [];
  const grant: AgentGrant = { kind: "oauth", oauthToken: "FAKE-routed-token" };
  const oauth: OAuthGrantProvider = {
    async getGrant() { return grant; },
    async acquireGrant() { return { grant, account_id: "account", lease_id: "lease" }; },
    async settleGrant(_tenant, _lease, outcome) { settlements.push(outcome); },
  };
  const adapter = new SpineTurnAdapter({
    oauth, appRun: new FakeAppRunClient(), gate: new FakeGateClient(),
    store: new FakeSessionStore(), runnerFor: () => runner,
    standardServicesResolver: { async resolve() { throw new Error("not called by fake"); } },
  });
  return { adapter, settlements };
}

it("passes the spine runner's error code to the routed grant settlement only when emitted", async () => {
  const { adapter, settlements } = routedAdapter();
  await drain(adapter.runTurn(turnInput({ text: "FAIL:oauth_org_not_allowed" })));
  await drain(adapter.runTurn(turnInput({ text: "hello", turn_id: "app-turn-2" })));
  expect(settlements).toHaveLength(2);
  expect(settlements[0]).toHaveProperty("error_code", "oauth_org_not_allowed");
  expect(settlements[1]).not.toHaveProperty("error_code");
});

it("keeps a BYO model refusal out of the tenant's standard-service settlement", async () => {
  const { adapter, settlements } = routedAdapter();
  await drain(adapter.runTurn(turnInput({
    text: "FAIL:oauth_org_not_allowed",
    credential_grant: { kind: "oauth", oauth_token: "FAKE-BYO-model-token" },
  })));
  expect(settlements).toEqual([{ usage: { cost_tokens: 0 }, stop_reason: "end_turn" }]);
});

it.each([7, "", "a".repeat(65)])("omits malformed runner error code %j", async code => {
  const { adapter, settlements } = routedAdapter({
    async *run(): AsyncIterable<ConverseRunnerEvent> {
      yield {
        type: "done", stopReason: "error", sdkSessionId: null,
        // Model a malformed event crossing the runner's typed boundary.
        error: { error_code: code as string, message: "fake failure", retryable: false },
      };
    },
  });
  await drain(adapter.runTurn(turnInput({ text: "hello" })));
  expect(settlements).toHaveLength(1);
  expect(settlements[0]).toMatchObject({ stop_reason: "error" });
  expect(settlements[0]).not.toHaveProperty("error_code");
});
