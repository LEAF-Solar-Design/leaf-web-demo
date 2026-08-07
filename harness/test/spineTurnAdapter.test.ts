/**
 * SpineTurnAdapter tests — hermetic (fakes only, zero network, zero Anthropic).
 * Proves the mount chip's contract: the frozen `POST /turn` wire is now served
 * by the §18 spine engine — gate consult before EVERY tool execution, submitRun
 * the only side effect, app-authoritative confirmation ids, wire-only event
 * vocabulary (the loop's turn_started / confirmation_resolved / session_state
 * never leak onto the /turn hop), GrantRequiredError pre-stream, and the
 * store-loss confirm reseed path.
 */

import { describe, expect, it } from "vitest";

import { SpineTurnAdapter } from "../src/agent/spineTurnAdapter.js";
import { GrantRequiredError } from "../src/ports/impl/oauthGrantProvider.js";
import { FakeAppRunClient } from "../src/ports/fakes/fakeAppRunClient.js";
import { FakeConverseRunner } from "../src/ports/fakes/fakeConverseRunner.js";
import { FakeGateClient } from "../src/ports/fakes/fakeGateClient.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { FakeSessionStore } from "../src/ports/fakes/fakeSessionStore.js";
import type { AgentGrant, OAuthGrantProvider } from "../src/ports/index.js";
import type { ConverseTurnInput, HarnessTurnEvent } from "../src/ports/converse.js";

/** The 9 types the frozen wire admits (ports/converse.ts HarnessTurnEvent). */
const WIRE_TYPES = new Set([
  "text_delta", "tool_call", "tool_result", "job_linked", "proposed_run",
  "confirmation_required", "turn_usage", "turn_complete", "error",
]);

function makeAdapter(opts?: { oauth?: OAuthGrantProvider; store?: FakeSessionStore }) {
  const runner = new FakeConverseRunner();
  const appRun = new FakeAppRunClient();
  const gate = new FakeGateClient();
  const store = opts?.store ?? new FakeSessionStore();
  const grants: AgentGrant[] = [];
  const adapter = new SpineTurnAdapter({
    oauth: opts?.oauth ?? new FakeOAuthGrantProvider(),
    appRun,
    gate,
    store,
    runnerFor: (grant) => {
      grants.push(grant);
      return runner;
    },
  });
  return { adapter, runner, appRun, gate, store, grants };
}

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

const typesOf = (events: HarnessTurnEvent[]): string[] => events.map((e) => e.type);

