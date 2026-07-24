/**
 * ConverseLoop unit tests — hermetic (fakes only, zero network, zero Anthropic).
 * Covers the section-18 turn machinery: the ONE-active-turn lock, persisted
 * monotonic seq + replay, read auto-dispatch, write -> proposed_run split turns,
 * confirm-resume dispatch, gate-deny relay, and the wire event vocabulary.
 */

import { describe, expect, it } from "vitest";
import { createHash } from "node:crypto";

import {
  BadMessageError,
  ConverseLoop,
  SessionNotFoundError,
  TurnInProgressError,
} from "../src/agent/converseLoop.js";
import { FakeAppRunClient } from "../src/ports/fakes/fakeAppRunClient.js";
import { FakeConverseRunner } from "../src/ports/fakes/fakeConverseRunner.js";
import { FakeGateClient } from "../src/ports/fakes/fakeGateClient.js";
import { FakeSessionStore } from "../src/ports/fakes/fakeSessionStore.js";
import type {
  ConverseEvent,
  GateCheckContext,
  GateCheckResult,
  SessionRecord,
  SpineConverseRunner,
  StoredEvent,
} from "../src/ports/index.js";

const PACKET = {
  catalog: [{ name: "count-by-layer", description: "Count entities per layer", capabilities: ["drawing.read"] }],
  drawing: { id: "rooftop_demo", head_version: 3 },
  grant: { kind: "oauth", degraded: false },
};

/** A confirmation id only the (fake) app gate can produce. */
const APP_MINTED_CID = "app-minted-confirmation-1";

function makeLoop() {
  const runner = new FakeConverseRunner();
  const appRun = new FakeAppRunClient();
  const gate = new FakeGateClient();
  const store = new FakeSessionStore();
  const loop = new ConverseLoop(
    { runner, appRun, gate, store },
    { model: "claude-sonnet-5" },
  );
  return { loop, runner, appRun, gate, store };
}

async function sendText(
  loop: ConverseLoop,
  session: SessionRecord,
  text: string,
  onEvent?: (ev: ConverseEvent) => void,
): Promise<string> {
  const { turnId, done } = await loop.handleMessage({
    sessionId: session.session_id,
    tenantId: session.tenant_id,
    text,
    contextPacket: PACKET,
    ...(onEvent ? { onEvent } : {}),
  });
  await done;
  return turnId;
}

function ofType(events: StoredEvent[], type: string): StoredEvent[] {
  return events.filter((e) => e.type === type);
}

/**
 * Model the app gate's request_confirmation policy (always-confirm, rung 0,
 * NOT tenant-tightenable): the verdict is awaiting_approval and the APP mints
 * the confirmation id — the harness never mints one (wire contract section 6).
 */
function gateAlwaysConfirms(gate: FakeGateClient, confirmationId: string): void {
  const origCheck = gate.check.bind(gate);
  gate.check = async (action: string, args: Record<string, unknown>, ctx: GateCheckContext) => {
    if (action !== "request_confirmation") return origCheck(action, args, ctx);
    const verdict: GateCheckResult = {
      decision: "awaiting_approval",
      confirmation_id: confirmationId,
      reason: "user confirmation required",
      policy: "always_confirm",
      rung: "R0",
    };
    gate.checks.push({ action, args, ctx, decision: verdict.decision });
    return verdict;
  };
}

describe("ConverseLoop — sessions", () => {
  it("createOrGetSession is idempotent per (tenant, drawing)", async () => {
    const { loop } = makeLoop();
    const a = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    const b = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    const c = await loop.createOrGetSession("demo-tenant", "other_drawing");
    expect(b.session_id).toBe(a.session_id);
    expect(c.session_id).not.toBe(a.session_id);
    expect(a.status).toBe("idle");
  });

  it("tenant mismatch answers session_not_found (no existence oracle)", async () => {
    const { loop } = makeLoop();
    const s = await loop.createOrGetSession("tenant-a", "rooftop_demo");
    await expect(
      loop.handleMessage({
        sessionId: s.session_id,
        tenantId: "tenant-b",
        text: "hello",
        contextPacket: PACKET,
      }),
    ).rejects.toBeInstanceOf(SessionNotFoundError);
  });

  it("requires exactly one of text | confirm", async () => {
    const { loop } = makeLoop();
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    await expect(
      loop.handleMessage({ sessionId: s.session_id, tenantId: "demo-tenant", contextPacket: PACKET }),
    ).rejects.toBeInstanceOf(BadMessageError);
    await expect(
      loop.handleMessage({
        sessionId: s.session_id,
        tenantId: "demo-tenant",
        text: "hi",
        confirm: { confirmationId: "x", approved: true },
        contextPacket: PACKET,
      }),
    ).rejects.toBeInstanceOf(BadMessageError);
  });

  it("clears a stale SDK resume id when the fresh recovery also fails", async () => {
    const appRun = new FakeAppRunClient();
    const gate = new FakeGateClient();
    const store = new FakeSessionStore();
    const runner: SpineConverseRunner = {
      async *run() {
        yield {
          type: "done",
          stopReason: "error",
          sdkSessionId: null,
          sdkSessionReset: true,
          error: { error_code: "internal", message: "fresh query failed", retryable: false },
        };
      },
    };
    const loop = new ConverseLoop({ runner, appRun, gate, store });
    const session = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    await store.updateSession(session.session_id, { sdk_session_id: "stale-sdk-session" });

    await sendText(loop, session, "hello");

    expect((await store.getSession(session.session_id))!.sdk_session_id).toBeNull();
  });
});

