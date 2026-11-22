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
 * Scripted plan mode (downstream overlay, leaf-web-demo SDS-PMW): a pre-merge
 * walk of Studio plan mode on the hermetic stack. Checked after the quota and
 * ratelimit routes and before "approve":
 *   P1. plan mode (context_packet.inline_authoring.plan_mode === true, or the
 *       text marker "planwalk" for stacks whose plan rollout flag is off)
 *         -> text_delta + questions_required (one question, two labelled
 *            options) + turn_complete{end_turn}
 *   P2. `answers` present (the reply to P1's card)
 *         -> text_delta + plan_proposed{confirmation_id "plan-<turn_id>", plan,
 *            plan_digest, revision 1} + turn_complete{awaiting_approval}
 *   P3. `confirm` for a "plan-" confirmation -> text_delta + turn_complete{end_turn}
 * Every event shape matches what converseLoop emits for the real plan tools, so
 * no server or web validation is loosened to accept the fake.
 *
 * A short (~10ms) delay precedes every yielded event so a real HTTP/NDJSON
 * consumer (the turn engine draining `POST /turn`) observes genuine
 * streaming rather than one buffered burst — mirrors the intent of
 * FakeAgentRunner (design-time author fake) for the converse lane.
 */

import { createHash } from "node:crypto";

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

/** Text marker that opens the scripted plan flow when the plan rollout flag is off. */
const PLAN_MARKER = "planwalk";

/** Plan confirmation ids carry this prefix so the resume turn can tell them apart. */
const PLAN_CONFIRMATION_PREFIX = "plan-";

/** The tool the scripted plan proposes. */
const PLAN_TOOL = "layer-tally";

/** The one scripted planning question (shape of buildPlan.ts Questions). */
const PLAN_QUESTION = {
  header: "Output",
  question: "How should the layer tally be reported?",
  multiSelect: false,
  options: [
    { label: "Table", description: "One row per layer with its entity count." },
    { label: "Summary", description: "One short sentence per layer." },
  ],
} as const;

/** One answer the way the turn wire carries it (TurnAnswers / BuildPlan.answers). */
type PlanAnswer = { labels: string[] } | { other: string };

/** The additive turn fields the plan flow reads; they predate this vendored port's types. */
type PlanTurnInput = ConverseTurnInput & {
  answers?: { question_id?: unknown; answers?: unknown };
};

/** Plan text must not start or end with whitespace or carry control characters. */
function planText(value: unknown, max: number): string | null {
  if (typeof value !== "string") return null;
  const cleaned = value.replace(/[\x00-\x1f\x7f]/g, " ").trim();
  const bounded = Array.from(cleaned).slice(0, max).join("").trim();
  return bounded ? bounded : null;
}

/** Copy the q1..q4 answers into BuildPlan.answers form, dropping anything malformed. */
function planAnswers(raw: unknown): Record<string, PlanAnswer> {
  const out: Record<string, PlanAnswer> = {};
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return out;
  for (const key of ["q1", "q2", "q3", "q4"]) {
    const answer = (raw as Record<string, unknown>)[key];
    if (!answer || typeof answer !== "object" || Array.isArray(answer)) continue;
    const { labels, other } = answer as { labels?: unknown; other?: unknown };
    if (Array.isArray(labels)) {
      const kept = labels.map((label) => planText(label, 60)).filter((label): label is string => !!label).slice(0, 4);
      if (kept.length) out[key] = { labels: kept };
    } else {
      const text = planText(other, 500);
      if (text) out[key] = { other: text };
    }
  }
  return out;
}

/** Key-sorted JSON, the same canonical form agent/buildPlan.ts planDigest hashes. */
function canonical(value: unknown): string {
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (typeof value === "object" && value !== null) {
    const entries = Object.entries(value).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
    return "{" + entries.map(([key, child]) => JSON.stringify(key) + ":" + canonical(child)).join(",") + "}";
  }
  return JSON.stringify(value);
}

/** The scripted build plan (shape of agent/buildPlan.ts BuildPlan; no numbers, so canonical is exact). */
function scriptedPlan(answers: Record<string, PlanAnswer>): Record<string, unknown> {
  return {
    schema: "leaf.build-plan.v1",
    target: "studio",
    change_kind: "create",
    tool_name: PLAN_TOOL,
    title: "Layer tally",
    summary: "Counts the entities on each layer of the open drawing and reports the totals.",
    behavior: [
      "Read every entity in the open drawing.",
      "Group the entities by layer.",
      "Report one count per layer.",
    ],
    inputs: [
      { name: "layer_filter", type: "string", default: "all", description: "Only count layers whose name contains this text." },
    ],
    output: "A list of layer names with their entity counts.",
    capabilities: ["drawing.read"],
    undo: "Nothing to undo; the tool only reads the drawing.",
    assumptions: ["A drawing is open in Studio."],
    acceptance: ["Every layer that holds entities appears exactly once."],
    answers,
  };
}

/** True when this turn should open the scripted plan flow. */
function planModeRequested(input: ConverseTurnInput, text: string): boolean {
  const inline = input.context_packet?.inline_authoring;
  if (inline && typeof inline === "object" && !Array.isArray(inline)
      && (inline as Record<string, unknown>).plan_mode === true) {
    return true;
  }
  return text.includes(PLAN_MARKER);
}

export class FakeTurnRunner implements ConverseRunner {
  /** Number of times runTurn() has been called (observability for tests). */
  calls = 0;

  async *runTurn(input: ConverseTurnInput): AsyncGenerator<HarnessTurnEvent> {
    this.calls += 1;

    if (input.confirm) {
      if (String(input.confirm.confirmation_id).startsWith(PLAN_CONFIRMATION_PREFIX)) {
        yield* this.planDecisionFlow(input);
        return;
      }
      yield* this.confirmFlow(input);
      return;
    }

    const answers = (input as PlanTurnInput).answers;
    if (answers !== undefined) {
      yield* this.planProposalFlow(input, answers);
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
    if (planModeRequested(input, text)) {
      yield* this.planQuestionsFlow(input);
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

  /** Plan step 1: one planning question card, then the turn ends and waits for answers. */
  private async *planQuestionsFlow(input: ConverseTurnInput): AsyncGenerator<HarnessTurnEvent> {
    await delay(STEP_DELAY_MS);
    yield { type: "text_delta", data: { text: "Before I plan this tool, one question." } };

    await delay(STEP_DELAY_MS);
    // The vendored wire union predates the two plan display events (see spineTurnAdapter).
    yield {
      type: ("questions_required" as string) as HarnessTurnEvent["type"],
      data: {
        question_id: `questions-${input.turn_id}`,
        questions: [{
          id: "q1",
          header: PLAN_QUESTION.header,
          question: PLAN_QUESTION.question,
          multiSelect: PLAN_QUESTION.multiSelect,
          options: PLAN_QUESTION.options.map((option) => ({ ...option })),
        }],
      },
    };

    await delay(STEP_DELAY_MS);
    yield { type: "turn_complete", data: { stop_reason: "end_turn" } };
  }

  /** Plan step 2: the answered card yields a plan awaiting approval. */
  private async *planProposalFlow(
    input: ConverseTurnInput,
    answers: NonNullable<PlanTurnInput["answers"]>,
  ): AsyncGenerator<HarnessTurnEvent> {
    const plan = scriptedPlan(planAnswers(answers.answers));
    await delay(STEP_DELAY_MS);
    yield { type: "text_delta", data: { text: "Here is the plan. Approve it to build the tool." } };

    await delay(STEP_DELAY_MS);
    yield {
      type: ("plan_proposed" as string) as HarnessTurnEvent["type"],
      data: {
        confirmation_id: `${PLAN_CONFIRMATION_PREFIX}${input.turn_id}`,
        plan,
        plan_digest: createHash("sha256").update(canonical(plan)).digest("hex"),
        revision: 1,
      },
    };

    await delay(STEP_DELAY_MS);
    yield { type: "turn_complete", data: { stop_reason: "awaiting_approval" } };
  }

  /** Plan step 3: the approved (or declined) plan resumes and ends the turn. */
  private async *planDecisionFlow(input: ConverseTurnInput): AsyncGenerator<HarnessTurnEvent> {
    const confirm = input.confirm;
    if (!confirm) return; // unreachable (guarded by the caller); keeps TS narrowing happy
    const feedback = planText((confirm as unknown as { feedback?: unknown }).feedback, 200);
    const text = confirm.approved
      ? `Plan approved. Building ${PLAN_TOOL} now.`
      : feedback
        ? `Understood, I will revise the plan: ${feedback}`
        : `Understood, I won't build ${PLAN_TOOL}.`;

    await delay(STEP_DELAY_MS);
    yield { type: "text_delta", data: { text } };

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
