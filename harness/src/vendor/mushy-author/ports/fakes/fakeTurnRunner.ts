/**
 * Fake ConverseRunner — a SCRIPTED, deterministic stand-in for the real Agent
 * SDK turn loop behind `POST /turn` (converse.ts FROZEN contract,
 * leaf-backend-gaps.md §2.1.3 for the per-event `data` shapes). Routes on
 * `input.confirm` / `input.text` with NO network and NO Anthropic auth, so
 * the sessions wire (turn engine -> harness NDJSON stream) can be exercised
 * hermetically end to end before the real Agent SDK runner exists.
 *
 * Routing (first match wins):
 *   1. `input.confirm` present   -> resume-after-approval:
 *        tool_result + text_delta + turn_complete{end_turn}
 *   2. text contains "quota"     -> error{llm_quota_exhausted} + turn_complete{llm_quota_exhausted}
 *   3. text contains "ratelimit" -> error{llm_rate_limited} + turn_complete{llm_rate_limited}
 *   4. text contains "approve"   -> proposed_run + confirmation_required (confirmation_id
 *                                    stably derived from turn_id) + turn_complete{awaiting_approval}
 *   5. text contains "count"     -> the default narration PLUS tool_call/tool_result/job_linked
 *                                    inserted before turn_usage/turn_complete{end_turn}
 *   6. default                   -> 3x text_delta + turn_usage + turn_complete{end_turn}
 *
 * A short (~10ms) delay precedes every yielded event so a real HTTP/NDJSON
 * consumer (the turn engine draining `POST /turn`) observes genuine
 * streaming rather than one buffered burst — mirrors the intent of
 * FakeAgentRunner (design-time author fake) for the converse lane.
 */

import type {
  ConverseRunner,
  ConverseTurnInput,
  HarnessTurnEvent,
} from "../converse.js";

/** Delay between scripted events (ms) — small enough for fast tests, large enough to observe. */
const STEP_DELAY_MS = 10;

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Stable, reproducible confirmation id derived from the turn id (no randomness). */
function confirmationIdFor(turnId: string): string {
  return `confirm-${turnId}`;
}

/** The canned narration shared by the default and count flows. */
const NARRATION = [
  "Looking at the drawing now.",
  "I don't see anything that needs your attention here.",
  "Let me know if you'd like me to check something specific.",
] as const;

/** The one tool the scripted "count" / approval flows pretend to run. */
const SCRIPTED_TOOL = "count_by_layer";

export class FakeTurnRunner implements ConverseRunner {
  /** Number of times runTurn() has been called (observability for tests). */
  calls = 0;

  async *runTurn(input: ConverseTurnInput): AsyncGenerator<HarnessTurnEvent> {
    this.calls += 1;

    if (input.confirm) {
      yield* this.confirmFlow(input);
      return;
    }

    const text = (input.text ?? "").toLowerCase();

    if (text.includes("quota")) {
      yield* this.errorFlow("llm_quota_exhausted", "LLM quota exhausted for this tenant.");
      return;
    }
    if (text.includes("ratelimit")) {
      yield* this.errorFlow("llm_rate_limited", "LLM rate limit hit; retry shortly.");
      return;
    }
    if (text.includes("approve")) {
      yield* this.approveFlow(input);
      return;
    }
    if (text.includes("count")) {
      yield* this.narrationFlow(input, { withTool: true });
      return;
    }
    yield* this.narrationFlow(input, { withTool: false });
  }

  /** Shared narration base for the default + "count" flows. */
  private async *narrationFlow(
    input: ConverseTurnInput,
    opts: { withTool: boolean },
  ): AsyncGenerator<HarnessTurnEvent> {
    for (const text of NARRATION) {
      await delay(STEP_DELAY_MS);
      yield { type: "text_delta", data: { text } };
    }

    if (opts.withTool) {
      await delay(STEP_DELAY_MS);
      yield { type: "tool_call", data: { tool: SCRIPTED_TOOL, args_summary: "layer=(all)" } };

      await delay(STEP_DELAY_MS);
      yield {
        type: "tool_result",
        data: { tool: SCRIPTED_TOOL, ok: true, summary: "12 entities across 4 layers." },
      };

      await delay(STEP_DELAY_MS);
      yield { type: "job_linked", data: { job_id: `job-${input.turn_id}`, tool: SCRIPTED_TOOL } };
    }

    await delay(STEP_DELAY_MS);
    yield { type: "turn_usage", data: { cost_tokens: opts.withTool ? 64 : 42, total_cost_usd: opts.withTool ? 0.0006 : 0.0004 } };

    await delay(STEP_DELAY_MS);
    yield { type: "turn_complete", data: { stop_reason: "end_turn" } };
  }

  /** "approve" flow — halts on a confirmation, never reports usage or completes the run. */
  private async *approveFlow(input: ConverseTurnInput): AsyncGenerator<HarnessTurnEvent> {
    const confirmationId = confirmationIdFor(input.turn_id);
    const params: Record<string, unknown> = {};

    await delay(STEP_DELAY_MS);
    yield {
      type: "proposed_run",
      data: {
        confirmation_id: confirmationId,
        tool: SCRIPTED_TOOL,
        params,
        dwg: input.drawing_id,
        capability: "drawing.write",
        rationale: `Running "${SCRIPTED_TOOL}" needs your approval before it changes drawing state.`,
      },
    };

    await delay(STEP_DELAY_MS);
    yield {
      type: "confirmation_required",
      data: {
        confirmation_id: confirmationId,
        kind: "tool_run",
        payload: { tool: SCRIPTED_TOOL, params },
      },
    };

    await delay(STEP_DELAY_MS);
    yield { type: "turn_complete", data: { stop_reason: "awaiting_approval" } };
  }

  /** Resume-after-approval flow — driven by `input.confirm`, never `input.text`. */
  private async *confirmFlow(input: ConverseTurnInput): AsyncGenerator<HarnessTurnEvent> {
    const confirm = input.confirm;
    if (!confirm) return; // unreachable (guarded by the caller); keeps TS narrowing happy
    const { tool } = confirm.proposal;
    const approved = confirm.approved;

    await delay(STEP_DELAY_MS);
    yield {
      type: "tool_result",
      data: {
        tool,
        ok: approved,
        summary: approved ? `${tool} completed successfully.` : `${tool} was skipped — not approved.`,
      },
    };

    await delay(STEP_DELAY_MS);
    yield {
      type: "text_delta",
      data: {
        text: approved ? `Done — I ran ${tool} as approved.` : `Understood, I won't run ${tool}.`,
      },
    };

    await delay(STEP_DELAY_MS);
    yield { type: "turn_complete", data: { stop_reason: "end_turn" } };
  }

  /** "quota" / "ratelimit" flow — one error event, then a matching non-end_turn completion. */
  private async *errorFlow(
    errorCode: "llm_quota_exhausted" | "llm_rate_limited",
    message: string,
  ): AsyncGenerator<HarnessTurnEvent> {
    await delay(STEP_DELAY_MS);
    yield { type: "error", data: { error: { error_code: errorCode, message } } };

    await delay(STEP_DELAY_MS);
    yield { type: "turn_complete", data: { stop_reason: errorCode } };
  }
}