describe("ConverseLoop — turn lock", () => {
  it("a second message during an active turn -> TurnInProgressError carrying the active turnId", async () => {
    const { loop, runner, store } = makeLoop();
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");

    let release!: () => void;
    runner.slowGate = new Promise<void>((r) => {
      release = r;
    });

    const first = await loop.handleMessage({
      sessionId: s.session_id,
      tenantId: "demo-tenant",
      text: "SLOW think about it",
      contextPacket: PACKET,
    });

    const second = loop.handleMessage({
      sessionId: s.session_id,
      tenantId: "demo-tenant",
      text: "second message",
      contextPacket: PACKET,
    });
    await expect(second).rejects.toBeInstanceOf(TurnInProgressError);
    await expect(second).rejects.toMatchObject({ turnId: first.turnId });

    release();
    await first.done;

    // Lock released: the next message starts a fresh turn.
    const third = await loop.handleMessage({
      sessionId: s.session_id,
      tenantId: "demo-tenant",
      text: "hello again",
      contextPacket: PACKET,
    });
    await third.done;
    expect(third.turnId).not.toBe(first.turnId);
    expect((await store.getSession(s.session_id))?.status).toBe("idle");
  });

  it("a confirm during an active turn -> TurnInProgressError WITHOUT consuming the confirmation", async () => {
    const { loop, runner, appRun, gate, store } = makeLoop();
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");

    // Propose a write -> pending confirmation.
    await sendText(loop, s, "RUN:add-panel");
    const cid = String(
      ofType(await store.eventsAfter(s.session_id, 0), "proposed_run")[0]!.data.confirmation_id,
    );

    // Occupy the turn lock with a slow-gated turn.
    let release!: () => void;
    runner.slowGate = new Promise<void>((r) => {
      release = r;
    });
    const slow = await loop.handleMessage({
      sessionId: s.session_id,
      tenantId: "demo-tenant",
      text: "SLOW hold the lock",
      contextPacket: PACKET,
    });

    await expect(
      loop.handleMessage({
        sessionId: s.session_id,
        tenantId: "demo-tenant",
        confirm: { confirmationId: cid, approved: true },
        contextPacket: PACKET,
      }),
    ).rejects.toBeInstanceOf(TurnInProgressError);
    // The 409 did NOT burn the approval: the record is still pending...
    expect((await store.getConfirmation(cid))!.status).toBe("pending");

    release();
    await slow.done;

    // ...so the client's retry after the active turn ends still dispatches.
    gate.grant(cid);
    const retry = await loop.handleMessage({
      sessionId: s.session_id,
      tenantId: "demo-tenant",
      confirm: { confirmationId: cid, approved: true },
      contextPacket: PACKET,
    });
    await retry.done;
    expect(appRun.submitCalls.some((c) => c.tool === "add-panel")).toBe(true);
    expect((await store.getConfirmation(cid))!.status).toBe("approved");
  });
});

describe("ConverseLoop — seq + replay", () => {
  it("seq is per-session monotonic and eventsAfter replays only seq > afterSeq", async () => {
    const { loop, store } = makeLoop();
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    const live: ConverseEvent[] = [];
    await sendText(loop, s, "hello", (ev) => live.push(ev));
    await sendText(loop, s, "STATE:summary");

    const all = await store.eventsAfter(s.session_id, 0);
    expect(all.length).toBeGreaterThan(4);
    for (let i = 0; i < all.length; i++) expect(all[i]!.seq).toBe(i + 1);

    const after = await store.eventsAfter(s.session_id, 3);
    expect(after.every((e) => e.seq > 3)).toBe(true);
    expect(after.length).toBe(all.length - 3);

    // Live fan-out mirrors the persisted transcript (same seq, same envelope).
    expect(live.length).toBeGreaterThan(0);
    expect(live[0]).toMatchObject({ v: 1, session_id: s.session_id, seq: 1, type: "turn_started" });
    expect(live[0]!.data).toMatchObject({ model: "claude-sonnet-5" });
  });
});

