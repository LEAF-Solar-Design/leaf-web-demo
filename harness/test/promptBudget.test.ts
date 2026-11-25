/**
 * Small-context runners (a tenant's self-hosted model) declare promptBudgetChars; the loop
 * then keeps only the newest prior_messages that fit. Nov 20 2026 staging: a Claude-era
 * session sent 18,474 tokens to a 16,384-token llama-server and the turn failed.
 */
import { describe, expect, it } from "vitest";

import { ConverseLoop } from "../src/agent/converseLoop.js";
import { FakeAppRunClient } from "../src/ports/fakes/fakeAppRunClient.js";
import { FakeConverseRunner } from "../src/ports/fakes/fakeConverseRunner.js";
import { FakeGateClient } from "../src/ports/fakes/fakeGateClient.js";
import { FakeSessionStore } from "../src/ports/fakes/fakeSessionStore.js";
import { OpenAiConverseRunner } from "../src/ports/impl/openaiConverseRunner.js";

const PACKET = { drawing: { id: "rooftop_demo" } };
const history = Array.from({ length: 200 }, (_, i) => ({
  role: (i % 2 ? "assistant" : "user") as "user" | "assistant",
  text: `message-${String(i).padStart(3, "0")} ` + "x".repeat(200),
}));

async function fallbackFor(budget: number | undefined) {
  const runner = new FakeConverseRunner();
  if (budget !== undefined) Object.assign(runner, { promptBudgetChars: budget });
  const store = new FakeSessionStore();
  const loop = new ConverseLoop({ runner, appRun: new FakeAppRunClient(), gate: new FakeGateClient(), store });
  const s = await loop.createOrGetSession("film-tenant", "rooftop_demo");
  await store.updateSession(s.session_id, { sdk_session_id: "sdk-claude-era" });
  const { done } = await loop.handleMessage({
    sessionId: s.session_id, tenantId: s.tenant_id, text: "count the panels",
    contextPacket: PACKET, priorMessages: history,
  });
  await done;
  return runner.runs[0]!.resumeFallbackUserMessage!;
}

describe("prompt budget for small-context runners", () => {
  it("keeps every prior message when the runner declares no budget", async () => {
    const prompt = await fallbackFor(undefined);
    expect(prompt).toContain("message-000");
    expect(prompt).toContain("message-199");
  });

  it("drops the oldest prior messages to fit, never the newest or the current message", async () => {
    const prompt = await fallbackFor(16_000);
    expect(prompt.length).toBeLessThanOrEqual(16_000);
    expect(prompt).toContain("message-199");
    expect(prompt).not.toContain("message-000");
    expect(prompt).toContain("=== USER MESSAGE ===\ncount the panels");
  });

  it("still sends the current message when even no history exceeds the budget", async () => {
    const prompt = await fallbackFor(10);
    expect(prompt).not.toContain("message-");
    expect(prompt).toContain("count the panels");
  });

  it("the OpenAI-compatible runner exposes its configured budget", () => {
    const grant = { kind: "oauth" as const, oauthToken: "g" };
    expect(new OpenAiConverseRunner({ grant, apiKey: "k", model: "m", promptBudgetChars: 16_000 }).promptBudgetChars)
      .toBe(16_000);
    expect(new OpenAiConverseRunner({ grant, apiKey: "k", model: "m" }).promptBudgetChars).toBeUndefined();
  });
});
