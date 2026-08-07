// OperatorLoop — the sibling loop for the operator control plane (Lane C).
// Shares the harness process with the tenant ConverseLoop but nothing else:
// its own wire (operatorWire.ts), its own sealed read-only catalog
// (operatorCatalog.ts), its own runner port. The tenant SPINE_TOOL_NAMES
// surface and /turn route are untouched.
//
// The runner is injectable (the repo's fake-runner idiom): production wires
// an Agent SDK runner when an operator model grant is configured; tests
// drive the loop with a deterministic fake. EVERY tool call — regardless of
// runner — passes through the gate callback before its executor runs; a
// deny returns as an error result the model can relay, and the call never
// reaches the executor.

import {
  OPERATOR_READONLY_TOOLS,
  sealCatalog,
  type OperatorExecutor,
} from "./operatorCatalog.js";
import type {
  OperatorTurnInput,
  OperatorTurnResult,
} from "./operatorWire.js";

export type OperatorGate = (
  tool: string,
  args: Record<string, unknown>,
  input: OperatorTurnInput,
) => Promise<{ decision: "allow" | "deny"; reason?: string }>;

/** A runner turns text + tool surface into tool invocations and prose.
 * The loop owns gating and execution; the runner owns model behavior. */
export interface OperatorRunner {
  runTurn(
    input: OperatorTurnInput,
    tools: ReadonlyArray<{ name: string; description: string }>,
    invoke: (
      tool: string,
      args: Record<string, unknown>,
    ) => Promise<{ ok: boolean; summary: string; data?: unknown }>,
  ): Promise<{ text: string; stopReason?: "end_turn" | "cap_hit" }>;
}

export class OperatorLoop {
  private readonly executors: Map<string, OperatorExecutor>;
  private readonly gate: OperatorGate;
  private readonly runner: OperatorRunner;

  constructor(opts: {
    executors: Map<string, OperatorExecutor>;
    gate: OperatorGate;
    runner: OperatorRunner;
  }) {
    // Startup seal: throws on any catalog/executor mismatch or a
    // write-shaped name; a partial operator surface must never boot.
    sealCatalog(opts.executors);
    this.executors = opts.executors;
    this.gate = opts.gate;
    this.runner = opts.runner;
  }

  listTools(): string[] {
    return OPERATOR_READONLY_TOOLS.map((t) => t.name);
  }

  async runTurn(input: OperatorTurnInput): Promise<OperatorTurnResult> {
    const toolCalls: OperatorTurnResult["toolCalls"] = [];

    const invoke = async (
      tool: string,
      args: Record<string, unknown>,
    ): Promise<{ ok: boolean; summary: string; data?: unknown }> => {
      const executor = this.executors.get(tool);
      if (!executor) {
        // Unknown tool: refused at the surface, never model-discretionary.
        const result = {
          ok: false,
          summary: `unknown_tool: ${tool} is not in the operator catalog`,
        };
        toolCalls.push({ tool, ok: false, summary: result.summary });
        return result;
      }
      const verdict = await this.gate(tool, args, input);
      if (verdict.decision !== "allow") {
        const result = {
          ok: false,
          summary: `denied: ${verdict.reason ?? "operator_gate_denied"}`,
        };
        toolCalls.push({ tool, ok: false, summary: result.summary });
        return result;
      }
      try {
        const result = await executor(args);
        toolCalls.push({ tool, ok: result.ok, summary: result.summary });
        return result;
      } catch (err) {
        const summary = `executor_error: ${String(err).slice(0, 300)}`;
        toolCalls.push({ tool, ok: false, summary });
        return { ok: false, summary };
      }
    };

    try {
      const outcome = await this.runner.runTurn(
        input,
        OPERATOR_READONLY_TOOLS.map((t) => ({
          name: t.name,
          description: t.description,
        })),
        invoke,
      );
      return {
        stopReason: outcome.stopReason ?? "end_turn",
        text: outcome.text,
        toolCalls,
      };
    } catch (err) {
      return {
        stopReason: "error",
        text: `operator turn failed: ${String(err).slice(0, 300)}`,
        toolCalls,
      };
    }
  }
}