describe("ConverseLoop — read auto-dispatch", () => {
  it("run_capability on a READ tool dispatches inline via the wait path", async () => {
    const { loop, appRun, gate, store } = makeLoop();
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    await sendText(loop, s, "RUN:count-by-layer");

    // Dispatch went through submitRun with the section-7 payload + 15s wait budget.
    expect(appRun.submitCalls).toHaveLength(1);
    expect(appRun.submitCalls[0]).toMatchObject({
      tenantId: "demo-tenant",
      tool: "count-by-layer",
      params: {},
      dwg: "rooftop_demo",
      wait: true,
      waitTimeoutS: 15,
    });

    // The gate ran before the execution — in the app policy catalog's vocabulary.
    expect(gate.checks.some((c) => c.action === "run_read_tool" && c.decision === "allow")).toBe(true);

    const events = await store.eventsAfter(s.session_id, 0);
    expect(ofType(events, "tool_call")[0]!.data).toMatchObject({
      tool: "run_capability",
      args_summary: "tool=count-by-layer",
    });
    expect(ofType(events, "job_linked")[0]!.data).toMatchObject({ tool: "count-by-layer" });
    expect(ofType(events, "tool_result")[0]!.data).toMatchObject({ tool: "run_capability", ok: true });
    expect(ofType(events, "turn_usage")[0]!.data).toMatchObject({ cost_tokens: 170 });
    expect(ofType(events, "turn_complete")[0]!.data).toEqual({ stop_reason: "end_turn" });
  });
});

