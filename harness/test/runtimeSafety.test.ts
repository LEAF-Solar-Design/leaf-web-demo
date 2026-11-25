import { describe, expect, it } from "vitest";

import { FakeTurnRunner } from "../src/ports/fakes/fakeTurnRunner.js";
import { BuildPlan, Questions, TurnAnswers, planDigest } from "../src/ports/index.js";
import type { ConverseTurnInput, HarnessTurnEvent } from "../src/ports/index.js";
import {
  assertAuthoredSandboxBoundary,
  authoredExecutionEnabled,
  authorRunnerMode,
  authorSandboxProvider,
  validateProductionHarnessEnv,
} from "../src/runtimeSafety.js";

const VALID = {
  LEAF_RUNTIME_ENV: "production",
  LEAF_AGENT_MOCK: "0",
  LEAF_HARNESS_AUTH: "1",
  LEAF_HARNESS_SECRET: "harness-secret",
  LEAF_BROKER_SECRET: "broker-secret",
  LEAF_AUTHORED_EXECUTION: "0",
};

describe("production harness runtime safety", () => {
  it("defaults authored execution off in every runtime posture", () => {
    expect(authoredExecutionEnabled({})).toBe(false);
    expect(authoredExecutionEnabled({ LEAF_RUNTIME_ENV: "staging" })).toBe(false);
    expect(authoredExecutionEnabled({ LEAF_AUTHORED_EXECUTION: "1" })).toBe(true);
  });

  it("leaves non-production startup unchanged", () => {
    expect(() => validateProductionHarnessEnv({})).not.toThrow();
  });

  it("accepts the explicit production-safe contract", () => {
    expect(() => validateProductionHarnessEnv(VALID)).not.toThrow();
  });

  it("rejects any production mock-agent value other than exact zero", () => {
    expect(() => validateProductionHarnessEnv({ ...VALID, LEAF_AGENT_MOCK: "1" }))
      .toThrow(/LEAF_AGENT_MOCK=0/);
    expect(() => validateProductionHarnessEnv({ ...VALID, LEAF_AGENT_MOCK: undefined }))
      .toThrow(/LEAF_AGENT_MOCK=0/);
  });

  it("rejects disabled harness caller authentication", () => {
    expect(() => validateProductionHarnessEnv({ ...VALID, LEAF_HARNESS_AUTH: "0" }))
      .toThrow(/LEAF_HARNESS_AUTH=1/);
  });

  it("rejects a missing harness secret", () => {
    expect(() => validateProductionHarnessEnv({ ...VALID, LEAF_HARNESS_SECRET: " " }))
      .toThrow(/LEAF_HARNESS_SECRET/);
  });

  it("rejects a missing broker secret", () => {
    expect(() => validateProductionHarnessEnv({ ...VALID, LEAF_BROKER_SECRET: "\n" }))
      .toThrow(/LEAF_BROKER_SECRET/);
  });

  it("requires an explicit authored-execution selector in production", () => {
    expect(() => validateProductionHarnessEnv({
      ...VALID,
      LEAF_AUTHORED_EXECUTION: undefined,
    })).toThrow(/explicit LEAF_AUTHORED_EXECUTION/);
  });

  it("preserves legacy local author fallback with explicit-selector precedence", () => {
    expect(authorSandboxProvider({ LEAF_SANDBOX: "e2b" })).toBe("e2b");
    expect(authorSandboxProvider({
      LEAF_SANDBOX: "e2b",
      LEAF_AUTHOR_SANDBOX_PROVIDER: "off",
    })).toBe("off");
    expect(authorSandboxProvider({
      LEAF_SANDBOX: "off",
      LEAF_AUTHOR_SANDBOX_PROVIDER: "e2b",
    })).toBe("e2b");
  });

  it("always selects AgentSdkRunner outside the explicit fake mode", () => {
    expect(authorRunnerMode({})).toBe("agent-sdk");
    expect(authorRunnerMode({ LEAF_AUTHOR_SANDBOX_PROVIDER: "e2b" })).toBe("agent-sdk");
    expect(authorRunnerMode({ LEAF_AGENT_MOCK: "1" })).toBe("fake");
  });

  it("keeps the Claude grant on the harness and the E2B credential on the broker", () => {
    const authored = {
      ...VALID,
      LEAF_AUTHORED_EXECUTION: "1",
      LEAF_HARNESS_SESSION_STORE: "postgres",
      LEAF_TOOL_SANDBOX_PROVIDER: "e2b",
    };
    expect(() => validateProductionHarnessEnv(authored)).not.toThrow();
    expect(() => validateProductionHarnessEnv({
      ...authored,
      LEAF_AUTHOR_SANDBOX_PROVIDER: "e2b",
    })).toThrow(/trusted harness host/);
    expect(() => validateProductionHarnessEnv({
      ...authored,
      E2B_API_KEY: "must-belong-to-broker",
    })).toThrow(/must not receive E2B credentials/);
  });

  it("requires PostgreSQL session authority for production authored execution", () => {
    const authored = {
      ...VALID,
      LEAF_AUTHORED_EXECUTION: "1",
      LEAF_TOOL_SANDBOX_PROVIDER: "e2b",
    };
    expect(() => validateProductionHarnessEnv({
      ...authored,
      LEAF_HARNESS_SESSION_STORE: "file",
    })).toThrow(/LEAF_HARNESS_SESSION_STORE=postgres/);
    expect(() => validateProductionHarnessEnv(authored))
      .toThrow(/LEAF_HARNESS_SESSION_STORE=postgres/);
  });

  it("requires the broker E2B tool sandbox before production authored execution", () => {
    expect(() => validateProductionHarnessEnv({
      ...VALID,
      LEAF_AUTHORED_EXECUTION: "1",
      LEAF_HARNESS_SESSION_STORE: "postgres",
    })).toThrow(/LEAF_TOOL_SANDBOX_PROVIDER=e2b/);
  });
});