describe("SpineTurnAdapter — wire vocabulary and gate discipline", () => {
  it("read turn: streams wire-only events, gate consulted, dispatch through appRun", async () => {
    const { adapter, appRun, gate } = makeAdapter();
    const events = await drain(adapter.runTurn(turnInput({ text: "STATE:summary" })));

    const types = typesOf(events);
    expect(types).toContain("tool_call");
    expect(types).toContain("tool_result");
    expect(types[types.length - 1]).toBe("turn_complete");
    // The turn engine owns turn_started/session_state on its own hop.
    for (const t of types) expect(WIRE_TYPES.has(t)).toBe(true);

    // Gate before the read, then the read through the back-edge client.
    expect(gate.checks.length).toBeGreaterThanOrEqual(1);
    expect(gate.checks[0].action).toBe("read_platform_state");
    expect(gate.checks[0].ctx.authoritySessionId).toBe("app-session-1");
    expect(gate.checks[0].ctx.authorityTurnId).toBe("app-turn-1");
    // The harness loop retains its own ids for resume and confirmation binding.
    expect(gate.checks[0].ctx.sessionId).not.toBe("app-session-1");
    expect(gate.checks[0].ctx.turnId).not.toBe("app-turn-1");
    expect(appRun.methodLog).toContain("getDrawingState");
    expect(appRun.submitCalls.length).toBe(0);

    const complete = events[events.length - 1];
    expect(complete.data.stop_reason).toBe("end_turn");
  });

  it("first turn folds the wire's prior messages into the context packet as data", async () => {
    const { adapter, runner } = makeAdapter();
    await drain(
      adapter.runTurn(
        turnInput({
          text: "hello there",
          messages: [
            { role: "user", text: "earlier question" },
            { role: "assistant", text: "earlier answer" },
          ],
        }),
      ),
    );
    expect(runner.runs.length).toBe(1);
    expect(runner.runs[0].userMessage).toContain("prior_messages");
    expect(runner.runs[0].userMessage).toContain("earlier question");
  });

  it("a resumed turn keeps bounded visible history ready for stale-session recovery", async () => {
    const { adapter, runner } = makeAdapter();
    await drain(adapter.runTurn(turnInput({ text: "first turn" })));
    await drain(
      adapter.runTurn(
        turnInput({
          turn_id: "app-turn-2",
          text: "current turn",
          messages: [
            { role: "user", text: "first turn" },
            { role: "assistant", text: "visible answer" },
          ],
        }),
      ),
    );

    expect(runner.runs[1]!.resumeSdkSessionId).toBe("fake-sdk-session-1");
    expect(runner.runs[1]!.userMessage).not.toContain("visible answer");
    expect(runner.runs[1]!.resumeFallbackUserMessage).toContain("visible answer");
    expect(runner.runs[1]!.resumeFallbackUserMessage).toContain("current turn");
  });

  it("write turn: proposed_run with the APP-minted id, mirror row persisted, no dispatch", async () => {
    const { adapter, appRun, gate, store } = makeAdapter();
    gate.nextConfirmationId = "app-cid-1";

    const events = await drain(adapter.runTurn(turnInput({ text: "RUN:add-panel" })));

    const proposed = events.find((e) => e.type === "proposed_run");
    expect(proposed).toBeDefined();
    expect(proposed!.data.confirmation_id).toBe("app-cid-1");
    expect(proposed!.data.tool).toBe("add-panel");
    expect(appRun.submitCalls.length).toBe(0);

    const complete = events[events.length - 1];
    expect(complete.type).toBe("turn_complete");
    expect(complete.data.stop_reason).toBe("awaiting_approval");

    const mirror = await store.getConfirmation("app-cid-1");
    expect(mirror).not.toBeNull();
    expect(mirror!.action).toBe("run_capability");
  });

  it("approved confirm resume: dispatches exactly once, wire-only events", async () => {
    const { adapter, appRun, gate } = makeAdapter();
    gate.nextConfirmationId = "app-cid-2";
    await drain(adapter.runTurn(turnInput({ text: "RUN:add-panel" })));
    gate.grant("app-cid-2");

    const events = await drain(
      adapter.runTurn(
        turnInput({
          turn_id: "app-turn-2",
          confirm: {
            confirmation_id: "app-cid-2",
            approved: true,
            proposal: { tool: "add-panel", params: {} },
          },
        }),
      ),
    );

    expect(appRun.submitCalls.length).toBe(1);
    expect(appRun.submitCalls[0].tool).toBe("add-panel");
    const types = typesOf(events);
    for (const t of types) expect(WIRE_TYPES.has(t)).toBe(true);
    expect(types).not.toContain("confirmation_resolved");
    expect(events[events.length - 1].data.stop_reason).toBe("end_turn");
  });

  it("store loss between propose and confirm: reseed keeps the turn streaming; the gate denies the foreign-session redemption fail-safe", async () => {
    const shared = makeAdapter();
    shared.gate.nextConfirmationId = "app-cid-3";
    await drain(shared.adapter.runTurn(turnInput({ text: "RUN:add-panel" })));
    shared.gate.grant("app-cid-3");

    // Fresh store (harness redeploy / volume loss) — same app-side gate + appRun.
    // The loop mints a NEW session identity, so the gate's session-bound approval
    // MUST NOT redeem (that binding is the security property). The reseed's value
    // is the failure MODE: a streamed, calmly-relayed denial instead of a
    // pre-stream ConfirmationInvalid crash that burns the approval opaquely.
    const fresh = new FakeSessionStore();
    const revived = new SpineTurnAdapter({
      oauth: new FakeOAuthGrantProvider(),
      appRun: shared.appRun,
      gate: shared.gate,
      store: fresh,
      runnerFor: () => shared.runner,
    });

    const events = await drain(
      revived.runTurn(
        turnInput({
          turn_id: "app-turn-2",
          confirm: {
            confirmation_id: "app-cid-3",
            approved: true,
            proposal: { tool: "add-panel", params: {} },
          },
        }),
      ),
    );

    // Denied, not dispatched — and the turn still streamed to a clean end.
    expect(shared.appRun.submitCalls.length).toBe(0);
    const result = events.find((e) => e.type === "tool_result");
    expect(result).toBeDefined();
    expect(result!.data.ok).toBe(false);
    expect(events[events.length - 1].type).toBe("turn_complete");
    expect(events[events.length - 1].data.stop_reason).toBe("end_turn");
  });

  it("gate deny: relayed as an isError tool result, nothing dispatched, turn survives", async () => {
    const { adapter, appRun, gate } = makeAdapter();
    gate.deny("add-panel", "blocked by policy");

    const events = await drain(adapter.runTurn(turnInput({ text: "RUN:add-panel" })));

    const result = events.find((e) => e.type === "tool_result");
    expect(result).toBeDefined();
    expect(result!.data.ok).toBe(false);
    expect(appRun.submitCalls.length).toBe(0);
    expect(events[events.length - 1].type).toBe("turn_complete");
    expect(events[events.length - 1].data.stop_reason).toBe("end_turn");
  });

  it("missing grant: rejects BEFORE the first event (non-stream 401 path)", async () => {
    const throwing: OAuthGrantProvider = {
      async getGrant(tenantId: string): Promise<AgentGrant> {
        throw new GrantRequiredError(tenantId);
      },
    };
    const { adapter } = makeAdapter({ oauth: throwing });
    const iterator = adapter.runTurn(turnInput({ text: "hello" }))[Symbol.asyncIterator]();
    await expect(iterator.next()).rejects.toBeInstanceOf(GrantRequiredError);
  });

  it("settles linked-grant usage against the exact routing lease", async () => {
    const settlements: Array<{ tenantId: string; leaseId: string; outcome: unknown }> = [];
    const routed: OAuthGrantProvider = {
      async getGrant(): Promise<AgentGrant> {
        throw new Error("manual grant path must not run");
      },
      async acquireGrant() {
        return {
          grant: { kind: "oauth", oauthToken: "FAKE-routed-token" },
          account_id: "acct-1",
          lease_id: "lease-1",
        };
      },
      async settleGrant(tenantId, leaseId, outcome) {
        settlements.push({ tenantId, leaseId, outcome });
      },
    };
    const { adapter } = makeAdapter({ oauth: routed });

    await drain(adapter.runTurn(turnInput({ text: "hello" })));

    expect(settlements).toHaveLength(1);
    expect(settlements[0]).toMatchObject({
      tenantId: "demo-tenant",
      leaseId: "lease-1",
      outcome: {
        usage: { cost_tokens: expect.any(Number) },
        stop_reason: "end_turn",
      },
    });
    expect((settlements[0]!.outcome as { usage: { cost_tokens: number } }).usage.cost_tokens).toBeGreaterThan(0);
  });

  it("never acquires or settles a tenant mount for an ephemeral wire credential", async () => {
    let acquired = 0;
    let settled = 0;
    const routed: OAuthGrantProvider = {
      async getGrant(): Promise<AgentGrant> {
        throw new Error("linked grant path must not run");
      },
      async acquireGrant() {
        acquired += 1;
        throw new Error("must not acquire");
      },
      async settleGrant() {
        settled += 1;
      },
    };
    const { adapter, grants } = makeAdapter({ oauth: routed });

    await drain(adapter.runTurn(turnInput({
      text: "hello",
      credential_grant: { kind: "oauth", oauth_token: "FAKE-ephemeral-token" },
    })));

    expect(acquired).toBe(0);
    expect(settled).toBe(0);
    expect(grants).toEqual([{ kind: "oauth", oauthToken: "FAKE-ephemeral-token" }]);
  });

  it("separates a BYO model grant from the tenant-owned standard-service mount", async () => {
    const settlements: unknown[] = [];
    const routed: OAuthGrantProvider = {
      async getGrant(): Promise<AgentGrant> {
        throw new Error("linked model grant must not replace BYO");
      },
      async acquireGrant() {
        return {
          grant: { kind: "oauth", oauthToken: "linked-model-token" },
          account_id: "account-owned-by-tenant",
          lease_id: "entitlement-lease",
        };
      },
      async settleGrant(_tenantId, _leaseId, outcome) {
        settlements.push(outcome);
      },
    };
    const runner = new FakeConverseRunner();
    const adapter = new SpineTurnAdapter({
      oauth: routed,
      appRun: new FakeAppRunClient(),
      gate: new FakeGateClient(),
      store: new FakeSessionStore(),
      runnerFor: () => runner,
      standardServicesResolver: { async resolve() { throw new Error("not called by fake"); } },
    });

    await drain(adapter.runTurn(turnInput({
      text: "hello",
      credential_grant: { kind: "oauth", oauth_token: "BYO-model-token" },
    })));

    expect(runner.runs[0]?.standardServicesContext).toEqual({
      tenant_id: "demo-tenant",
      session_id: "app-session-1",
      subscription_mount_id: "account-owned-by-tenant",
      authority_session_id: "app-session-1",
      authority_turn_id: "app-turn-1",
    });
    expect(settlements).toEqual([{ usage: { cost_tokens: 0 }, stop_reason: "end_turn" }]);
  });

  it("aborted signal: the stream ends without yielding further events", async () => {
    const { adapter } = makeAdapter();
    const ctrl = new AbortController();
    ctrl.abort();
    const iterator = adapter
      .runTurn(turnInput({ text: "STATE:summary" }), { signal: ctrl.signal })
      [Symbol.asyncIterator]();
    const first = await iterator.next();
    expect(first.done).toBe(true);
  });
});
