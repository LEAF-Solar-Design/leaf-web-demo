// Lane C freeze + behavior gate: the OperatorLoop's sealed read-only
// catalog, gate-before-execute discipline, and the tenant surface staying
// byte-identical beside it.

import { describe, expect, it } from "vitest";
import {
  OPERATOR_READONLY_TOOLS,
  sealCatalog,
  type OperatorExecutor,
} from "../src/agent/operatorCatalog.js";
import { OperatorLoop, type OperatorRunner } from "../src/agent/operatorLoop.js";
import {
  OPERATOR_EVENT_TYPES,
  OPERATOR_STOP_REASONS,
  type OperatorTurnInput,
} from "../src/agent/operatorWire.js";
import { SPINE_TOOL_NAMES } from "../src/ports/index.js";

const FROZEN_READONLY_NAMES = [
  "operator_read_fleet_state",
  "operator_read_tenant_state",
  "operator_read_jobs",
  "operator_read_sessions",
  "operator_read_audit",
  "operator_read_worker_status",
] as const;

function fullExecutors(): Map<string, OperatorExecutor> {
  return new Map(
    OPERATOR_READONLY_TOOLS.map((t) => [
      t.name,
      async () => ({ ok: true, summary: `${t.name} ok` }),
    ]),
  );
}

const INPUT: OperatorTurnInput = {
  sessionId: "opsess-test",
  turnId: "opturn-test",
  text: "inspect the fleet",
  operator: {
    subject: "auth0|op-test",
    roleRevision: 1,
    profile: "default",
    environment: "staging",
  },
};

describe("operator wire vocabulary is pinned", () => {
  it("event types and stop reasons equal their frozen literals", () => {
    expect([...OPERATOR_EVENT_TYPES]).toEqual([
      "operator_turn_started", "operator_text_delta", "operator_tool_call",
      "operator_tool_result", "operator_proposed_action",
      "operator_authority_minted", "operator_authority_redeemed",
      "operator_turn_usage", "operator_turn_complete",
      "operator_session_state", "operator_error",
    ]);
    expect([...OPERATOR_STOP_REASONS]).toEqual([
      "end_turn", "awaiting_approval", "cap_hit", "error", "timeout",
    ]);
  });

  it("tenant spine constants are untouched beside the operator loop", () => {
    expect(SPINE_TOOL_NAMES).toHaveLength(10);
    for (const name of SPINE_TOOL_NAMES) {
      expect(name.startsWith("operator")).toBe(false);
    }
  });
});

describe("sealed read-only catalog", () => {
  it("first enabled catalog is exactly the frozen read-only names", () => {
    expect(OPERATOR_READONLY_TOOLS.map((t) => t.name)).toEqual([
      ...FROZEN_READONLY_NAMES,
    ]);
  });

  it("startup fails closed on a missing executor", () => {
    const executors = fullExecutors();
    executors.delete("operator_read_audit");
    expect(() => sealCatalog(executors)).toThrow(/no executor/);
  });

  it("startup fails closed on an executor outside the catalog", () => {
    const executors = fullExecutors();
    executors.set("operator_read_extra", async () => ({
      ok: true, summary: "x",
    }));
    expect(() => sealCatalog(executors)).toThrow(/not in catalog/);
  });

  it("startup refuses a write-shaped executor name", () => {
    const executors = fullExecutors();
    executors.set("operator_submit_job", async () => ({
      ok: true, summary: "x",
    }));
    expect(() => sealCatalog(executors)).toThrow(/read-only|not in catalog/);
  });
});

describe("gate-before-execute", () => {
  const runnerCalling = (tool: string, args = {}): OperatorRunner => ({
    async runTurn(_input, _tools, invoke) {
      const result = await invoke(tool, args);
      return { text: result.summary };
    },
  });

  it("every tool call passes the gate; deny never reaches the executor", async () => {
    let executed = 0;
    const executors = fullExecutors();
    executors.set("operator_read_jobs", async () => {
      executed += 1;
      return { ok: true, summary: "jobs" };
    });
    const gates: string[] = [];
    const loop = new OperatorLoop({
      executors,
      gate: async (tool) => {
        gates.push(tool);
        return { decision: "deny", reason: "kill_switch_active" };
      },
      runner: runnerCalling("operator_read_jobs"),
    });
    const result = await loop.runTurn(INPUT);
    expect(gates).toEqual(["operator_read_jobs"]);
    expect(executed).toBe(0);
    expect(result.toolCalls[0]).toMatchObject({
      tool: "operator_read_jobs",
      ok: false,
    });
    expect(result.toolCalls[0].summary).toContain("kill_switch_active");
  });

  it("an unknown tool is refused at the surface, not by model choice", async () => {
    let gateCalls = 0;
    const loop = new OperatorLoop({
      executors: fullExecutors(),
      gate: async () => {
        gateCalls += 1;
        return { decision: "allow" };
      },
      runner: runnerCalling("run_capability"), // a TENANT tool name
    });
    const result = await loop.runTurn(INPUT);
    expect(result.toolCalls[0].summary).toContain("unknown_tool");
    expect(gateCalls).toBe(0);
  });

  it("an allowed call executes and reports", async () => {
    const loop = new OperatorLoop({
      executors: fullExecutors(),
      gate: async () => ({ decision: "allow" }),
      runner: runnerCalling("operator_read_fleet_state"),
    });
    const result = await loop.runTurn(INPUT);
    expect(result.stopReason).toBe("end_turn");
    expect(result.toolCalls[0]).toMatchObject({
      tool: "operator_read_fleet_state",
      ok: true,
    });
  });

  it("a runner crash yields stopReason error, never a hang", async () => {
    const loop = new OperatorLoop({
      executors: fullExecutors(),
      gate: async () => ({ decision: "allow" }),
      runner: {
        async runTurn() {
          throw new Error("model exploded");
        },
      },
    });
    const result = await loop.runTurn(INPUT);
    expect(result.stopReason).toBe("error");
  });
});