describe("ConverseLoop — write split turns (wire contract section 7)", () => {
  it("run_capability on a WRITE tool proposes and the turn ends awaiting_approval", async () => {
    const { loop, appRun, store } = makeLoop();
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    await sendText(loop, s, 'RUN:add-panel PARAMS:{"row":2}');

    // NOTHING dispatched.
    expect(appRun.submitCalls).toHaveLength(0);

    const events = await store.eventsAfter(s.session_id, 0);
    const proposed = ofType(events, "proposed_run");
    expect(proposed).toHaveLength(1);
    expect(proposed[0]!.data).toMatchObject({
      tool: "add-panel",
      params: { row: 2 },
      // The chip renders the TARGET drawing (resolved exactly as dispatch will).
      dwg: "rooftop_demo",
      capability: "drawing.write",
    });
    expect(typeof proposed[0]!.data.confirmation_id).toBe("string");
    expect(typeof proposed[0]!.data.rationale).toBe("string");
    expect(ofType(events, "turn_complete")[0]!.data).toEqual({ stop_reason: "awaiting_approval" });

    // The mirror confirmation record is pending + args-bound.
    const cid = String(proposed[0]!.data.confirmation_id);
    const rec = await store.getConfirmation(cid);
    expect(rec).toMatchObject({ status: "pending", action: "run_capability" });
    expect(JSON.parse(rec!.args_json)).toMatchObject({ tool: "add-panel", params: { row: 2 } });

    // The 300s TTL (wire contract section 7) is pinned at CREATION, not just
    // enforced at read time: expires_at = created_at + 300s, still in the future.
    expect(Date.parse(rec!.expires_at) - Date.parse(rec!.created_at)).toBe(300_000);
    const ttlMs = Date.parse(rec!.expires_at) - Date.now();
    expect(ttlMs).toBeGreaterThan(295_000);
    expect(ttlMs).toBeLessThanOrEqual(300_000);
  });

  it("an approved confirm message resumes and dispatches (gate grants on confirmation_id)", async () => {
    const { loop, appRun, gate, store, runner } = makeLoop();
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    await sendText(loop, s, "RUN:add-panel");

    const events1 = await store.eventsAfter(s.session_id, 0);
    const cid = String(ofType(events1, "proposed_run")[0]!.data.confirmation_id);
    const lastSeq = events1[events1.length - 1]!.seq;

    // Section 7 step 3a: the app approves the pending record; then the client
    // posts the confirm message (step 3b) which starts the resume turn.
    gate.grant(cid);
    const { done } = await loop.handleMessage({
      sessionId: s.session_id,
      tenantId: "demo-tenant",
      confirm: { confirmationId: cid, approved: true },
      contextPacket: PACKET,
    });
    await done;

    const events2 = await store.eventsAfter(s.session_id, lastSeq);
    expect(ofType(events2, "confirmation_resolved")[0]!.data).toMatchObject({
      confirmation_id: cid,
      approved: true,
      by: "demo-tenant",
    });

    // The re-invoked run_capability carried the confirmation_id to the gate (as
    // the write-rung catalog action)...
    const confirmedCheck = gate.checks.find(
      (c) => c.action === "run_write_tool" && c.args.confirmation_id === cid,
    );
    expect(confirmedCheck?.decision).toBe("allow");

    // ...and dispatch happened (write: async row, no inline wait).
    expect(appRun.submitCalls).toHaveLength(1);
    expect(appRun.submitCalls[0]).toMatchObject({
      tool: "add-panel",
      dwg: "rooftop_demo",
      catalogDigest: expect.stringMatching(/^sha256:/),
      drawingVersion: 3,
      expectedDrawingHead: 3,
      catalogCommit: "a".repeat(40),
      effectiveCatalogDigest: "b".repeat(64),
      toolManifestSha256: `sha256:${"3".repeat(64)}`,
      wait: false,
    });
    expect(ofType(events2, "job_linked")).toHaveLength(1);
    expect(ofType(events2, "turn_complete")[0]!.data).toEqual({ stop_reason: "end_turn" });

    // The resume turn resumed the SDK session captured on turn 1.
    expect(runner.runs[1]!.resumeSdkSessionId).toBe("fake-sdk-session-1");
    expect(runner.runs[1]!.userMessage).toContain(`CONFIRMATION ${cid} APPROVED — dispatch it now.`);
  });

  it("rejects an approved write when the drawing head changed", async () => {
    const { loop, appRun, gate, store } = makeLoop();
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    await sendText(loop, s, "RUN:add-panel");
    const cid = String(
      ofType(await store.eventsAfter(s.session_id, 0), "proposed_run")[0]!.data.confirmation_id,
    );

    gate.grant(cid);
    appRun.drawingHead = 4;
    const { done } = await loop.handleMessage({
      sessionId: s.session_id,
      tenantId: s.tenant_id,
      confirm: { confirmationId: cid, approved: true },
      contextPacket: PACKET,
    });
    await done;

    expect(appRun.submitCalls).toHaveLength(0);
    expect(gate.checks).toContainEqual(expect.objectContaining({
      action: "run_write_tool",
      args: expect.objectContaining({
        confirmation_id: cid,
        drawing_version: 4,
        expected_drawing_head: 4,
      }),
      decision: "deny",
    }));
  });

  it("a denied confirm message resumes without dispatching", async () => {
    const { loop, appRun, store } = makeLoop();
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    await sendText(loop, s, "RUN:add-panel");
    const cid = String(
      ofType(await store.eventsAfter(s.session_id, 0), "proposed_run")[0]!.data.confirmation_id,
    );

    const { done } = await loop.handleMessage({
      sessionId: s.session_id,
      tenantId: "demo-tenant",
      confirm: { confirmationId: cid, approved: false },
      contextPacket: PACKET,
    });
    await done;

    expect(appRun.submitCalls).toHaveLength(0);
    const events = await store.eventsAfter(s.session_id, 0);
    const resolved = ofType(events, "confirmation_resolved");
    expect(resolved[0]!.data).toMatchObject({ confirmation_id: cid, approved: false });
    expect((await store.getConfirmation(cid))!.status).toBe("denied");
  });

  it("a valid confirmation posted from ANOTHER session (another tenant) answers missing and is NOT consumed", async () => {
    const { loop, appRun, store } = makeLoop();
    const sA = await loop.createOrGetSession("tenant-a", "rooftop_demo");
    await sendText(loop, sA, "RUN:add-panel");
    const cid = String(
      ofType(await store.eventsAfter(sA.session_id, 0), "proposed_run")[0]!.data.confirmation_id,
    );

    // Tenant B replays tenant A's pending confirmation via B's OWN session: the
    // answer is exactly like a nonexistent record (no cross-session oracle).
    const sB = await loop.createOrGetSession("tenant-b", "other_dwg");
    await expect(
      loop.handleMessage({
        sessionId: sB.session_id,
        tenantId: "tenant-b",
        confirm: { confirmationId: cid, approved: true },
        contextPacket: PACKET,
      }),
    ).rejects.toMatchObject({ name: "ConfirmationInvalidError", reason: "missing" });

    // The foreign attempt neither consumed the approval nor dispatched anything.
    expect((await store.getConfirmation(cid))!.status).toBe("pending");
    expect(appRun.submitCalls).toHaveLength(0);
    expect(await store.getActiveTurn(sB.session_id)).toBeNull();
  });

  it("a confirm for an unknown confirmation is rejected before burning a turn", async () => {
    const { loop, store } = makeLoop();
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    await expect(
      loop.handleMessage({
        sessionId: s.session_id,
        tenantId: "demo-tenant",
        confirm: { confirmationId: "nope", approved: true },
        contextPacket: PACKET,
      }),
    ).rejects.toMatchObject({ name: "ConfirmationInvalidError", reason: "missing" });
    expect(await store.getActiveTurn(s.session_id)).toBeNull();
  });

  it("a confirm past the TTL is rejected as expired, marked expired, and dispatches NOTHING", async () => {
    const { loop, appRun, store } = makeLoop();
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    await sendText(loop, s, "RUN:add-panel");
    const cid = String(
      ofType(await store.eventsAfter(s.session_id, 0), "proposed_run")[0]!.data.confirmation_id,
    );

    // Backdate the stored record past its TTL (wire contract section 7: 300s).
    const rec = store.confirmations.get(cid)!;
    store.confirmations.set(cid, {
      ...rec,
      expires_at: new Date(Date.now() - 1000).toISOString(),
    });

    await expect(
      loop.handleMessage({
        sessionId: s.session_id,
        tenantId: "demo-tenant",
        confirm: { confirmationId: cid, approved: true },
        contextPacket: PACKET,
      }),
    ).rejects.toMatchObject({ name: "ConfirmationInvalidError", reason: "expired" });

    expect((await store.getConfirmation(cid))!.status).toBe("expired");
    expect(appRun.submitCalls).toHaveLength(0);
    expect(await store.getActiveTurn(s.session_id)).toBeNull();
  });

  it("a second confirm for an already-decided record is rejected as already_decided", async () => {
    const { loop, store } = makeLoop();
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    await sendText(loop, s, "RUN:add-panel");
    const cid = String(
      ofType(await store.eventsAfter(s.session_id, 0), "proposed_run")[0]!.data.confirmation_id,
    );

    const first = await loop.handleMessage({
      sessionId: s.session_id,
      tenantId: "demo-tenant",
      confirm: { confirmationId: cid, approved: false },
      contextPacket: PACKET,
    });
    await first.done;
    expect((await store.getConfirmation(cid))!.status).toBe("denied");

    await expect(
      loop.handleMessage({
        sessionId: s.session_id,
        tenantId: "demo-tenant",
        confirm: { confirmationId: cid, approved: false },
        contextPacket: PACKET,
      }),
    ).rejects.toMatchObject({ name: "ConfirmationInvalidError", reason: "already_decided" });
    expect(await store.getActiveTurn(s.session_id)).toBeNull();
  });

  it("fails closed when confirmation resolution returns the opposite decision", async () => {
    const { loop, appRun, store } = makeLoop();
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    await sendText(loop, s, "RUN:add-panel");
    const cid = String(
      ofType(await store.eventsAfter(s.session_id, 0), "proposed_run")[0]!.data.confirmation_id,
    );

    const resolveConfirmation = store.resolveConfirmation.bind(store);
    store.resolveConfirmation = async (confirmationId, approved, resolvedBy) =>
      resolveConfirmation(confirmationId, !approved, resolvedBy);

    await expect(
      loop.handleMessage({
        sessionId: s.session_id,
        tenantId: "demo-tenant",
        confirm: { confirmationId: cid, approved: true },
        contextPacket: PACKET,
      }),
    ).rejects.toMatchObject({ name: "ConfirmationInvalidError", reason: "already_decided" });

    expect((await store.getConfirmation(cid))!.status).toBe("denied");
    expect(appRun.submitCalls).toHaveLength(0);
    expect(await store.getActiveTurn(s.session_id)).toBeNull();
  });
});

