/**
 * FakeTurnRunner — deterministic scripted ConverseRunner (H3, hermetic; NO
 * network, NO Anthropic auth). Asserts the exact event-type sequence + key
 * data fields per routing branch, described in fakeTurnRunner.ts's header.
 */

import { describe, expect, it } from "vitest";

import { FakeTurnRunner } from "../src/ports/fakes/fakeTurnRunner.js";
import type { ConverseTurnInput, HarnessTurnEvent } from "../src/ports/converse.js";

function mkInput(overrides: Partial<ConverseTurnInput> = {}): ConverseTurnInput {
  return {
    tenant_id: "demo-tenant",
    session_id: "sess-1",
    turn_id: "turn-1",
    drawing_id: "demo",
    messages: [],
    ...overrides,
  };
}

async function collect(runner: FakeTurnRunner, input: ConverseTurnInput): Promise<HarnessTurnEvent[]> {
  const out: HarnessTurnEvent[] = [];
  for await (const ev of runner.runTurn(input)) out.push(ev);
  return out;
}

function types(events: HarnessTurnEvent[]): string[] {
  return events.map((e) => e.type);
}

describe("FakeTurnRunner", () => {
  it("default flow: 3x text_delta + turn_usage + turn_complete{end_turn}", async () => {
    const runner = new FakeTurnRunner();
    const started = Date.now();
    const events = await collect(runner, mkInput({ text: "hello there" }));
    const elapsed = Date.now() - started;

    expect(types(events)).toEqual(["text_delta", "text_delta", "text_delta", "turn_usage", "turn_complete"]);
    expect(events.every((e) => e.type !== "text_delta" || typeof e.data.text === "string")).toBe(true);
    const last = events[events.length - 1];
    expect(last.data.stop_reason).toBe("end_turn");
    // 4 inter-event delays of ~10ms each -> observable streaming, not one buffered burst.
    expect(elapsed).toBeGreaterThanOrEqual(30);
    expect(runner.calls).toBe(1);
  });

  it("'count' flow: narration + tool_call/tool_result/job_linked before turn_usage/turn_complete", async () => {
    const runner = new FakeTurnRunner();
    const events = await collect(runner, mkInput({ turn_id: "turn-count", text: "please COUNT the walls" }));

    expect(types(events)).toEqual([
      "text_delta",
      "text_delta",
      "text_delta",
      "tool_call",
      "tool_result",
      "job_linked",
      "turn_usage",
      "turn_complete",
    ]);

    const byType = Object.fromEntries(events.map((e) => [e.type, e])) as Record<string, HarnessTurnEvent>;
    expect(byType.tool_call.data.tool).toBe(byType.tool_result.data.tool);
    expect(byType.tool_result.data.ok).toBe(true);
    expect(byType.job_linked.data.tool).toBe(byType.tool_call.data.tool);
    expect(byType.job_linked.data.job_id).toBe("job-turn-count");
    expect(byType.turn_complete.data.stop_reason).toBe("end_turn");
  });

  it("'approve' flow: proposed_run + confirmation_required + turn_complete{awaiting_approval}", async () => {
    const runner = new FakeTurnRunner();
    const events = await collect(runner, mkInput({ turn_id: "turn-42", text: "please approve this run" }));

    expect(types(events)).toEqual(["proposed_run", "confirmation_required", "turn_complete"]);

    const [proposed, required, complete] = events;
    const confirmationId = proposed.data.confirmation_id;
    expect(typeof confirmationId).toBe("string");
    expect(proposed.data.dwg).toBe("demo");
    expect(required.data.confirmation_id).toBe(confirmationId);
    expect(complete.data.stop_reason).toBe("awaiting_approval");
  });

  it("confirmation_id is stably derivable from turn_id (same turn_id -> same id, across instances)", async () => {
    const events1 = await collect(new FakeTurnRunner(), mkInput({ turn_id: "turn-stable", text: "approve it" }));
    const events2 = await collect(new FakeTurnRunner(), mkInput({ turn_id: "turn-stable", text: "approve it" }));
    expect(events1[0].data.confirmation_id).toBe(events2[0].data.confirmation_id);

    const eventsOther = await collect(new FakeTurnRunner(), mkInput({ turn_id: "turn-different", text: "approve it" }));
    expect(eventsOther[0].data.confirmation_id).not.toBe(events1[0].data.confirmation_id);
  });

  it("confirm input (approved): tool_result + text_delta + turn_complete{end_turn}", async () => {
    const runner = new FakeTurnRunner();
    const events = await collect(
      runner,
      mkInput({
        confirm: {
          confirmation_id: "confirm-turn-1",
          approved: true,
          proposal: { tool: "count_by_layer", params: {}, capability: "drawing.write" },
        },
      }),
    );

    expect(types(events)).toEqual(["tool_result", "text_delta", "turn_complete"]);
    expect(events[0].data.ok).toBe(true);
    expect(events[0].data.tool).toBe("count_by_layer");
    expect(events[2].data.stop_reason).toBe("end_turn");
  });

  it("confirm input (rejected): still tool_result + text_delta + turn_complete{end_turn}, but ok:false", async () => {
    const runner = new FakeTurnRunner();
    const events = await collect(
      runner,
      mkInput({
        confirm: {
          confirmation_id: "confirm-turn-1",
          approved: false,
          proposal: { tool: "count_by_layer", params: {} },
        },
      }),
    );

    expect(types(events)).toEqual(["tool_result", "text_delta", "turn_complete"]);
    expect(events[0].data.ok).toBe(false);
    expect(events[2].data.stop_reason).toBe("end_turn");
  });

  it("confirm takes precedence over any text keyword routing", async () => {
    const runner = new FakeTurnRunner();
    const events = await collect(
      runner,
      mkInput({
        text: "quota approve count ratelimit", // every keyword present
        confirm: {
          confirmation_id: "confirm-turn-1",
          approved: true,
          proposal: { tool: "count_by_layer", params: {} },
        },
      }),
    );
    expect(types(events)).toEqual(["tool_result", "text_delta", "turn_complete"]);
  });

  it("'quota' flow: error{llm_quota_exhausted} + turn_complete{llm_quota_exhausted}", async () => {
    const runner = new FakeTurnRunner();
    const events = await collect(runner, mkInput({ text: "I think we hit a quota issue" }));

    expect(types(events)).toEqual(["error", "turn_complete"]);
    expect((events[0].data.error as Record<string, unknown>).error_code).toBe("llm_quota_exhausted");
    expect(events[1].data.stop_reason).toBe("llm_quota_exhausted");
  });

  it("'ratelimit' flow: error{llm_rate_limited} + turn_complete{llm_rate_limited}", async () => {
    const runner = new FakeTurnRunner();
    const events = await collect(runner, mkInput({ text: "hitting a ratelimit right now" }));

    expect(types(events)).toEqual(["error", "turn_complete"]);
    expect((events[0].data.error as Record<string, unknown>).error_code).toBe("llm_rate_limited");
    expect(events[1].data.stop_reason).toBe("llm_rate_limited");
  });

  it("routing precedence: quota beats ratelimit/approve/count when several keywords are present", async () => {
    const runner = new FakeTurnRunner();
    const events = await collect(runner, mkInput({ text: "quota ratelimit approve count" }));
    expect(types(events)).toEqual(["error", "turn_complete"]);
    expect((events[0].data.error as Record<string, unknown>).error_code).toBe("llm_quota_exhausted");
  });

  it("routing is case-insensitive on text keywords", async () => {
    const runner = new FakeTurnRunner();
    const events = await collect(runner, mkInput({ text: "Please APPROVE this" }));
    expect(types(events)).toEqual(["proposed_run", "confirmation_required", "turn_complete"]);
  });

  it("calls counter increments once per runTurn() invocation", async () => {
    const runner = new FakeTurnRunner();
    await collect(runner, mkInput({ text: "hi" }));
    await collect(runner, mkInput({ text: "hi again" }));
    expect(runner.calls).toBe(2);
  });
});