describe("authored-execution sandbox boundary is posture-independent", () => {
  it("does nothing when authored execution is not armed", () => {
    expect(() => assertAuthoredSandboxBoundary({})).not.toThrow();
    expect(() => assertAuthoredSandboxBoundary({ LEAF_RUNTIME_ENV: "staging" }))
      .not.toThrow();
  });

  it("refuses an armed NON-production harness with no tool sandbox provider", () => {
    // The old guard returned early for any non-production posture, so an armed
    // staging harness with no sandbox escaped entirely. It must now fail closed.
    expect(() => assertAuthoredSandboxBoundary({
      LEAF_RUNTIME_ENV: "staging",
      LEAF_AUTHORED_EXECUTION: "1",
    })).toThrow(/LEAF_TOOL_SANDBOX_PROVIDER=e2b/);
    expect(() => validateProductionHarnessEnv({
      LEAF_RUNTIME_ENV: "staging",
      LEAF_AUTHORED_EXECUTION: "1",
    })).toThrow(/LEAF_TOOL_SANDBOX_PROVIDER=e2b/);
  });

  it("accepts an armed non-production harness with the correct boundary", () => {
    const armed = {
      LEAF_RUNTIME_ENV: "staging",
      LEAF_AUTHORED_EXECUTION: "1",
      LEAF_TOOL_SANDBOX_PROVIDER: "e2b",
      LEAF_AUTHOR_SANDBOX_PROVIDER: "off",
    };
    expect(() => assertAuthoredSandboxBoundary(armed)).not.toThrow();
    expect(() => assertAuthoredSandboxBoundary({ ...armed, LEAF_AUTHOR_SANDBOX_PROVIDER: undefined }))
      .not.toThrow();
  });

  it("refuses an author sandbox provider or an E2B credential in any posture", () => {
    const armed = {
      LEAF_RUNTIME_ENV: "staging",
      LEAF_AUTHORED_EXECUTION: "1",
      LEAF_TOOL_SANDBOX_PROVIDER: "e2b",
    };
    expect(() => assertAuthoredSandboxBoundary({
      ...armed,
      LEAF_AUTHOR_SANDBOX_PROVIDER: "e2b",
    })).toThrow(/trusted harness host/);
    expect(() => assertAuthoredSandboxBoundary({
      ...armed,
      E2B_API_KEY: "must-belong-to-broker",
    })).toThrow(/must not receive E2B credentials/);
  });
});