describe("ConverseLoop — live-APS split turns (submit_live_solve, R4)", () => {
  it("run_capability on an aps_live tool consults submit_live_solve — never a run_* rung — and dispatches only after approval", async () => {
    const { loop, appRun, gate, store } = makeLoop();
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    await sendText(loop, s, "RUN:solve-live");

    // NOTHING dispatched; the consult rode the R4 real-USD action.
    expect(appRun.submitCalls).toHaveLength(0);
    expect(gate.checks.map((c) => c.action)).toEqual(["submit_live_solve"]);
    expect(gate.checks[0]!.decision).toBe("awaiting_approval");

    const cid = String(
      ofType(await store.eventsAfter(s.session_id, 0), "proposed_run")[0]!.data.confirmation_id,
    );
    gate.grant(cid);
    const { done } = await loop.handleMessage({
      sessionId: s.session_id,
      tenantId: "demo-tenant",
      confirm: { confirmationId: cid, approved: true },
      contextPacket: PACKET,
    });
    await done;

    // The confirmed re-invocation stayed on the live rung and dispatched async.
    const confirmed = gate.checks.find(
      (c) => c.action === "submit_live_solve" && c.args.confirmation_id === cid,
    );
    expect(confirmed?.decision).toBe("allow");
    expect(appRun.submitCalls).toHaveLength(1);
    expect(appRun.submitCalls[0]).toMatchObject({ tool: "solve-live", dwg: "rooftop_demo", wait: false });
  });
});

