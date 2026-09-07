// Tenant spine constant freeze (contract/OPERATOR.md "tenant surfaces
// unchanged" clause). Lands with the contract-only operator
// PR. The existing converseSdkRunner test pins the MOUNTED list EQUAL TO the
// constant; this file pins the constant itself to literals, so a drift of
// SPINE_TOOL_NAMES can no longer pass both tests tautologically. The event
// and stop-reason unions are type-level; their pins below are compile-time
// and enforced by `npm run typecheck` (tsc --noEmit covers test files).

import { describe, expect, it } from "vitest";
import type {
  ConverseEventType,
  ConverseStopReason,
} from "../src/vendor/mushy-author/ports/index.js";
import { SPINE_TOOL_NAMES } from "../src/vendor/mushy-author/ports/index.js";

const FROZEN_SPINE_TOOL_NAMES = [
  "catalog_search",
  "drawing_state",
  "ask_user",
  "run_capability",
  "job_status",
  "author_tool",
  "request_publication",
  "request_confirmation",
  "propose_overlay",
  "customize_platform",
  "finish_project",
  "project_completion_status",
] as const;

const FROZEN_EVENT_TYPES = [
  "turn_started",
  "text_delta",
  "tool_call",
  "tool_result",
  "job_linked",
  "proposed_run",
  "confirmation_required",
  "question_required",
  "confirmation_resolved",
  "turn_usage",
  "turn_complete",
  "session_state",
  "error",
] as const;

const FROZEN_STOP_REASONS = [
  "end_turn",
  "awaiting_approval",
  "cap_hit",
  "llm_quota_exhausted",
  "llm_rate_limited",
  "error",
  "timeout",
] as const;

// Compile-time exhaustiveness pins: each tuple slot fails typecheck if the
// union and the literal list diverge (member added OR removed). Exported so
// noUnusedLocals keeps the assertion instead of flagging it.
type AssertNever<T extends never> = T;
export type SpineVocabularyFreeze = [
  AssertNever<Exclude<ConverseEventType, (typeof FROZEN_EVENT_TYPES)[number]>>,
  AssertNever<Exclude<(typeof FROZEN_EVENT_TYPES)[number], ConverseEventType>>,
  AssertNever<Exclude<ConverseStopReason, (typeof FROZEN_STOP_REASONS)[number]>>,
  AssertNever<Exclude<(typeof FROZEN_STOP_REASONS)[number], ConverseStopReason>>,
];

describe("tenant spine constants stay frozen under the operator contract", () => {
  it("SPINE_TOOL_NAMES equals the twelve frozen literals, in order", () => {
    expect([...SPINE_TOOL_NAMES]).toEqual([...FROZEN_SPINE_TOOL_NAMES]);
  });

  it("no operator-namespaced name leaks into the tenant tool surface", () => {
    for (const name of SPINE_TOOL_NAMES) {
      expect(name.startsWith("operator")).toBe(false);
    }
  });
});
