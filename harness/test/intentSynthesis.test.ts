/**
 * Per-turn intent synthesis: the turn prompt carries the classifier's read, and
 * — the part that must never regress — a classifier that is absent, silent, or
 * broken costs the user NOTHING. The prompt in that case is byte-identical to
 * the one the loop built before this feature existed.
 */

import { describe, expect, it } from "vitest";

import { ConverseLoop } from "../src/agent/converseLoop.js";
import { FakeAppRunClient } from "../src/ports/fakes/fakeAppRunClient.js";
import { FakeConverseRunner } from "../src/ports/fakes/fakeConverseRunner.js";
import { FakeGateClient } from "../src/ports/fakes/fakeGateClient.js";
import { FakeIntentSynthesizer } from "../src/ports/fakes/fakeIntentSynthesizer.js";
import { FakeSessionStore } from "../src/ports/fakes/fakeSessionStore.js";
import { parseIntent } from "../src/ports/impl/haikuIntentSynthesizer.js";

const PACKET = { drawing: { id: "rooftop_demo" } };

function makeLoop(intent?: FakeIntentSynthesizer) {
  const runner = new FakeConverseRunner();
  const appRun = new FakeAppRunClient();
  const gate = new FakeGateClient();
  const store = new FakeSessionStore();
  const loop = new ConverseLoop({ runner, appRun, gate, store, intent });
  return { loop, runner, store, intent };
}

async function send(loop: ConverseLoop, s: { session_id: string; tenant_id: string }, text: string) {
  const { done } = await loop.handleMessage({
    sessionId: s.session_id,
    tenantId: s.tenant_id,
    text,
    contextPacket: PACKET,
  });
  await done;
}

describe("turn intent synthesis", () => {
  it("puts the classifier's read in the prompt, marked advisory", async () => {
    const intent = new FakeIntentSynthesizer();
    intent.next = { target: "product", rationale: "asks about the app's appearance" };
    const { loop, runner } = makeLoop(intent);
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");

    await send(loop, s, "change the background to light mode");

    expect(intent.calls).toEqual(["change the background to light mode"]);
    const prompt = runner.runs[0]!.userMessage;
    expect(prompt).toContain("=== INTENT SIGNAL");
    expect(prompt).toContain("target: product");
    expect(prompt).toContain("asks about the app's appearance");
    // It must read as evidence, not as an instruction to obey.
    expect(prompt).toContain("advisory");
    expect(prompt).toContain("trust the message");
    // The signal precedes the message it describes.
    expect(prompt.indexOf("=== INTENT SIGNAL")).toBeLessThan(
      prompt.indexOf("=== USER MESSAGE ==="),
    );
  });

  it("carries an unclear verdict rather than hiding it", async () => {
    const intent = new FakeIntentSynthesizer();
    intent.next = { target: "unclear", rationale: "could be either surface" };
    const { loop, runner } = makeLoop(intent);
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");

    await send(loop, s, "make the background lighter");

    expect(runner.runs[0]!.userMessage).toContain("target: unclear");
  });

  it("is byte-identical to the classifier-free prompt when there is no signal", async () => {
    // Baseline: no port at all — exactly how the loop shipped before this.
    const plain = makeLoop();
    const ps = await plain.loop.createOrGetSession("demo-tenant", "rooftop_demo");
    await send(plain.loop, ps, "change the background");
    const baseline = plain.runner.runs[0]!.userMessage;

    // A port that returns null (no confident read).
    const quiet = new FakeIntentSynthesizer();
    quiet.next = null;
    const withNull = makeLoop(quiet);
    const qs = await withNull.loop.createOrGetSession("demo-tenant", "rooftop_demo");
    await send(withNull.loop, qs, "change the background");

    // A port that THROWS. A classifier outage must not cost the user a turn.
    const broken = new FakeIntentSynthesizer();
    broken.throwWith = new Error("classifier unreachable");
    const withThrow = makeLoop(broken);
    const bs = await withThrow.loop.createOrGetSession("demo-tenant", "rooftop_demo");
    await send(withThrow.loop, bs, "change the background");

    expect(withNull.runner.runs[0]!.userMessage).toBe(baseline);
    expect(withThrow.runner.runs[0]!.userMessage).toBe(baseline);
    expect(baseline).not.toContain("INTENT SIGNAL");
    expect(broken.calls).toHaveLength(1); // it really was consulted, and really failed
  });

  it("does not classify a confirmation turn — the action is already named", async () => {
    const intent = new FakeIntentSynthesizer();
    intent.next = { target: "product", rationale: "n/a" };
    const { loop } = makeLoop(intent);
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    await send(loop, s, "SEARCH:panel");

    const before = intent.calls.length;
    await loop
      .handleMessage({
        sessionId: s.session_id,
        tenantId: s.tenant_id,
        confirm: { confirmationId: "missing-id", approved: true },
        contextPacket: PACKET,
      })
      .catch(() => undefined); // an unknown confirmation is rejected; that is fine here
    expect(intent.calls.length).toBe(before);
  });
});

describe("parseIntent", () => {
  it("accepts a well-formed verdict, with or without surrounding prose", () => {
    expect(parseIntent('{"target":"product","rationale":"the app"}')).toEqual({
      target: "product",
      rationale: "the app",
    });
    expect(
      parseIntent('Sure! {"target":"drawing","rationale":"a layer"} hope that helps'),
    ).toEqual({ target: "drawing", rationale: "a layer" });
  });

  it("returns null rather than inventing a label", () => {
    // An invented verdict would be worse than no verdict at all.
    for (const bad of [
      "",
      "product",
      "{}",
      '{"target":"platform"}',
      '{"target":123}',
      "{not json}",
      '{"rationale":"no target"}',
    ]) {
      expect(parseIntent(bad)).toBeNull();
    }
  });

  it("tolerates a missing rationale and bounds a long one", () => {
    expect(parseIntent('{"target":"unclear"}')).toEqual({ target: "unclear", rationale: "" });
    const long = parseIntent(`{"target":"product","rationale":"${"x".repeat(400)}"}`);
    expect(long?.rationale.length).toBe(120);
  });
});