describe("ConverseLoop — gate action vocabulary (app policy catalog)", () => {
  it("consults the gate with rung actions + schema-shaped args, never raw spine tool names", async () => {
    const { loop, gate } = makeLoop();
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    await sendText(loop, s, "SEARCH:layers");
    await sendText(loop, s, "STATE:versions");
    await sendText(loop, s, "JOB:job-1");
    await sendText(loop, s, "RUN:count-by-layer");
    await sendText(loop, s, "RUN:add-panel");

    // server/agent_policy.json defines ONLY these actions; a spine tool name on
    // the wire would come back deny "unknown_action".
    expect(gate.checks.map((c) => c.action)).toEqual([
      "read_platform_state",
      "read_platform_state",
      "read_platform_state",
      "run_read_tool",
      "run_write_tool",
    ]);
    // read_platform_state args honor its {what} args_schema (additionalProperties: false).
    expect(gate.checks[0]!.args).toEqual({ what: "capabilities" });
    expect(gate.checks[1]!.args).toEqual({ what: "versions" });
    expect(gate.checks[2]!.args).toEqual({ what: "jobs" });
    // run_* args include the exact catalog and drawing target pins.
    expect(gate.checks[3]!.args).toEqual({
      tool: "count-by-layer",
      params: {},
      dwg: "rooftop_demo",
      catalog_digest: `sha256:${"1".repeat(64)}`,
    });
  });
});

describe("ConverseLoop — gate deny + policy surface", () => {
  it("a gate deny becomes an isError tool result the model relays; the turn still ends cleanly", async () => {
    const { loop, appRun, gate, store } = makeLoop();
    gate.deny("count-by-layer", "tenant is rate limited");
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    await sendText(loop, s, "RUN:count-by-layer");

    expect(appRun.submitCalls).toHaveLength(0);
    const events = await store.eventsAfter(s.session_id, 0);
    const toolResult = ofType(events, "tool_result")[0]!;
    expect(toolResult.data).toMatchObject({ tool: "run_capability", ok: false });
    expect(String(toolResult.data.summary)).toContain("denied: tenant is rate limited");
    // The model RELAYED the deny as text (not a crash, not an error turn).
    const text = ofType(events, "text_delta").map((e) => String(e.data.text)).join("");
    expect(text).toContain("tenant is rate limited");
    expect(ofType(events, "turn_complete")[0]!.data).toEqual({ stop_reason: "end_turn" });
  });

  it("non-spine tools are denied by canUseTool before any gate/executor contact", async () => {
    const { loop, gate, store } = makeLoop();
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    await sendText(loop, s, "FORBIDDEN_TOOL please");
    expect(gate.checks).toHaveLength(0);
    const events = await store.eventsAfter(s.session_id, 0);
    expect(ofType(events, "tool_call")).toHaveLength(0);
    const text = ofType(events, "text_delta").map((e) => String(e.data.text)).join("");
    expect(text).toContain("not permitted");
  });
});

