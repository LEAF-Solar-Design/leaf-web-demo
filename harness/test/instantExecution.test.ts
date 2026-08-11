import { describe, expect, it } from "vitest";
import { ConverseLoop } from "../src/agent/converseLoop.js";
import { FakeAppRunClient } from "../src/ports/fakes/fakeAppRunClient.js";
import { FakeConverseRunner } from "../src/ports/fakes/fakeConverseRunner.js";
import { FakeGateClient } from "../src/ports/fakes/fakeGateClient.js";
import { FakeInstantExecutorClient } from "../src/ports/fakes/fakeInstantExecutorClient.js";
import { FakeSessionStore } from "../src/ports/fakes/fakeSessionStore.js";
import type { CapabilityEntry, InstantDrawingContext, InstantSessionAssignment, SessionRecord } from "../src/ports/index.js";

const digest = (ch: string) => `sha256:${ch.repeat(64)}`;
const drawing: InstantDrawingContext = {
  drawing_id: "rooftop_demo", version_id: "55555555-5555-4555-8555-555555555555",
  content_digest: digest("e"), geometry_ref: "drawing-context:rooftop-ref-001",
};

function assignment(sessionId: string): InstantSessionAssignment {
  return {
    contract: "leaf.instant-execution/v1", assignment_id: "11111111-1111-4111-8111-111111111111",
    tenant_id: "demo-tenant", session_id: sessionId, executor_id: "executor-local-001",
    executor_endpoint: "http://127.0.0.1:8123", binding_epoch: 1,
    lease_id: "77777777-7777-4777-8777-777777777777", lease_token: "x".repeat(32),
    execution_class: "instant", effective_catalog_digest: digest("d"), code_digest: digest("a"),
    artifact_digest: digest("b"), drawing_context: drawing,
    issued_at: "2026-07-28T16:00:00Z", expires_at: "2099-07-28T16:01:00Z",
  };
}

async function run(loop: ConverseLoop, session: SessionRecord, a?: InstantSessionAssignment, p: Record<string, unknown> = {}) {
  const result = await loop.handleMessage({
    sessionId: session.session_id, tenantId: session.tenant_id, text: `RUN:instant-read PARAMS:${JSON.stringify(p)}`,
    contextPacket: {}, instantAssignment: a, instantDrawingContext: drawing,
    authoritySessionId: "app-session-instant", authorityTurnId: "app-turn-instant",
  });
  await result.done;
}

describe("instant run_capability", () => {
  function setup(entry?: Partial<CapabilityEntry>, withExecutor = true) {
    const appRun = new FakeAppRunClient();
    appRun.catalog = [{
      name: "instant-read", description: "instant", capabilities: ["drawing.read"], catalog_digest: digest("1"),
      execution_class: "instant", effective_catalog_digest: digest("d"), artifact_digest: digest("b"),
      runtime: "python-3.12", limits: { max_wall_ms: 1000 }, ...entry,
    }];
    const instant = new FakeInstantExecutorClient();
    const store = new FakeSessionStore();
    const loop = new ConverseLoop({
      runner: new FakeConverseRunner(), appRun, gate: new FakeGateClient(), store,
      ...(withExecutor ? { instantExecutor: instant } : {}),
    });
    return { appRun, instant, store, loop };
  }

  it("selects the direct executor, not /api/run, for a valid instant assignment", async () => {
    const { loop, appRun, instant } = setup();
    const session = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    await run(loop, session, assignment("app-session-instant"), { layer: "Panels" });
    expect(instant.calls).toHaveLength(1);
    expect(appRun.submitCalls).toHaveLength(0);
    expect(instant.calls[0]!.invocation).toMatchObject({ capability: { capability_id: "drawing.read", tool_id: "instant-read" }, drawing_context: drawing });
  });

  it("fails closed on a mismatched or expired assignment and rejects secret-shaped params", async () => {
    const { loop, appRun, instant } = setup();
    const session = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    const wrong = assignment("other-session");
    await run(loop, session, wrong);
    await run(loop, session, { ...assignment("app-session-instant"), expires_at: "2020-01-01T00:00:00Z" });
    await run(loop, session, assignment("app-session-instant"), { redis_password: "nope" });
    expect(instant.calls).toHaveLength(0);
    expect(appRun.submitCalls).toHaveLength(0);
  });

  it("uses batch only when the catalog explicitly declares a visible fallback", async () => {
    const { loop, appRun, instant, store } = setup({ batch_fallback: true });
    instant.failure = new Error("network down");
    const session = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    await run(loop, session, assignment("app-session-instant"));
    expect(appRun.submitCalls).toHaveLength(1);
    expect(appRun.submitCalls[0]).toMatchObject({
      authoritySessionId: "app-session-instant",
      authorityTurnId: "app-turn-instant",
      drawingVersion: 3,
    });
    const events = await store.eventsAfter(session.session_id, 0);
    expect(events.find((event) => event.type === "job_linked")?.data).toMatchObject({ route: "batch_fallback", reason: "instant_transport_failure" });
  });

  it("uses the declared batch fallback when assignment or executor capacity is unavailable", async () => {
    const missingAssignment = setup({ batch_fallback: true });
    const first = await missingAssignment.loop.createOrGetSession("demo-tenant", "rooftop_demo");
    await run(missingAssignment.loop, first);
    expect(missingAssignment.appRun.submitCalls).toHaveLength(1);
    expect(missingAssignment.appRun.submitCalls[0]).toMatchObject({
      authoritySessionId: "app-session-instant",
      authorityTurnId: "app-turn-instant",
      drawingVersion: 3,
    });

    const disabledExecutor = setup({ batch_fallback: true }, false);
    const second = await disabledExecutor.loop.createOrGetSession("demo-tenant", "rooftop_demo");
    await run(disabledExecutor.loop, second, assignment("app-session-instant"));
    expect(disabledExecutor.appRun.submitCalls).toHaveLength(1);
    expect(disabledExecutor.appRun.submitCalls[0]).toMatchObject({
      authoritySessionId: "app-session-instant",
      authorityTurnId: "app-turn-instant",
      drawingVersion: 3,
    });
  });
});