// SDS-PMW: the scripted plan path lives in FakeTurnRunner, which serve.ts mounts only
// under LEAF_AGENT_MOCK=1; production refuses that posture (asserted first below).
describe("mock plan walk (LEAF_AGENT_MOCK=1 only)", () => {
  function turn(overrides: Partial<ConverseTurnInput> = {}): ConverseTurnInput {
    return {
      tenant_id: "tenant-1", session_id: "session-1", turn_id: "turn-1",
      drawing_id: "drawing-1", messages: [], ...overrides,
    };
  }

  async function drain(input: ConverseTurnInput): Promise<HarnessTurnEvent[]> {
    const events: HarnessTurnEvent[] = [];
    for await (const event of new FakeTurnRunner().runTurn(input)) events.push(event);
    return events;
  }

  const types = (events: HarnessTurnEvent[]) => events.map((event) => String(event.type));

  it("is reachable only in the mock posture, which production refuses", () => {
    expect(authorRunnerMode({ LEAF_AGENT_MOCK: "1" })).toBe("fake");
    expect(authorRunnerMode({ LEAF_AGENT_MOCK: "0" })).toBe("agent-sdk");
    expect(() => validateProductionHarnessEnv({ ...VALID, LEAF_AGENT_MOCK: "1" }))
      .toThrow(/LEAF_AGENT_MOCK=0/);
  });

  it("asks one valid planning question in plan mode", async () => {
    for (const input of [
      turn({ text: "planwalk: a tool that tallies entities per layer" }),
      turn({ text: "Make a layer tally tool", context_packet: { inline_authoring: { plan_mode: true } } }),
    ]) {
      const events = await drain(input);
      expect(types(events)).toEqual(["text_delta", "questions_required", "turn_complete"]);
      const data = events[1]!.data as { question_id: unknown; questions: Array<Record<string, unknown>> };
      expect(data.question_id).toBe("questions-turn-1");
      expect(data.questions).toHaveLength(1);
      expect(data.questions[0]!.id).toBe("q1");
      const { header, question, multiSelect, options } = data.questions[0]!;
      expect(Questions.safeParse([{ header, question, multiSelect, options }]).success).toBe(true);
      expect((options as unknown[]).length).toBe(2);
      expect(events[2]!.data).toEqual({ stop_reason: "end_turn" });
    }
  });

  it("keeps the existing routes when plan mode is off", async () => {
    expect(types(await drain(turn({ text: "Please plan a layer tally" }))))
      .not.toContain("questions_required");
    expect(types(await drain(turn({
      text: "Please plan a layer tally", context_packet: { inline_authoring: { plan_mode: false } },
    })))).not.toContain("questions_required");
    expect(types(await drain(turn({ text: "please approve this" }))))
      .toEqual(["proposed_run", "confirmation_required", "turn_complete"]);
  });

  it("answers yield a valid plan awaiting approval", async () => {
    const answers = TurnAnswers.parse({ question_id: "questions-turn-1", answers: { q1: { labels: ["Table"] } } });
    const events = await drain(turn({ turn_id: "turn-2", answers }));
    expect(types(events)).toEqual(["text_delta", "plan_proposed", "turn_complete"]);
    const data = events[1]!.data as Record<string, unknown>;
    expect(data.confirmation_id).toBe("plan-turn-2");
    expect(data.revision).toBe(1);
    const plan = BuildPlan.parse(data.plan);
    expect(plan).toEqual(data.plan);
    expect(data.plan_digest).toBe(planDigest(plan));
    expect(plan.title).toBe("Layer tally");
    expect(plan.answers).toEqual({ q1: { labels: ["Table"] } });
    expect(events[2]!.data).toEqual({ stop_reason: "awaiting_approval" });

    const other = TurnAnswers.parse({ question_id: "questions-turn-1", answers: { q1: { other: "A chart" } } });
    const revised = await drain(turn({ turn_id: "turn-3", answers: other }));
    expect(BuildPlan.parse((revised[1]!.data as { plan: unknown }).plan).answers)
      .toEqual({ q1: { other: "A chart" } });
  });

  it("an approved plan resumes and ends the turn; a denial does not build", async () => {
    const proposal = { tool: null as unknown as string, params: null as unknown as Record<string, unknown> };
    const approved = await drain(turn({
      turn_id: "turn-4", confirm: { confirmation_id: "plan-turn-2", approved: true, proposal },
    }));
    expect(types(approved)).toEqual(["text_delta", "turn_complete"]);
    expect(String(approved[0]!.data.text)).toContain("Plan approved");
    expect(approved[1]!.data).toEqual({ stop_reason: "end_turn" });

    const denied = await drain(turn({
      turn_id: "turn-5",
      confirm: { confirmation_id: "plan-turn-2", approved: false, proposal, feedback: "Add a total row" },
    }));
    expect(types(denied)).toEqual(["text_delta", "turn_complete"]);
    expect(String(denied[0]!.data.text)).toContain("Add a total row");
    expect(String(denied[0]!.data.text)).not.toContain("approved");
  });
});