describe("ConverseLoop — remaining spine tools", () => {
  it("catalog_search returns no match for SQL-shaped nonsense", async () => {
    const { loop, store } = makeLoop();
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    await sendText(loop, s, "SEARCH:DROP_TABLE_drawings");
    const events = await store.eventsAfter(s.session_id, 0);
    expect(ofType(events, "tool_result")[0]!.data).toMatchObject({
      tool: "catalog_search",
      ok: true,
      summary: "0 matches",
    });
  });

  it("author_tool executes through the app back-edge after approval", async () => {
    const { loop, appRun, gate, store } = makeLoop();
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    await sendText(loop, s, "AUTHOR:panel-gap-checker");
    const pending = await store.eventsAfter(s.session_id, 0);
    const cid = String(ofType(pending, "confirmation_required")[0]!.data.confirmation_id);
    expect(appRun.authorCalls).toHaveLength(0);

    gate.grant(cid);
    const { done } = await loop.handleMessage({
      sessionId: s.session_id,
      tenantId: s.tenant_id,
      confirm: { confirmationId: cid, approved: true },
      contextPacket: PACKET,
    });
    await done;

    const idempotencyKey = `author:${createHash("sha256")
      .update(JSON.stringify({
        action: "author_tool",
        tenant_id: "demo-tenant",
        session_id: s.session_id,
        description: "panel-gap-checker",
      }))
      .digest("hex")}`;
    expect(appRun.authorCalls).toEqual([
      { tenantId: "demo-tenant", description: "panel-gap-checker", idempotencyKey },
    ]);
    expect(gate.checks).toContainEqual(expect.objectContaining({
      action: "author_tool",
      args: expect.objectContaining({ confirmation_id: cid }),
      decision: "allow",
    }));
  });

  it("request_publication sends only the durable change-set id", async () => {
    const { loop, appRun, gate, store } = makeLoop();
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    const changeSetId = "7f3a51f0-9d9a-43be-8d29-cbdba31249c8";

    await sendText(loop, s, `PUBLISH:${changeSetId}`);

    expect(appRun.publicationCalls).toEqual([
      { tenantId: "demo-tenant", changeSetId },
    ]);
    expect(gate.checks).toContainEqual(expect.objectContaining({
      action: "request_publication",
      args: { change_set_id: changeSetId },
      decision: "allow",
    }));
    const result = ofType(await store.eventsAfter(s.session_id, 0), "tool_result")[0]!;
    expect(JSON.stringify(result.data)).not.toContain("confirmation_id");
  });

  it("request_confirmation emits the chip carrying the id the GATE minted", async () => {
    const { loop, gate, store } = makeLoop();
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    gateAlwaysConfirms(gate, APP_MINTED_CID);
    await sendText(loop, s, "CONFIRM_REQ:deploy");

    const events = await store.eventsAfter(s.session_id, 0);
    const required = ofType(events, "confirmation_required");
    expect(required).toHaveLength(1);
    expect(required[0]!.data).toMatchObject({
      confirmation_id: APP_MINTED_CID,
      kind: "request_confirmation",
      payload: { kind: "deploy" },
    });
    // The mirror row carries the APP's id — nothing was minted locally.
    expect([...store.confirmations.keys()]).toEqual([APP_MINTED_CID]);
    expect(ofType(events, "tool_result")[0]!.data).toMatchObject({
      tool: "request_confirmation",
      ok: true,
      summary: `pending (confirmation ${APP_MINTED_CID})`,
    });
    // The model was told to end the turn on a pending result (section 5).
    expect(ofType(events, "turn_complete")[0]!.data).toEqual({ stop_reason: "awaiting_approval" });
  });

  it("request_confirmation on an ALLOW verdict (policy misconfigured) mints nothing", async () => {
    const { loop, store } = makeLoop();
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    // The stock fake still allows rung-0 actions: an allow means the app minted
    // NO pending record, so a chip here would 404 on approval.
    await sendText(loop, s, "CONFIRM_REQ:deploy");

    const events = await store.eventsAfter(s.session_id, 0);
    expect(ofType(events, "confirmation_required")).toHaveLength(0);
    expect(store.confirmations.size).toBe(0);
    expect(ofType(events, "tool_result")[0]!.data).toMatchObject({
      tool: "request_confirmation",
      ok: true,
    });
    expect(ofType(events, "turn_complete")[0]!.data).toEqual({ stop_reason: "end_turn" });
  });

  it("a terminal LLM quota failure maps to the error event + stop_reason", async () => {
    const { loop, store } = makeLoop();
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    await sendText(loop, s, "FAIL:llm_quota_exhausted");
    const events = await store.eventsAfter(s.session_id, 0);
    const error = ofType(events, "error")[0]!;
    expect(error.data).toMatchObject({ degraded_mode: true });
    expect((error.data.error as Record<string, unknown>).error_code).toBe("llm_quota_exhausted");
    expect(ofType(events, "turn_complete")[0]!.data).toEqual({ stop_reason: "llm_quota_exhausted" });
  });
});

describe("ConverseLoop — approval ids are APP truth (wire contract section 6)", () => {
  it("every confirmation_id on the wire is one the gate returned — none is minted locally", async () => {
    const { loop, gate, store } = makeLoop();
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");

    // request_confirmation is always-confirm too, so BOTH chip shapes are covered.
    gateAlwaysConfirms(gate, APP_MINTED_CID);

    // Every id the gate handed back, across the whole scenario.
    const minted = new Set<string>();
    const origCheck = gate.check.bind(gate);
    gate.check = async (action: string, args: Record<string, unknown>, ctx: GateCheckContext) => {
      const verdict = await origCheck(action, args, ctx);
      if (verdict.confirmation_id) minted.add(verdict.confirmation_id);
      return verdict;
    };

    await sendText(loop, s, "RUN:add-panel");
    await sendText(loop, s, "CONFIRM_REQ:deploy");

    const events = await store.eventsAfter(s.session_id, 0);
    const onWire = events
      .map((e) => e.data.confirmation_id)
      .filter((id): id is string => typeof id === "string");
    expect(onWire.length).toBeGreaterThan(0);
    for (const id of onWire) expect(minted.has(id)).toBe(true);
    // The mirror rows are the same ids — no local minting behind the stream either.
    for (const id of store.confirmations.keys()) expect(minted.has(id)).toBe(true);
  });

  it("awaiting_approval WITHOUT a confirmation_id is malformed: denied, nothing proposed", async () => {
    const { loop, gate, appRun, store } = makeLoop();
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");

    // A gate that says "approval needed" but mints no record: the app would 404
    // the approval, so the harness must never render a chip for it.
    const origCheck = gate.check.bind(gate);
    gate.check = async (action: string, args: Record<string, unknown>, ctx: GateCheckContext) => {
      const verdict = await origCheck(action, args, ctx);
      return verdict.decision === "awaiting_approval"
        ? ({ decision: "awaiting_approval", reason: verdict.reason } as GateCheckResult)
        : verdict;
    };

    await sendText(loop, s, "RUN:add-panel");

    const events = await store.eventsAfter(s.session_id, 0);
    expect(ofType(events, "proposed_run")).toHaveLength(0);
    expect(ofType(events, "confirmation_required")).toHaveLength(0);
    expect(store.confirmations.size).toBe(0);
    expect(appRun.submitCalls).toHaveLength(0);
    const toolResult = ofType(events, "tool_result")[0]!;
    expect(toolResult.data).toMatchObject({ tool: "run_capability", ok: false });
    expect(String(toolResult.data.summary)).toContain(
      "gate_awaiting_approval_without_confirmation_id",
    );
    expect(ofType(events, "turn_complete")[0]!.data).toEqual({ stop_reason: "end_turn" });
  });
});

