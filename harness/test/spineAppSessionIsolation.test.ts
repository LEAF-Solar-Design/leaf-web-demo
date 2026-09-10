import { expect, it } from "vitest";
import { SpineTurnAdapter } from "../src/agent/spineTurnAdapter.js";
import { FakeAppRunClient } from "../src/ports/fakes/fakeAppRunClient.js";
import { FakeConverseRunner } from "../src/ports/fakes/fakeConverseRunner.js";
import { FakeGateClient } from "../src/ports/fakes/fakeGateClient.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { FakeSessionStore } from "../src/ports/fakes/fakeSessionStore.js";

it("clears only the current app mapping when its SDK transcript is lost", async () => {
  const store = new FakeSessionStore();
  await store.setAppSdkSession("demo-tenant", "app-a", "expired-sdk");
  await store.setAppSdkSession("demo-tenant", "app-b", "other-sdk");
  const adapter = new SpineTurnAdapter({ oauth: new FakeOAuthGrantProvider(),
    appRun: new FakeAppRunClient(), gate: new FakeGateClient(), store,
    runnerFor: () => ({ async *run(input) {
      expect(input.resumeSdkSessionId).toBe("expired-sdk");
      yield { type: "done" as const, stopReason: "end_turn" as const,
        sdkSessionId: null, sdkSessionReset: true };
    } }),
  });
  for await (const _event of adapter.runTurn({ tenant_id: "demo-tenant",
    session_id: "app-a", turn_id: "turn-a", drawing_id: "rooftop_demo",
    text: "Continue", messages: [] })) { /* Drain the turn. */ }
  expect(await store.getAppSdkSession("demo-tenant", "app-a")).toBeNull();
  expect(await store.getAppSdkSession("demo-tenant", "app-b")).toBe("other-sdk");
});

it("seeds a new app mapping from that app's history rather than an unowned legacy SDK transcript", async () => {
  const runner = new FakeConverseRunner();
  const store = new FakeSessionStore();
  const legacy = await store.createOrGetSession("demo-tenant", "rooftop_demo");
  await store.updateSession(legacy.session_id, { sdk_session_id: "legacy-drawing-sdk" });
  const appRun = new FakeAppRunClient();
  const adapter = new SpineTurnAdapter({ oauth: new FakeOAuthGrantProvider(), appRun,
    gate: new FakeGateClient(), store, runnerFor: () => runner });
  for await (const _event of adapter.runTurn({
    tenant_id: "demo-tenant", session_id: "recipe-app", turn_id: "recipe-turn",
    drawing_id: "rooftop_demo", text: "STATE:summary",
    messages: [{ role: "user", text: "My recipe project history" }],
  })) { /* Drain the turn. */ }
  expect(runner.runs[0].resumeSdkSessionId).toBeUndefined();
  expect(runner.runs[0].userMessage).toContain("My recipe project history");
  expect(appRun.methodLog).toContain("getDrawingState");
  expect((await store.getSession(legacy.session_id))?.sdk_session_id).toBe("legacy-drawing-sdk");
});

it("isolates app conversations sharing a drawing and preserves each conversation on resume", async () => {
  const runner = new FakeConverseRunner();
  const adapter = new SpineTurnAdapter({
    oauth: new FakeOAuthGrantProvider(),
    appRun: new FakeAppRunClient(),
    gate: new FakeGateClient(),
    store: new FakeSessionStore(),
    runnerFor: () => runner,
  });
  const run = async (session: string, turn: string, text: string) => {
    for await (const _event of adapter.runTurn({
      tenant_id: "demo-tenant", session_id: session, turn_id: turn,
      drawing_id: "rooftop_demo", messages: [], text,
    })) { /* Drain the real adapter's turn. */ }
  };

  await run("drawing-conversation", "turn-1", "Discuss this drawing.");
  await run("recipe-project-conversation", "turn-2", "Author my recipe project validator.");
  await run("drawing-conversation", "turn-3", "Continue discussing the drawing.");
  await run("recipe-project-conversation", "turn-4", "Continue my validator.");

  expect(runner.runs).toHaveLength(4);
  expect(runner.runs[0].resumeSdkSessionId).toBeUndefined();
  expect(runner.runs[1].resumeSdkSessionId).toBeUndefined();
  expect(runner.runs[2].resumeSdkSessionId).toBe("fake-sdk-session-1");
  expect(runner.runs[3].resumeSdkSessionId).toBe("fake-sdk-session-2");
});