describe("FakeGateClient — approval records are bound and single-use", () => {
  const CTX: GateCheckContext = { tenantId: "demo-tenant", sessionId: "sess-1", turnId: "turn-1" };
  const ARGS = { tool: "add-panel", params: { row: 2 } };

  async function mint(gate: FakeGateClient): Promise<string> {
    const verdict = await gate.check("run_write_tool", ARGS, CTX);
    expect(verdict.decision).toBe("awaiting_approval");
    return verdict.confirmation_id!;
  }

  it("a granted approval allows exactly once, then reads as consumed", async () => {
    const gate = new FakeGateClient();
    const cid = await mint(gate);
    gate.grant(cid);
    const args = { ...ARGS, confirmation_id: cid };
    // A different turn of the same session still carries the approval.
    expect((await gate.check("run_write_tool", args, { ...CTX, turnId: "turn-2" })).decision).toBe("allow");
    expect(await gate.check("run_write_tool", args, { ...CTX, turnId: "turn-3" })).toMatchObject({
      decision: "deny",
      reason: "confirmation_already_consumed",
    });
  });

  it("drifted args are denied args_mismatch even though the id is granted", async () => {
    const gate = new FakeGateClient();
    const cid = await mint(gate);
    gate.grant(cid);
    const drifted = { tool: "add-panel", params: { row: 3 }, confirmation_id: cid };
    expect(await gate.check("run_write_tool", drifted, CTX)).toMatchObject({
      decision: "deny",
      reason: "args_mismatch",
    });
  });

  it("another tenant's session cannot spend the approval", async () => {
    const gate = new FakeGateClient();
    const cid = await mint(gate);
    gate.grant(cid);
    const foreign: GateCheckContext = { tenantId: "tenant-b", sessionId: "sess-2", turnId: "t" };
    expect(
      await gate.check("run_write_tool", { ...ARGS, confirmation_id: cid }, foreign),
    ).toMatchObject({ decision: "deny", reason: "confirmation_not_bound_to_session" });
  });

  it("an ungranted or unknown id never allows, and the rung cannot be swapped", async () => {
    const gate = new FakeGateClient();
    const cid = await mint(gate);
    expect(await gate.check("run_write_tool", { ...ARGS, confirmation_id: cid }, CTX)).toMatchObject({
      decision: "deny",
      reason: "confirmation_not_approved",
    });
    gate.grant(cid);
    // Approved for the R3 write — not for the R4 real-USD action.
    expect(
      await gate.check("submit_live_solve", { ...ARGS, confirmation_id: cid }, CTX),
    ).toMatchObject({ decision: "deny", reason: "action_mismatch" });
    expect(
      await gate.check("run_write_tool", { ...ARGS, confirmation_id: "made-up" }, CTX),
    ).toMatchObject({ decision: "deny", reason: "unknown_confirmation" });
    expect(() => gate.grant("made-up")).toThrow();
  });
});

describe("ConverseLoop — finalization crash-safety", () => {
  it("a store append failure during finalization still releases the turn lock", async () => {
    const { loop, store } = makeLoop();
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");

    // Disk-error drill: the terminal transcript append throws.
    const origAppend = store.appendEvent.bind(store);
    store.appendEvent = async (sessionId, turnId, type, data) => {
      if (type === "turn_complete") throw new Error("disk full");
      return origAppend(sessionId, turnId, type, data);
    };

    const { done } = await loop.handleMessage({
      sessionId: s.session_id,
      tenantId: "demo-tenant",
      text: "hello",
      contextPacket: PACKET,
    });
    await done;

    // No eternal 409: the turns row is closed and the session is idle again...
    expect(await store.getActiveTurn(s.session_id)).toBeNull();
    expect((await store.getSession(s.session_id))?.status).toBe("idle");

    // ...and the next message starts a fresh turn normally.
    store.appendEvent = origAppend;
    await sendText(loop, s, "hello again");
    const events = await store.eventsAfter(s.session_id, 0);
    expect(ofType(events, "turn_complete")).toHaveLength(1);
  });
});
